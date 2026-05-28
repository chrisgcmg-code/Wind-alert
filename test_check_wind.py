"""Tests for check_wind."""

import datetime as dt

import check_wind
from check_wind import (
    MODEL_1_NAME,
    MODEL_2_NAME,
    WIND_SUSTAINED_TIME_MIN_HOURS,
    WIND_THRESHOLD_KTS,
    DayResult,
    Interval,
    WindReading,
    analyze_day,
    build_message,
    choose_intervals,
    combine_intervals,
    extract_above_threshold,
    filter_sustained,
    fmt_models,
    merge_consecutive_intervals,
    parse_readings,
    split_models,
)


# --- parse_readings -----------------------------------------------------------

def test_parse_readings_empty():
    assert parse_readings([]) == []


def test_parse_readings_skips_blank_lines():
    rows = ["", "2026-05-27 10:00:00\t5\tW", ""]
    assert parse_readings(rows) == [WindReading(dt.datetime(2026, 5, 27, 10), 5)]


def test_parse_readings_ignores_extra_columns():
    # Some upstream rows have direction or other trailing fields; *_ catches them.
    row = "2026-05-27 10:00:00\t7\tW\tGust=12\textra"
    assert parse_readings([row]) == [WindReading(dt.datetime(2026, 5, 27, 10), 7)]


# --- extract_above_threshold -------------------------------------------------

def test_extract_above_threshold_is_strict_greater():
    t = WIND_THRESHOLD_KTS
    base = dt.datetime(2026, 5, 27, 10)
    readings = [
        WindReading(base, t),         # at threshold — excluded
        WindReading(base, t + 1),     # above — included
        WindReading(base, t - 1),     # below — excluded
    ]
    above = extract_above_threshold(readings)
    assert [r.wind_kts for r in above] == [t + 1]


def test_extract_above_threshold_empty():
    assert extract_above_threshold([]) == []


# --- merge_consecutive_intervals ---------------------------------------------

def _r(hour: int, kts: int) -> WindReading:
    return WindReading(dt.datetime(2026, 5, 27, hour), kts)


def test_merge_empty():
    assert merge_consecutive_intervals([]) == []


def test_merge_single_reading_is_zero_duration_interval():
    r = _r(10, 8)
    assert merge_consecutive_intervals([r]) == [Interval(r.timestamp, r.timestamp)]


def test_merge_collapses_consecutive_hours():
    readings = [_r(10, 6), _r(11, 8), _r(12, 7)]
    merged = merge_consecutive_intervals(readings)
    assert merged == [Interval(readings[0].timestamp, readings[-1].timestamp)]


def test_merge_splits_on_non_consecutive_gap():
    readings = [_r(10, 6), _r(13, 7)]  # 3-hour gap
    merged = merge_consecutive_intervals(readings)
    assert merged == [
        Interval(readings[0].timestamp, readings[0].timestamp),
        Interval(readings[1].timestamp, readings[1].timestamp),
    ]


def test_merge_multiple_stretches():
    readings = [_r(8, 6), _r(9, 7), _r(12, 8), _r(13, 9)]
    merged = merge_consecutive_intervals(readings)
    assert merged == [
        Interval(readings[0].timestamp, readings[1].timestamp),
        Interval(readings[2].timestamp, readings[3].timestamp),
    ]


# --- combine_intervals -------------------------------------------------------

NO_RAW = {MODEL_1_NAME: [], MODEL_2_NAME: []}
ONLY_M1_RAW = {MODEL_1_NAME: ["x"], MODEL_2_NAME: []}
ONLY_M2_RAW = {MODEL_1_NAME: [], MODEL_2_NAME: ["x"]}
BOTH_RAW = {MODEL_1_NAME: ["x"], MODEL_2_NAME: ["x"]}


def test_combine_neither_model_has_data():
    used, intervals = combine_intervals({MODEL_1_NAME: [], MODEL_2_NAME: []}, NO_RAW)
    assert used == []
    assert intervals == []


