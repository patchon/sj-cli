"""Timezone rules: API timestamps are Swedish wall-clock; 'now' comparisons are aware."""

from datetime import UTC, datetime, timedelta

import pytest

from sj_api_client.booking import _segment_to_display_row, booking_date_range
from sj_api_client.cli import _is_expired, validate_dates_against_pass
from sj_api_client.dates import SWEDEN, parse_api_datetime, sweden_now, to_sweden
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
    validate_dates_against_pass(base_cfg(date_start=first_day, date_end=last_day), tp)
    with pytest.raises(SystemExit):
        validate_dates_against_pass(base_cfg(date_start=day_before, date_end=last_day), tp)
    with pytest.raises(SystemExit):
        validate_dates_against_pass(base_cfg(date_start=first_day, date_end=day_after), tp)
    validate_dates_against_pass(base_cfg(), {})  # no validity info → no check
