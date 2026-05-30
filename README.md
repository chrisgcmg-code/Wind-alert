# Wind-alert

A small Python script that checks the Jericho Beach (Vancouver) wind forecast
twice a day, looks for sustained windy windows, and posts an alert to
[ntfy.sh](https://ntfy.sh/) if it finds any. Runs on GitHub Actions.

Forecast data comes from [bigwavedave.ca](https://bigwavedave.ca/jerichobch.html?site=20).
Two model runs are parsed: HRDPS (Environment Canada, 2.5 km) and WRF-GFS
(U. Washington).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # includes pytest
.venv/bin/pytest                                # run tests
NTFY_TOPIC='your-ntfy-topic' .venv/bin/python check_wind.py   # one-off run
```

For CI, set `NTFY_TOPIC` as a repository **Secret** (ntfy topics have no
auth — anyone with the topic name can read or spam your alerts).

The workflow ([.github/workflows/wind_alert.yml](.github/workflows/wind_alert.yml))
fires twice daily on cron — `14:00 UTC` (≈ 7 AM Pacific) and `23:00 UTC`
(≈ 4 PM Pacific) — and can also be run on demand from the **Actions** tab via
"Run workflow."

## Project layout

- [check_wind.py](check_wind.py) — main script (fetch → parse → analyze → notify).
- [test_check_wind.py](test_check_wind.py) — pytest suite.
- [requirements.txt](requirements.txt) / [requirements-dev.txt](requirements-dev.txt) — runtime and dev dependencies.
- [.github/workflows/wind_alert.yml](.github/workflows/wind_alert.yml) — Alert workflow (cron + manual dispatch).
- [.github/workflows/test.yml](.github/workflows/test.yml) — Runs pytest on push to main and on PRs.

## How it works

1. Fetch forecast text for the next `FORECAST_DAYS` (default 3) in parallel.
2. Parse Model 1 (`-9998`) and Model 2 (`-9999`) sections.
3. Filter each model's readings to those above `WIND_THRESHOLD_KTS`.
4. Collapse consecutive hourly readings into `(start, end)` intervals.
5. Combine the per-day models per the configured strategy (see below).
6. Drop intervals shorter than `WIND_SUSTAINED_TIME_MIN_HOURS`.
7. If any sustained windows remain — or any day had no upstream data — post a message to ntfy.

### Sample alert

```
Wed May 27 (via 2):
> 11:00 AM - 3:00 PM

Thu May 28 (via 1, 2):
> no sustained wind

Fri May 29 (via 1, 2):
> 8:00 AM - 12:00 PM
> 2:00 PM - 5:00 PM

Threshold: >10kt for 2+ hours
https://bigwavedave.ca/jerichobch.html?site=20
```

## Forecast models

The upstream feed exposes two model runs, marked with sentinel lines in the
response (`-9998` for Model 1, `-9999` for Model 2):

- **Model 1 — WRF-GFS (U. Washington).** High-resolution WRF driven by GFS;
  values are read off the model's output plots, so they come back blocky
  (5-knot quantization) and occasionally spiky from interpretation noise.
  [Model details](https://a.atmos.washington.edu/wrfrt/info.html).
- **Model 2 — HRDPS (Environment Canada).** 2.5 km GEM-based, tuned on
  Canadian observations; the site owner has direct raw access, so values
  are cleaner. The trusted baseline.
  [Technical specs (PDF)](https://collaboration.cmc.ec.gc.ca/cmc/CMOI/product_guide/docs/tech_specifications/tech_specifications_HRDPS_e.pdf).

## Configuration

Constants at the top of [check_wind.py](check_wind.py):

| Constant                          | Default | Meaning                                                       |
| --------------------------------- | ------- | ------------------------------------------------------------- |
| `USE_MODEL_2_ONLY`                | `False` | If True, ignore Model 1 entirely when both models have data (use Model 2 alone). If False, intersect both models when both have data (Model 1 must corroborate). Either way: Model 2 alone when only Model 2 has data; no alert when only Model 1 has data. |
| `WIND_THRESHOLD_KTS`              | `10`    | Strict-greater threshold; `10` is *not* alerted on, `11` is. |
| `WIND_SUSTAINED_TIME_MIN_HOURS`   | `2`     | An alert requires sustained wind over this many hours.        |
| `WIND_DATAPOINT_TIME_DELTA_HOURS` | `1`     | Spacing between forecast readings (don't change unless upstream changes). |
| `FORECAST_DAYS`                   | `3`     | How many days ahead to check, starting today.                 |
| `HTTP_TIMEOUT_SECONDS`            | `10`    | Per-request timeout for both data fetch and ntfy POST.        |

Sites are configured in the `SITES` dict; add an entry to support a new
beach (English Bay is already wired in but not yet used for alerts).

Environment variables:

| Variable     | Required | Notes                                                  |
| ------------ | -------- | ------------------------------------------------------ |
| `NTFY_TOPIC` | no       | If unset, the script logs the would-be body and skips the POST. Useful for local testing. |

## Corner cases & design notes

A handful of decisions that wouldn't be obvious from the code:

- **Vancouver is treated as fixed UTC-7 year-round.** `ZoneInfo("America/Vancouver")`
  is used so the tzdb is authoritative if rules change, but the user-stated
  intent is that DST doesn't apply here. Don't be surprised by the lack of
  PST handling.
- **Wind speed is treated as a scalar.** Direction is parsed but discarded.
  We never average wind speeds across readings because magnitude-only averages
  of a vector quantity are misleading.
- **Strict-greater threshold.** A reading of exactly `WIND_THRESHOLD_KTS`
  is excluded. This is intentional — 10 kt is the "not windy enough yet"
  case, not the "just barely windy" case.
- **Sustained filter is `delta >= MIN_HOURS`.** A two-reading interval (e.g.
  10:00 → 11:00, delta 1h) is dropped. A three-reading interval (10:00 → 12:00,
  delta 2h) is kept.
- **`combine_intervals` semantics by raw-data availability:**
  - Both models have raw data → return the intersection of their above-threshold
    intervals. This *correctly* suppresses an alert when one model predicts
    wind and the other (with data) disagrees.
  - Only Model 2 has raw data → use Model 2's intervals.
  - Only Model 1 has raw data → suppress the alert. Model 1 is the
    less-trusted source (read off output plots), so we never alert on
    it alone; the day shows up as "Model 2 unavailable" in any alert
    that fires for other days.
  - Neither has raw data → empty (the day is reported as "no forecast data").
- **Model trust hierarchy.** Model 2 (HRDPS, raw data) is the trusted baseline;
  Model 1 (WRF-GFS, scraped from output plots) is blocky and noisy. When the
  upstream has both, the intersection logic gives a high-confidence signal.
- **Per-day reporting carries model attribution when Model 2 was consulted.**
  Calm and windy days that ran through the strategy include `(via 1, 2)` or
  `(via 2)` so it's clear which forecast was used. The two exceptions are
  `"no forecast data available"` (upstream returned nothing) and `"Model 2
  unavailable"` (only Model 1 came back, suppression triggered) — neither
  carries a `(via …)` tag because there was no Model-2-backed analysis to
  attribute.
- **Failed HTTP requests degrade gracefully.** A failed `extractwinddata.php`
  fetch logs a warning and returns `""`, which propagates to a "no forecast
  data" report for that day rather than crashing.

## Troubleshooting

- **No notifications arriving.** Check that `NTFY_TOPIC` is set as a repo
  **Secret** (not a Variable), and that the topic name you subscribed to in
  the ntfy app matches exactly. The workflow logs `"NTFY_TOPIC not set"` if
  it can't see one.
- **Alert says "no forecast data available" every day.** The upstream
  `extractwinddata.php` is unreachable or returning empty. The workflow run
  log will show a `Failed to fetch ...` warning. Usually transient — wait
  for the next run.
- **A windy window I expected is missing.** Most often this is `combine_intervals`
  suppression at work: Model 2 returned data but didn't predict wind, so the
  lone-Model-1 alert was correctly suppressed. The Actions run log shows
  per-model reading counts (`%s %s: %d readings above %d kts`).
- **Pytest fails to import after a Python upgrade.** Recreate the venv:
  `rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`.

## Future improvements

- **Drop Model 1.** Per the bigwavedave site owner, Model 1's data is read off
  output plots (blocky, occasional spikes from interpretation noise) while
  Model 2's is direct. Eventually `USE_MODEL_2_ONLY` should become unconditional.
- **Multi-site alerts.** `SITES` has both Jericho and English Bay, but `main()`
  only runs Jericho. A small loop or a config-driven entry point would extend
  coverage.
- **State persistence between runs.** Currently every cron emits its own
  notification even if conditions haven't changed. Storing the last alert
  fingerprint (e.g. in an Actions cache, a gist, or a small commit) would
  let us avoid duplicate pages.
- **Direction- and gust-aware analysis.** The upstream observation feed
  includes gust and direction. A direction-conditional threshold (e.g. only
  alert on W/SW winds, which are kiteable at Jericho) would cut false
  positives. Gusts could feed a separate `WIND_GUST_THRESHOLD_KTS` rule.
- **Single-source notification.** Once Model 1 is gone, the "via N" tag can
  also disappear from the alert body.