def test_combine_only_model_1_has_raw_data_uses_m1():
    iv = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 12))
    used, intervals = combine_intervals({MODEL_1_NAME: [iv], MODEL_2_NAME: []}, ONLY_M1_RAW)
    assert used == [MODEL_1_NAME]
    assert intervals == [iv]


def test_combine_only_model_2_has_raw_data_uses_m2():
    iv = Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))
    used, intervals = combine_intervals({MODEL_1_NAME: [], MODEL_2_NAME: [iv]}, ONLY_M2_RAW)
    assert used == [MODEL_2_NAME]
    assert intervals == [iv]


def test_combine_both_models_intersects():
    iv1 = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 17))
    iv2 = Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))
    used, intervals = combine_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: [iv2]}, BOTH_RAW
    )
    assert used == [MODEL_1_NAME, MODEL_2_NAME]
    assert intervals == [Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))]


def test_combine_both_models_no_intersection():
    iv1 = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 10))
    iv2 = Interval(dt.datetime(2026, 5, 27, 14), dt.datetime(2026, 5, 27, 16))
    used, intervals = combine_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: [iv2]}, BOTH_RAW
    )
    assert used == [MODEL_1_NAME, MODEL_2_NAME]
    assert intervals == []


def test_combine_both_have_raw_but_only_m1_above_threshold_suppresses_alert():
    # The regression case: Model 1 predicts wind, Model 2 disagrees (has data
    # but nothing above threshold). Should NOT alert on Model 1 alone.
    iv1 = Interval(dt.datetime(2026, 5, 28, 9), dt.datetime(2026, 5, 28, 12))
    used, intervals = combine_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: []}, BOTH_RAW
    )
    assert used == [MODEL_1_NAME, MODEL_2_NAME]
    assert intervals == []


def test_combine_both_have_raw_but_only_m2_above_threshold_suppresses_alert():
    iv2 = Interval(dt.datetime(2026, 5, 28, 9), dt.datetime(2026, 5, 28, 12))
    used, intervals = combine_intervals(
        {MODEL_1_NAME: [], MODEL_2_NAME: [iv2]}, BOTH_RAW
    )
    assert used == [MODEL_1_NAME, MODEL_2_NAME]
    assert intervals == []


# --- choose_intervals (strategy router) -------------------------------------

def test_choose_intervals_use_model_2_only_on(monkeypatch):
    monkeypatch.setattr(check_wind, "USE_MODEL_2_ONLY", True)
    iv1 = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 17))
    iv2 = Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))
    used, intervals = choose_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: [iv2]},
        {MODEL_1_NAME: ["x"], MODEL_2_NAME: ["x"]},
    )
    assert used == [MODEL_2_NAME]
    assert intervals == [iv2]  # Model 2 only, no intersection math


def test_choose_intervals_use_model_2_only_falls_back_when_no_m2_raw(monkeypatch):
    monkeypatch.setattr(check_wind, "USE_MODEL_2_ONLY", True)
    iv1 = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 12))
    used, intervals = choose_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: []},
        {MODEL_1_NAME: ["x"], MODEL_2_NAME: []},  # no Model 2 raw data
    )
    assert used == [MODEL_1_NAME]
    assert intervals == [iv1]


def test_choose_intervals_off_uses_combine(monkeypatch):
    monkeypatch.setattr(check_wind, "USE_MODEL_2_ONLY", False)
    iv1 = Interval(dt.datetime(2026, 5, 27, 8), dt.datetime(2026, 5, 27, 17))
    iv2 = Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))
    used, intervals = choose_intervals(
        {MODEL_1_NAME: [iv1], MODEL_2_NAME: [iv2]},
        {MODEL_1_NAME: ["x"], MODEL_2_NAME: ["x"]},
    )
    assert used == [MODEL_1_NAME, MODEL_2_NAME]
    assert len(intervals) == 1  # intersection


# --- filter_sustained --------------------------------------------------------

