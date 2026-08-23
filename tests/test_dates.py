"""Timezone rules: API timestamps are Swedish wall-clock; 'now' comparisons are aware."""

from datetime import UTC, date, datetime, timedelta

import pytest

from sj_api_client.booking import _segment_to_display_row, booking_date_range
from sj_api_client.cli import _is_expired, validate_dates_against_pass
from sj_api_client.dates import (
    SWEDEN,
    booking_dates,
    normalise_date_selection,
    parse_api_datetime,
    parse_date_selection,
    selected_dates,
    sweden_now,
    to_sweden,
)
from sj_api_client.errors import SJConfigError
from sj_api_client.output import _days_remaining, _format_tp_date
from tests.fakes import base_cfg


def test_parse_api_datetime_offset_z_and_naive():
    aware = parse_api_datetime("2026-09-01T06:59:00+02:00")
    assert aware.utcoffset() == timedelta(hours=2)
    assert parse_api_datetime("2026-09-01T04:59:00Z") == aware
    naive = parse_api_datetime("2026-09-01T06:59:00")  # assumed Swedish local
    assert naive == aware and naive.tzinfo is SWEDEN


def test_to_sweden_converts_wall_clock():
    # 22:30Z on the 31st is 00:30 on the 1st in Sweden (CEST)
    assert to_sweden("2026-08-31T22:30:00Z").strftime("%Y-%m-%d %H:%M") == "2026-09-01 00:30"
    # winter: CET
    assert to_sweden("2026-12-24T23:30:00Z").strftime("%Y-%m-%d %H:%M") == "2026-12-25 00:30"
    assert sweden_now().tzinfo is SWEDEN


def test_segment_row_past_detection_is_machine_tz_independent():
    seg = {
        "direction": "OUTBOUND",
        "departureDateTime": "2026-09-01T06:59:00+02:00",
        "arrivalDateTime": "2026-09-01T11:36:00+02:00",
    }
    # 'now' given in UTC: 05:00Z == 07:00 Swedish → departure is in the past
    row = _segment_to_display_row(seg, "N", datetime(2026, 9, 1, 5, 0, tzinfo=UTC))
    assert row["past"] == "Y"
    assert (row["date"], row["departure"], row["arrival"]) == ("2026-09-01", "06:59", "11:36")
    row = _segment_to_display_row(seg, "N", datetime(2026, 9, 1, 4, 0, tzinfo=UTC))
    assert row["past"] == "N"
    # a Z timestamp is shown in Swedish local time
    seg_z = dict(
        seg, departureDateTime="2026-09-01T04:59:00Z", arrivalDateTime="2026-09-01T09:36:00Z"
    )
    row = _segment_to_display_row(seg_z, "N", datetime(2026, 9, 1, 4, 0, tzinfo=UTC))
    assert (row["date"], row["departure"], row["arrival"]) == ("2026-09-01", "06:59", "11:36")
    # garbage does not crash
    row = _segment_to_display_row({"departureDateTime": "soon"}, "N", sweden_now())
    assert row["departure"] == "soon" and row["past"] == "N"


def test_format_tp_date_and_days_remaining():
    assert _format_tp_date("2026-03-18T01:00:00+01:00") == "2026-03-18"
    assert _format_tp_date("2026-03-18T01:00:00+01:00", exclusive=True) == "2026-03-17"
    assert _format_tp_date("2026-03-17T23:30:00Z") == "2026-03-18"
    assert _format_tp_date("") == "—"
    assert _format_tp_date("nope") == "nope"
    future = (sweden_now() + timedelta(days=10, hours=1)).isoformat()
    assert _days_remaining(future) == "10"
    assert _days_remaining("2020-01-01T00:00:00+01:00") == "expired"
    assert _days_remaining(None) == "—"


def test_is_expired():
    assert _is_expired({"endTravelValidityDateTime": "2020-01-01T00:00:00+01:00"})
    assert not _is_expired(
        {"endTravelValidityDateTime": (sweden_now() + timedelta(days=1)).isoformat()}
    )
    assert not _is_expired({})
    assert not _is_expired({"endTravelValidityDateTime": "garbage"})


