#!/usr/bin/env python3
"""Jericho Beach wind forecast alert.

Fetches forecast data from bigwavedave.ca, parses the Model1/Model2 output,
and reports sustained wind windows per the configured model strategy (model
intersection by default; Model 2 alone when USE_MODEL_2_ONLY is set). Alerts
are sent via ntfy.sh when NTFY_TOPIC is set in the environment.
"""

import datetime as dt
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from zoneinfo import ZoneInfo

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

# If True, ignore Model 1 entirely when both models have data (use Model 2 alone).
# If False, intersect both models when both have data (Model 1 must corroborate Model 2).
# Either way: Model 2 alone when only Model 2 has data; no alert when only Model 1 has data.
USE_MODEL_2_ONLY = False

WIND_THRESHOLD_KTS = 10
WIND_SUSTAINED_TIME_MIN_HOURS = 2
WIND_DATAPOINT_TIME_DELTA_HOURS = 1

FORECAST_DAYS = 3

JERICHO_SITE_NAME = "jericho"
SITES = {
    "jericho": {"id": 20, "page": "jerichobch.html"},
    "english_bay": {"id": 9, "page": "englishbay.html"},
}

MODEL_1_START_STRING = "-9998"
MODEL_2_START_STRING = "-9999"
MODEL_1_NAME = "model1"
MODEL_2_NAME = "model2"
MODEL_NAMES = (MODEL_1_NAME, MODEL_2_NAME)

HTTP_TIMEOUT_SECONDS = 10
PACIFIC_TIMEZONE = ZoneInfo("America/Vancouver")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wind-alert")

# Browser-impersonating session — bigwavedave sits behind Cloudflare, which
# fingerprints requests at the TLS layer. curl_cffi's impersonate="chrome"
# replays a real Chrome TLS handshake + header order, which header-only fixes
# couldn't. We still prime the session against the public page first so any
# clearance cookies attach.
_session = requests.Session(impersonate="chrome")


@dataclass
class WindReading:
    timestamp: dt.datetime
    wind_kts: int


@dataclass
class Interval:
    start: dt.datetime
    end: dt.datetime


@dataclass
class DayResult:
    """One day's forecast analysis.

    intervals/models_used combinations:
        intervals is None              — upstream returned no data at all
        intervals=[],  models_used=[]  — Model 2 unavailable; Model-1-only alert suppressed
        intervals=[],  models_used=[X] — data found, no sustained wind
        intervals=[..]                 — sustained wind windows
    """
    date: dt.date
    models_used: list[str]
    intervals: list[Interval] | None


DATE_FORMAT = "%a %b %-d"
DATETIME_FORMAT = "%a %b %-d %-I:%M %p"
TIME_FORMAT = "%-I:%M %p"


def fmt_date(d: dt.date) -> str:
    """Format a date as e.g. 'Wed May 27'."""
    return d.strftime(DATE_FORMAT)


def fmt_datetime(d: dt.datetime) -> str:
    """Format a datetime as e.g. 'Wed May 27 2:00 PM'."""
    return d.strftime(DATETIME_FORMAT)


def fmt_time(d: dt.datetime) -> str:
    """Format a datetime's time-of-day as e.g. '2:00 PM'."""
    return d.strftime(TIME_FORMAT)


def fmt_models(model_names: list[str]) -> str:
    """Format model name list for display, e.g. ['model1', 'model2'] -> '1, 2'."""
    return ", ".join(name.removeprefix("model") for name in model_names)


def forecast_page_url(site_name: str) -> str:
    """Return the public forecast page URL for a site."""
    site = SITES[site_name]
    return f"https://bigwavedave.ca/{site['page']}?site={site['id']}"


def prime_session(site_name: str) -> None:
    """Visit the public forecast page so Cloudflare can issue clearance cookies."""
    try:
        _session.get(forecast_page_url(site_name), timeout=HTTP_TIMEOUT_SECONDS)
    except RequestException as e:
        log.warning("Failed to prime session for %s: %s", site_name, e)


def fetch_wind_data(date: dt.date, site_name: str) -> str:
    """Fetch the raw forecast text for a single day. Returns '' on failure."""
    url = f"https://bigwavedave.ca/sqlwind/extractwinddata.php?site={SITES[site_name]['id']}&day={date}"
    try:
        resp = _session.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"Referer": forecast_page_url(site_name)},
        )
        resp.raise_for_status()
        return resp.text
    except RequestException as e:
        log.warning("Failed to fetch %s for %s: %s", site_name, date, e)
        return ""


