"""
Tests for handle_upgrade_class: a read-only preview of fallback-class legs.

Every test asserts nothing was written (no provisional created, checked out,
cancelled or seat-updated) — the whole point of this half of the feature is
that it only searches (without the travel pass) and reads offers.
"""

from datetime import timedelta

import pytest

from sj_cli.booking import handle_upgrade_class
from sj_cli.dates import sweden_now
from tests.fakes import FakeClient, base_cfg, dep, offers

# Computed from the real clock at test-run time so these are never
# accidentally in the past (see tests/test_change_seat.py for the same idiom).
FUTURE_DATE = (sweden_now() + timedelta(days=30)).date().isoformat()
FUTURE_DATE_2 = (sweden_now() + timedelta(days=31)).date().isoformat()
PAST_DATE = (sweden_now() - timedelta(days=2)).date().isoformat()

ORIGIN, ORIGIN_ID = "Göteborg Central", "740000002"
DEST, DEST_ID = "Stockholm Central", "740000001"

_WRITE_TAGS = {"create", "add", "customer", "checkout", "seats"}


def _segment(date, dep_time="06:00", train="520", comfort_code="SECOND"):
    """A booked segment on the configured route, in comfort_code, at dep_time."""
    return {
        "direction": "OUTBOUND",
        "departureDateTime": f"{date}T{dep_time}:00+02:00",
        "arrivalDateTime": f"{date}T{dep_time[:2]}:59:00+02:00",
        "departureStation": {"uicStationCode": ORIGIN_ID, "name": ORIGIN},
        "arrivalStation": {"uicStationCode": DEST_ID, "name": DEST},
        "publicServiceName": train,
        "serviceName": train,
        "serviceBrandNameDescription": "X 2000",
        "productFamily": {"salesCategoryComfort": comfort_code},
    }


def _booking(number, date, **seg_kwargs):
    """One active booking, one journey, one segment."""
    return {
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED",
            "journeys": [{"segments": [_segment(date, **seg_kwargs)]}],
        }
    }


def _key(date):
    """The FakeClient search-id key for a pass-free one-way search on this route/date."""
    return f"{ORIGIN}->{DEST}@{date}"


def _assert_no_writes(c):
    assert not any(call[0] in _WRITE_TAGS for call in c.calls), c.calls


def test_fallback_leg_with_purchasable_seats_is_worth_trying(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=345, second_price=0)  # calm sold, paid

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    out = capsys.readouterr().out
    assert "holds 2 class" in out
    assert "seats exist" in out
    assert "1 leg(s) not in 2 class calm" in out and "1 worth trying" in out
    _assert_no_writes(c)


def test_fallback_leg_with_no_seats_is_not_possible(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=None, second_price=0)  # calm not sold at all

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    out = capsys.readouterr().out
    assert "no seats on this departure" in out
    assert "seats exist" not in out
    assert "1 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


def test_leg_already_in_configured_class_is_not_reported(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND_CALM")]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # never probed: nothing to upgrade
    out = capsys.readouterr().out
    assert "NUM1" not in out  # no card at all
    assert "0 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


def test_probe_uses_pass_free_search_not_the_travel_pass():
    # The whole point of this feature: a search WITH the travel pass reports
    # every class unavailable on a departure the account already holds, so
    # the probe must never pass a travel_pass_id.
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=0, second_price=0)

    cfg = base_cfg(comfort_class="2 class calm")
    handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert c.search_tp_ids == [None]
    search_calls = [call for call in c.calls if call[0] == "search"]
    assert search_calls == [("search", ORIGIN, DEST, FUTURE_DATE, None)]


def test_probe_matches_the_booked_departure_not_the_closest_to_time_leave(capsys):
    # Two departures that day; time_leave (09:00) is much closer to the decoy
    # (08:55, a different train) than to the one actually booked (06:00). A
    # probe that picked "closest to time_leave" instead of the booked
    # departure would read the decoy's offers and wrongly call this worth
    # trying.
    c = FakeClient()
    c.bookings_list = [
        _booking("NUM1", FUTURE_DATE, dep_time="06:00", train="520", comfort_code="SECOND")
    ]
    c.departures[_key(FUTURE_DATE)] = [
        dep("520", FUTURE_DATE, "06:00", "07:46"),  # the actual booked departure
        dep("999", FUTURE_DATE, "08:55", "10:41"),  # closer to time_leave, a different train
    ]
    c.offers_by_dep["520"] = offers(calm_price=None, second_price=0)  # booked one: no seats
    c.offers_by_dep["999"] = offers(calm_price=430, second_price=0)  # decoy: seats exist

    cfg = base_cfg(comfort_class="2 class calm", time_leave="09:00")
    handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    offer_calls = [call for call in c.calls if call[0] == "offers"]
    assert offer_calls == [("offers", "520")]
    out = capsys.readouterr().out
    assert "no seats on this departure" in out
    assert "seats exist" not in out


def test_past_leg_is_skipped(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", PAST_DATE, comfort_code="SECOND")]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[PAST_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # never probed: already departed
    out = capsys.readouterr().out
    assert "no bookings found for the given dates" in out
    _assert_no_writes(c)


def test_leg_on_a_date_outside_the_given_range_is_skipped(capsys):
    # One booking, two travel days: FUTURE_DATE (in range, already the
    # configured class) and FUTURE_DATE_2 (out of range, a fallback class
    # that would print a card and get probed if the day scoping leaked it in).
    booking = {
        "booking": {
            "bookingNumber": "NUM1",
            "bookingId": "ID-NUM1",
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {"segments": [_segment(FUTURE_DATE, train="520", comfort_code="SECOND_CALM")]},
                {"segments": [_segment(FUTURE_DATE_2, train="777", comfort_code="SECOND")]},
            ],
        }
    }
    c = FakeClient()
    c.bookings_list = [booking]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # neither leg needed a probe
    out = capsys.readouterr().out
    assert "777" not in out
    assert "0 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


def test_upgrade_class_refuses_to_run_without_dry_run(capsys):
    c = FakeClient()
    cfg = base_cfg(comfort_class="2 class calm")

    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=False)

    assert ok is False
    assert c.calls == []
    out = capsys.readouterr().out
    assert "--dry-run" in out


@pytest.mark.parametrize("dates", [[], ["2020-01-01"]])  # empty selection / date well in the past
def test_nothing_matched_is_reported_red_but_not_a_failure(capsys, dates):
    c = FakeClient()
    cfg = base_cfg(comfort_class="2 class calm")

    ok = handle_upgrade_class(c, "tok", cfg, dates=dates, dry_run=True)

    assert ok is True
    if dates:
        out = capsys.readouterr().out
        assert "no bookings found for the given dates" in out
    _assert_no_writes(c)