def test_booking_date_range_uses_swedish_wall_clock():
    _, end = booking_date_range({"endTravelValidityDateTime": "2027-03-17T23:30:00Z"})
    assert end == "2027-03-19"  # 00:30 on the 18th in Sweden, +1 day


@pytest.mark.parametrize(
    ("start", "end", "first_day", "last_day"),
    [
        # real API shape: midnight UTC rendered in the Swedish offset; the end
        # instant is exclusive (the day after the last valid day)
        ("2025-03-17T01:00:00+01:00", "2026-03-18T01:00:00+01:00", "2025-03-17", "2026-03-17"),
        ("2026-09-01T02:00:00+02:00", "2027-09-02T02:00:00+02:00", "2026-09-01", "2027-09-01"),
    ],
    ids=["winter-cet", "summer-cest"],
)
def test_validate_dates_against_pass_boundaries(start, end, first_day, last_day):
    from datetime import date, timedelta

    tp = {"startTravelValidityDateTime": start, "endTravelValidityDateTime": end}
    day_before = (date.fromisoformat(first_day) - timedelta(days=1)).isoformat()
    day_after = (date.fromisoformat(last_day) + timedelta(days=1)).isoformat()
    # today pinned before the window and every day bookable: this test is
    # about the boundaries. The two boundary days are selected on their own
    # (a range over a year-long pass would exceed the grammar's year limit).
    today = date.fromisoformat(day_before)
    every_day = {"skip_weekends": False, "skip_holidays": False}
    validate_dates_against_pass(base_cfg(dates=f"{first_day}, {last_day}", **every_day), tp, today)
    with pytest.raises(SystemExit):
        validate_dates_against_pass(
            base_cfg(dates=f"{day_before}, {last_day}", **every_day), tp, today
        )
    with pytest.raises(SystemExit):
        validate_dates_against_pass(
            base_cfg(dates=f"{first_day}, {day_after}", **every_day), tp, today
        )
    validate_dates_against_pass(base_cfg(), {})  # no validity info → no check


# --- date selection grammar (dates key, --cancel-date) ----------------------


def _week(year, week):
    monday = date.fromisocalendar(year, week, 1)
    return [monday + timedelta(days=i) for i in range(7)]


TODAY = date(2026, 8, 23)  # ISO year 2026, week 34


def test_selection_dates_lists_and_ranges():
    assert parse_date_selection("2026-09-16", today=TODAY) == ([date(2026, 9, 16)], [])
    dates, errors = parse_date_selection("2026-09-18, 2026-09-16,2026-09-16", today=TODAY)
    assert errors == [] and dates == [date(2026, 9, 16), date(2026, 9, 18)]
    dates, errors = parse_date_selection(
        "2026-09-21,2026-09-16..2026-09-17,2026-09-21..2026-09-21", today=TODAY
    )
    assert errors == [] and dates == [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 21)]


def test_selection_week_terms_and_range_inheritance():
    w43 = _week(2026, 43)
    assert parse_date_selection("W43", today=TODAY) == (w43, [])
    assert parse_date_selection("w43", today=TODAY) == (w43, [])  # case-insensitive W
    assert parse_date_selection("2026-W43", today=TODAY) == (w43, [])
    assert parse_date_selection("2026-W43", today=TODAY)[0][0] == date(2026, 10, 19)
    assert parse_date_selection("W43..W43", today=TODAY) == (w43, [])  # single-week range
    two_weeks = _week(2026, 43) + _week(2026, 44)
    for spec in ("W43..44", "W43..W44", "2026-W43..44", "2026-W43..2026-W44", "W44, W43"):
        assert parse_date_selection(spec, today=TODAY) == (two_weeks, []), spec
    # a year seam: today in ISO week 2026-W01, the range's own start decides the end's year
    assert parse_date_selection("W1..2", today=date(2025, 12, 29)) == (
        [*_week(2026, 1), *_week(2026, 2)],
        [],
    )
    # mixed term kinds in one selection are fine; only a single range may not mix
    mixed, errors = parse_date_selection("2026-10-28..2026-10-29, W43", today=TODAY)
    assert errors == [] and mixed == [
        *w43,
        datetime(2026, 10, 28).date(),
        datetime(2026, 10, 29).date(),
    ]