def test_filter_drops_intervals_shorter_than_minimum():
    # Filter keeps intervals where delta >= WIND_SUSTAINED_TIME_MIN_HOURS.
    short_delta = dt.timedelta(hours=WIND_SUSTAINED_TIME_MIN_HOURS - 1)
    start = dt.datetime(2026, 5, 27, 8)
    short = Interval(start, start + short_delta)
    assert filter_sustained([short]) == []


def test_filter_keeps_intervals_at_or_above_minimum():
    long_delta = dt.timedelta(hours=WIND_SUSTAINED_TIME_MIN_HOURS)
    start = dt.datetime(2026, 5, 27, 8)
    long_iv = Interval(start, start + long_delta)
    assert filter_sustained([long_iv]) == [long_iv]


# --- split_models ------------------------------------------------------------

def test_split_models_empty_input():
    assert split_models("") == {MODEL_1_NAME: [], MODEL_2_NAME: []}


def test_split_models_both_markers_present():
    raw = (
        "header line\n"
        "-9999\n"
        "2026-05-27 10:00:00\t5\tW\n"
        "2026-05-27 11:00:00\t7\tW\n"
        "-9998\n"
        "2026-05-27 06:00:00\t10\tN\n"
    )
    result = split_models(raw)
    assert any("10:00:00\t5\tW" in line for line in result[MODEL_2_NAME])
    assert any("11:00:00\t7\tW" in line for line in result[MODEL_2_NAME])
    assert any("06:00:00\t10\tN" in line for line in result[MODEL_1_NAME])
    # Model 1 markers should not leak into Model 2 section.
    assert not any("06:00:00\t10\tN" in line for line in result[MODEL_2_NAME])


def test_split_models_only_model_2_marker():
    raw = "header\n-9999\n2026-05-27 10:00:00\t5\tW\n"
    result = split_models(raw)
    assert result[MODEL_1_NAME] == []
    assert any("10:00:00\t5\tW" in line for line in result[MODEL_2_NAME])


# --- fmt_models --------------------------------------------------------------

def test_fmt_models_single():
    assert fmt_models(["model2"]) == "2"


def test_fmt_models_multiple_preserves_order():
    assert fmt_models(["model1", "model2"]) == "1, 2"


def test_fmt_models_empty():
    assert fmt_models([]) == ""


# --- build_message -----------------------------------------------------------

def test_build_message_no_data_day():
    results = [DayResult(date=dt.date(2026, 5, 27), models_used=[], intervals=None)]
    msg = build_message(results, "jericho")
    assert "Wed May 27: no forecast data available" in msg
    assert msg.endswith("https://bigwavedave.ca/jerichobch.html?site=20")
    assert f"Threshold: >{WIND_THRESHOLD_KTS}kt for {WIND_SUSTAINED_TIME_MIN_HOURS}+ hours" in msg


def test_build_message_calm_day_uses_via_header_and_bullet():
    results = [DayResult(
        date=dt.date(2026, 5, 27),
        models_used=[MODEL_1_NAME, MODEL_2_NAME],
        intervals=[],
    )]
    msg = build_message(results, "jericho")
    assert "Wed May 27 (via 1, 2):" in msg
    assert "> no sustained wind" in msg


def test_build_message_single_same_day_interval():
    results = [DayResult(
        date=dt.date(2026, 5, 27),
        models_used=[MODEL_2_NAME],
        intervals=[Interval(dt.datetime(2026, 5, 27, 11), dt.datetime(2026, 5, 27, 15))],
    )]
    msg = build_message(results, "jericho")
    assert "Wed May 27 (via 2):" in msg
    assert "> 11:00 AM - 3:00 PM" in msg