def split_models(wind_data: str) -> dict[str, list[str]]:
    """Split raw forecast text into per-model line lists keyed by model name.

    Assumes the upstream layout where the Model 2 section (-9999) precedes
    the Model 1 section (-9998) in the response. We extract Model 1 from the
    tail of the response, then look for Model 2 in everything before it.
    """
    result: dict[str, list[str]] = {MODEL_1_NAME: [], MODEL_2_NAME: []}

    if MODEL_1_START_STRING in wind_data:
        before_m1, _, after_m1 = wind_data.rpartition(MODEL_1_START_STRING)
        result[MODEL_1_NAME] = after_m1.split("\n")
        m2_search = before_m1
    else:
        m2_search = wind_data

    if MODEL_2_START_STRING in m2_search:
        _, _, after_m2 = m2_search.rpartition(MODEL_2_START_STRING)
        result[MODEL_2_NAME] = after_m2.split("\n")

    return result


def parse_readings(model_data: list[str]) -> list[WindReading]:
    """Parse raw tab-separated model lines into WindReading objects."""
    readings = []
    for raw in model_data:
        if not raw:
            continue
        ts, kts, *_ = raw.split("\t")
        readings.append(WindReading(dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"), int(kts)))
    return readings


def extract_above_threshold(readings: list[WindReading]) -> list[WindReading]:
    """Filter readings whose wind exceeds the threshold."""
    return [r for r in readings if r.wind_kts > WIND_THRESHOLD_KTS]


def merge_consecutive_intervals(readings: list[WindReading]) -> list[Interval]:
    """Collapse consecutive hourly readings into Interval(start, end)."""
    if not readings:
        return []

    hourly = dt.timedelta(hours=WIND_DATAPOINT_TIME_DELTA_HOURS)
    intervals = []
    start = prev = readings[0].timestamp

    for r in readings[1:]:
        if r.timestamp == prev + hourly:
            prev = r.timestamp
        else:
            intervals.append(Interval(start, prev))
            start = prev = r.timestamp

    intervals.append(Interval(start, prev))
    return intervals


def combine_intervals(
    model_intervals: dict[str, list[Interval]],
    models_raw: dict[str, list[str]],
) -> tuple[list[str], list[Interval]]:
    """Combine per-model intervals based on data availability.

    Returns (models_used, intervals):
    - Both models have raw data: intersect their intervals. The intersection
      may be empty if they disagree, which correctly suppresses lone-model
      alerts. models_used = [model1, model2].
    - Only Model 2 has raw data: return Model 2's intervals as-is.
    - Only Model 1 has raw data (Model 2 unavailable): suppress — Model 1 is
      the less-trusted source (read off output plots), so we never alert on
      it alone. ([], []).
    - Neither has data: ([], []).
    """
    m1_has_data = bool(models_raw[MODEL_1_NAME])
    m2_has_data = bool(models_raw[MODEL_2_NAME])

    if not m2_has_data:
        # Covers both "neither" and "only Model 1" — Model 1 alone never alerts.
        return [], []

    if not m1_has_data:
        return [MODEL_2_NAME], model_intervals[MODEL_2_NAME]

    overlap: list[Interval] = []
    for i1 in model_intervals[MODEL_1_NAME]:
        for i2 in model_intervals[MODEL_2_NAME]:
            start = max(i1.start, i2.start)
            end = min(i1.end, i2.end)
            if start < end:
                overlap.append(Interval(start, end))
    return [MODEL_1_NAME, MODEL_2_NAME], overlap


def choose_intervals(
    models_merged: dict[str, list[Interval]],
    models_raw: dict[str, list[str]],
) -> tuple[list[str], list[Interval]]:
    """Apply the per-day model strategy. Returns (models_used, intervals)."""
    if USE_MODEL_2_ONLY and models_raw[MODEL_2_NAME]:
        return [MODEL_2_NAME], models_merged[MODEL_2_NAME]
    return combine_intervals(models_merged, models_raw)


def filter_sustained(intervals: list[Interval]) -> list[Interval]:
    """Drop intervals shorter than the sustained-wind minimum."""
    threshold = dt.timedelta(hours=WIND_SUSTAINED_TIME_MIN_HOURS)
    return [iv for iv in intervals if iv.end - iv.start >= threshold]


def analyze_day(date: dt.date, site_name: str) -> DayResult:
    """Fetch, parse, and analyze one day's forecast; log and return a summary."""
    raw = fetch_wind_data(date, site_name)
    models_raw = split_models(raw)

    if not models_raw[MODEL_1_NAME] and not models_raw[MODEL_2_NAME]:
        log.info("%s: no data returned", fmt_date(date))
        return DayResult(date=date, models_used=[], intervals=None)

    models_readings = {name: parse_readings(models_raw[name]) for name in MODEL_NAMES}
    models_cleaned = {name: extract_above_threshold(models_readings[name]) for name in MODEL_NAMES}
    for name in MODEL_NAMES:
        log.info(
            "%s %s: %d readings above %d kts",
            fmt_date(date),
            name,
            len(models_cleaned[name]),
            WIND_THRESHOLD_KTS,
        )

    models_merged = {name: merge_consecutive_intervals(models_cleaned[name]) for name in MODEL_NAMES}
    models_used, combined = choose_intervals(models_merged, models_raw)
    sustained = filter_sustained(combined)

    if sustained:
        log.info("%s sustained windows (sources: %s):", fmt_date(date), models_used)
        for iv in sustained:
            log.info("  %s -> %s", fmt_datetime(iv.start), fmt_datetime(iv.end))
    elif not models_used:
        log.info("%s: Model 2 unavailable; suppressing Model-1-only alert", fmt_date(date))
    else:
        log.info("%s: no sustained windy periods", fmt_date(date))

    return DayResult(date=date, models_used=models_used, intervals=sustained)


def build_message(results: list[DayResult], site_name: str) -> str:
    """Build the human-readable alert body listing windy days and missing days."""
    lines = []
    for day in results:
        if day.intervals is None:
            lines.append(f"{fmt_date(day.date)}: no forecast data available")
        elif not day.models_used:
            lines.append(f"{fmt_date(day.date)}: Model 2 unavailable")
        elif not day.intervals:
            lines.append(f"{fmt_date(day.date)} (via {fmt_models(day.models_used)}):")
            lines.append("> no sustained wind")
        else:
            lines.append(f"{fmt_date(day.date)} (via {fmt_models(day.models_used)}):")
            for iv in day.intervals:
                if iv.start.date() == iv.end.date():
                    lines.append(f"> {fmt_time(iv.start)} - {fmt_time(iv.end)}")
                else:
                    lines.append(f"> {fmt_datetime(iv.start)} - {fmt_datetime(iv.end)}")
        lines.append("")
    lines.append(f"Threshold: >{WIND_THRESHOLD_KTS}kt for {WIND_SUSTAINED_TIME_MIN_HOURS}+ hours")
    lines.append(forecast_page_url(site_name))
    return "\n".join(lines)


def send_notification(message: str) -> None:
    """Post the message to ntfy.sh, or log it locally if NTFY_TOPIC is unset."""
    if not NTFY_TOPIC:
        log.warning("NTFY_TOPIC not set; skipping notification.")
        log.info("Would have sent:\n%s", message)
        return

    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "Jericho Beach Wind Alert!", "Priority": "high", "Tags": "wind_face"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        log.info("Notification sent (status %d)", resp.status_code)
    except RequestException as e:
        log.error("Failed to send notification: %s", e)
    log.info("Body:\n%s", message)


def main() -> int:
    """Run the forecast check across the configured day range."""
    log.info(
        "Jericho Beach Wind Alert Check - alert on sustained wind >%d kts for >=%d hrs",
        WIND_THRESHOLD_KTS,
        WIND_SUSTAINED_TIME_MIN_HOURS,
    )

    today = dt.datetime.now(PACIFIC_TIMEZONE).date()
    days = [today + dt.timedelta(days=i) for i in range(FORECAST_DAYS)]
    log.info("Checking days: %s", [fmt_date(d) for d in days])

    prime_session(JERICHO_SITE_NAME)

    with ThreadPoolExecutor(max_workers=len(days)) as pool:
        results = list(pool.map(partial(analyze_day, site_name=JERICHO_SITE_NAME), days))

    windy_days = [r for r in results if r.intervals]
    missing_days = [r for r in results if r.intervals is None]

    if windy_days or missing_days:
        if windy_days:
            log.info("ALERT - %d day(s) with sustained wind", len(windy_days))
        if missing_days:
            log.info("ALERT - %d day(s) with no forecast data", len(missing_days))
        send_notification(build_message(results, JERICHO_SITE_NAME))
    else:
        log.info("No sustained wind >%d kts in forecast. All clear.", WIND_THRESHOLD_KTS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