def test_spaces_around_a_range_are_accepted():
    assert parse_date_selection("2026-09-16 .. 2026-09-17", today=TODAY) == (
        [date(2026, 9, 16), date(2026, 9, 17)],
        [],
    )
    assert parse_date_selection("W43 .. 44", today=TODAY) == (
        [*_week(2026, 43), *_week(2026, 44)],
        [],
    )
    assert parse_date_selection("2026-09-16 .. ", today=TODAY) == (
        [],
        ["'2026-09-16 ..' must be a single range start..end"],
    )


def test_bare_week_uses_todays_iso_year():
    # 29 dec 2025 already belongs to ISO week 2026-W01
    assert parse_date_selection("W1", today=date(2025, 12, 29)) == (_week(2026, 1), [])
    assert parse_date_selection("2027-W02..03", today=TODAY)[0][0] == date(2027, 1, 11)


def test_week_53_only_in_years_that_have_it():
    assert parse_date_selection("2026-W53", today=TODAY) == (_week(2026, 53), [])
    assert parse_date_selection("2027-W53", today=TODAY) == ([], ["2027 has no week 53"])


def test_selection_errors_are_all_collected():
    dates, errors = parse_date_selection(
        "2026-09-31, foo, W54, 2026-10-19..W43, W43..2026-10-25, W44..43, "
        "2026-01-01..2027-01-01, 2026-W01..2027-W10, ,2026-09-16..2026-09-17..2026-09-18, 46, "
        "9999-W52, ..2026-09-01",
        today=TODAY,
    )
    assert dates == []
    assert errors == [
        "'2026-09-31' is not a real calendar date",
        "'foo' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)",
        "week 54 is out of range 1..53",
        "range '2026-10-19..W43' mixes a date and a week",
        "range 'W43..2026-10-25' mixes a date and a week",
        "range 'W44..43' must run forwards (start before end)",
        "range '2026-01-01..2027-01-01' spans more than a year",
        "range '2026-W01..2027-W10' spans more than a year",
        "empty entry in the date list",
        "'2026-09-16..2026-09-17..2026-09-18' must be a single range start..end",
        "'46' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)",
        "9999 has no week 52",
        "'..2026-09-01' must be a single range start..end",
    ]
    # a leap day inside a year-long range is still "a year"
    assert parse_date_selection("2028-01-01..2028-12-31", today=TODAY)[1] == []
    # the calendar's last year has no next year: the span check is skipped, not failed
    assert parse_date_selection("9999-12-01..9999-12-31", today=TODAY)[1] == []
    # a range end with a year but no W is not a recognised week shape
    assert parse_date_selection("W43..2026-46", today=TODAY)[1] == [
        "'2026-46' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)"
    ]


def test_normalise_date_selection():
    assert normalise_date_selection(" w43 ,W45..46 ") == "W43, W45..46"
    assert normalise_date_selection("2026-09-01..2026-10-30") == "2026-09-01..2026-10-30"


def test_selected_and_booking_dates():
    p = {"dates": "2026-08-20..2026-08-25"}
    assert selected_dates(p, TODAY)[0] == date(2026, 8, 20)
    assert booking_dates(p, TODAY) == [date(2026, 8, 23), date(2026, 8, 24), date(2026, 8, 25)]
    assert booking_dates({"dates": "W43"}, TODAY) == _week(2026, 43)
    assert booking_dates({"dates": "2099-01-01"}) == [date(2099, 1, 1)]  # default today
    with pytest.raises(SJConfigError, match="dates: 'nope' is not a date"):
        selected_dates({"dates": "nope"}, TODAY)