def test_build_message_multi_interval_day():
    results = [DayResult(
        date=dt.date(2026, 5, 27),
        models_used=[MODEL_1_NAME, MODEL_2_NAME],
        intervals=[
            Interval(dt.datetime(2026, 5, 27, 6), dt.datetime(2026, 5, 27, 8)),
            Interval(dt.datetime(2026, 5, 27, 10), dt.datetime(2026, 5, 27, 17)),
        ],
    )]
    msg = build_message(results, "jericho")
    assert "> 6:00 AM - 8:00 AM" in msg
    assert "> 10:00 AM - 5:00 PM" in msg


def test_build_message_cross_day_interval_uses_full_datetime():
    results = [DayResult(
        date=dt.date(2026, 5, 27),
        models_used=[MODEL_2_NAME],
        intervals=[Interval(dt.datetime(2026, 5, 27, 23), dt.datetime(2026, 5, 28, 2))],
    )]
    msg = build_message(results, "jericho")
    # When start.date() != end.date(), both ends print as full datetimes.
    assert "Wed May 27 11:00 PM" in msg
    assert "Thu May 28 2:00 AM" in msg


# --- analyze_day (integration with mocked fetch) -----------------------------

def test_analyze_day_no_data_returns_intervals_none(monkeypatch):
    monkeypatch.setattr(check_wind, "fetch_wind_data", lambda *_: "")
    result = analyze_day(dt.date(2026, 5, 27), "jericho")
    assert result.date == dt.date(2026, 5, 27)
    assert result.models_used == []
    assert result.intervals is None


def test_analyze_day_intersection_pipeline(monkeypatch):
    """Both models predict wind that overlaps and meets the sustained threshold."""
    monkeypatch.setattr(check_wind, "USE_MODEL_2_ONLY", False)
    t = WIND_THRESHOLD_KTS
    fake_raw = (
        "header\n"
        "-9999\n"  # Model 2 above-threshold 10:00-12:00
        f"2026-05-27 09:00:00\t{t - 2}\tW\n"
        f"2026-05-27 10:00:00\t{t + 2}\tW\n"
        f"2026-05-27 11:00:00\t{t + 2}\tW\n"
        f"2026-05-27 12:00:00\t{t + 2}\tW\n"
        f"2026-05-27 13:00:00\t{t - 2}\tW\n"
        "-9998\n"  # Model 1 above-threshold 09:00-12:00
        f"2026-05-27 09:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 10:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 11:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 12:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 13:00:00\t{t - 5}\tNW\n"
    )
    monkeypatch.setattr(check_wind, "fetch_wind_data", lambda *_: fake_raw)

    result = analyze_day(dt.date(2026, 5, 27), "jericho")

    assert result.date == dt.date(2026, 5, 27)
    assert result.models_used == [MODEL_1_NAME, MODEL_2_NAME]
    # Intersection: max(09:00, 10:00) -> min(12:00, 12:00) = (10:00, 12:00). 2h passes sustained.
    assert result.intervals == [Interval(
        dt.datetime(2026, 5, 27, 10), dt.datetime(2026, 5, 27, 12),
    )]


def test_analyze_day_use_model_2_only_ignores_model_1_wind(monkeypatch):
    """With strategy on, a Model-1-only wind window is suppressed."""
    monkeypatch.setattr(check_wind, "USE_MODEL_2_ONLY", True)
    t = WIND_THRESHOLD_KTS
    fake_raw = (
        "-9999\n"  # Model 2 has data but nothing above threshold
        f"2026-05-27 10:00:00\t{t - 5}\tW\n"
        f"2026-05-27 11:00:00\t{t - 5}\tW\n"
        "-9998\n"  # Model 1 predicts wind 10:00-12:00
        f"2026-05-27 10:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 11:00:00\t{t + 5}\tNW\n"
        f"2026-05-27 12:00:00\t{t + 5}\tNW\n"
    )
    monkeypatch.setattr(check_wind, "fetch_wind_data", lambda *_: fake_raw)

    result = analyze_day(dt.date(2026, 5, 27), "jericho")

    # Model 2 has data, so it's used alone. It has no sustained wind, so no alert.
    assert result.models_used == [MODEL_2_NAME]
    assert result.intervals == []


