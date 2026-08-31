"""Tests for handle_change_seat: re-seating existing bookings by date or booking number."""

from datetime import timedelta

import pytest

from sj_cli import booking
from sj_cli.booking import handle_change_seat
from sj_cli.dates import to_sweden
from tests.fakes import FakeClient, base_cfg, seatmap

# One fixed instant the whole file shares: the dates below are relative to it,
# so a "future" booking is never accidentally in the past (a past one is
# reported "already departed" and every test would pass vacuously), and the
# autouse fixture makes the code read the same clock — so the card times stay
# put whatever the real date is, in summer time like the fixtures' +02:00.
NOW = to_sweden("2026-09-15T12:00:00+02:00")
FUTURE_DATE = (NOW + timedelta(days=30)).date().isoformat()
FUTURE_DATE_2 = (NOW + timedelta(days=31)).date().isoformat()
PAST_DATE = (NOW - timedelta(days=2)).date().isoformat()


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """The departed check reads sweden_now(); freeze it where the fixtures live."""
    monkeypatch.setattr(booking, "sweden_now", lambda: NOW)


def _segment(date, tag, dep_time, origin="740000002"):
    return {
        "direction": "OUTBOUND",
        "serviceIdentifier": f"SI-{tag}",
        "seatMapAvailable": True,
        "seatMapSearchId": f"SM-{tag}",
        "departureDateTime": f"{date}T{dep_time}:00+02:00",
        "arrivalDateTime": f"{date}T{dep_time[:2]}:59:00+02:00",
        "departureStation": {"uicStationCode": origin},
        "arrivalStation": {"uicStationCode": "740000001"},
        "requiredProducts": [{"seat": {"number": "39", "carriageNumber": "3"}}],
    }


def two_day_booking(number="NUM1"):
    """One booking, two travel days — the shape --change-seat-date must scope to."""
    return {
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {"segments": [_segment(FUTURE_DATE, "D1", "06:00")]},
                {"segments": [_segment(FUTURE_DATE_2, "D2", "09:00")]},
            ],
        }
    }


def booking_item(number="NUM1", date=FUTURE_DATE, direction="OUTBOUND", origin="740000002"):
    return {
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {
                    "segments": [
                        {
                            "direction": direction,
                            "serviceIdentifier": f"SI-{direction}",
                            "seatMapAvailable": True,
                            "seatMapSearchId": f"SM-{direction}",
                            "departureDateTime": f"{date}T06:00:00+02:00",
                            "arrivalDateTime": f"{date}T07:46:00+02:00",
                            "departureStation": {"uicStationCode": origin},
                            "arrivalStation": {"uicStationCode": "740000001"},
                            "requiredProducts": [{"seat": {"number": "39", "carriageNumber": "3"}}],
                        }
                    ]
                }
            ],
        }
    }


def a_known_current_seat(current_codes, free, number="39", reversed_=False):
    """
    A seat map whose assigned seat can be found in the carriage layout.

    fakes.seatmap() makes every layout seat selectable and its default
    assigned seat (3/39) is not in the layout at all — the shape where
    seats.assigned_seat() gives up and the best free seat is taken. The keep
    rule needs the other shape: an assigned seat the layout knows, so it can
    be ranked against what is free. So the assigned seat is put in the
    carriage and then taken back out of seatsPossibleToSelect (a seat you
    already hold is not one you can move to).
    """
    m = seatmap(free=((number, current_codes, reversed_), *free), assigned=("3", number))
    m["seatsPossibleToSelect"]["3"] = [spec[0] for spec in free]
    return m


def test_change_seat_by_date_patches_the_confirmed_endpoint():
    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window", "table"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True
    _, updates, provisional = c.seat_updates[0]
    assert provisional is False
    assert updates[0]["seatNumber"] == "70"
    assert updates[0]["serviceIdentifier"] == "SI-OUTBOUND"


def test_change_seat_by_date_ignores_other_routes():
    c = FakeClient()
    c.bookings_list = [booking_item(origin="740000009")]  # not the configured route
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True
    assert not c.seat_updates


def test_change_seat_by_booking_number_ignores_the_route():
    c = FakeClient()
    c.bookings_list = [booking_item(origin="740000009")]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, booking_numbers=["NUM1"]) is True
    assert c.seat_updates


def test_change_seat_dry_run_sends_no_patch(capsys):
    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE], dry_run=True)
    assert not c.seat_updates
    out = capsys.readouterr().out
    assert "would take" in out
    assert "● dry run · 1 seat(s) would change" in out


def test_change_seat_reports_an_unknown_booking_number(capsys):
    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    handle_change_seat(c, "TOKEN", cfg, booking_numbers=["NOPE", "NUM1"])
    out = capsys.readouterr().out
    assert "NOPE" in out
    assert c.seat_updates  # NUM1 was still handled


def test_change_seat_skips_a_locked_seat_map():
    c = FakeClient()
    c.bookings_list = [booking_item()]
    c.seatmaps = {"SM-OUTBOUND": seatmap(can_change=False)}
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE])
    assert not c.seat_updates


def test_change_seat_skips_a_past_segment(capsys):
    c = FakeClient()
    c.bookings_list = [booking_item(date=PAST_DATE)]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[PAST_DATE]) is True
    out = capsys.readouterr().out
    assert "already departed" in out
    assert not c.seat_updates
    # No seat map should even be read for a segment we already know is past.
    assert not [call for call in c.calls if call[0] == "seatmap"]


def test_change_seat_by_date_touches_and_shows_only_that_day(capsys):
    """A booking spanning two days: the other day is neither patched nor printed."""
    c = FakeClient()
    c.bookings_list = [two_day_booking()]
    c.seatmaps = {"SM-D1": seatmap(), "SM-D2": seatmap()}
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True

    ids = [u["serviceIdentifier"] for _, updates, _ in c.seat_updates for u in updates]
    assert ids == ["SI-D1"]  # only the named day's segment was patched
    assert not [call for call in c.calls if call[0] == "seatmap" and call[2] == "SM-D2"]

    out = capsys.readouterr().out
    assert FUTURE_DATE_2 not in out
    assert "06:00" in out and "09:00" not in out  # the card shows one day, not the booking
    assert "carriage 3 seat 70" in out
    assert "● 1 seat(s) changed" in out


@pytest.mark.parametrize("preference", ["ask", "  ASK  "])
def test_change_seat_dry_run_in_ask_mode_reports_without_prompting(capsys, monkeypatch, preference):
    from sj_cli import booking

    monkeypatch.setattr(booking, "ask_optional", lambda _: pytest.fail("a dry run must not ask"))
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)

    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()
    # Any string is "ask": an unnormalised one must not be read as a wish list
    cfg["search_parameters"]["seat_preference"] = preference

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE], dry_run=True) is True
    assert not c.seat_updates
    out = capsys.readouterr().out
    assert "currently carriage 3 seat 39 · 1 free seat(s)" in out


def test_change_seat_by_booking_number_skips_a_cancelled_booking(capsys):
    c = FakeClient()
    item = booking_item()
    item["booking"]["bookingStatus"] = "CANCELLED"
    c.bookings_list = [item]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, booking_numbers=["NUM1"]) is False
    assert not c.seat_updates
    assert "no active booking found with number NUM1" in capsys.readouterr().out


def test_an_unresolved_booking_number_closes_with_one_status_line(capsys):
    c = FakeClient()
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, booking_numbers=["NOPE"]) is False
    out = capsys.readouterr().out
    assert out.count("●") == 1  # one closing status per operation
    assert "no active booking found with number NOPE" in out
    assert "no bookings matched" not in out


def test_change_seat_without_a_preference_fails_instead_of_prompting(capsys):
    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()  # no seat_preference at all

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is False
    assert not c.calls  # nothing was even fetched
    assert "● no seat_preference in [search_parameters]" in capsys.readouterr().out


def test_a_seat_the_api_kept_is_not_counted_as_changed(capsys, monkeypatch):
    """The closing ● must not claim a change the booking contradicts."""
    c = FakeClient()
    c.bookings_list = [booking_item()]
    # the PATCH is accepted but the booking still shows the old seat
    monkeypatch.setattr(
        c,
        "update_seats",
        lambda _t, bid, updates, **_k: (
            c.seat_updates.append((bid, updates, False))
            or {"bookingId": bid, "booking": c._listed_booking(bid)}
        ),
    )
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE])
    out = capsys.readouterr().out
    assert "! asked for carriage 3 seat 70, the booking says otherwise" in out
    assert "● nothing changed" in out
    assert "seat(s) changed" not in out


def test_an_unstated_can_change_seat_is_tried_anyway():
    """canChangeSeat null means "not stated", not "locked" (API-null rule)."""
    c = FakeClient()
    c.bookings_list = [booking_item()]
    c.seatmaps = {"SM-OUTBOUND": {**seatmap(), "canChangeSeat": None}}
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE])
    assert c.seat_updates


def test_a_standalone_return_booking_is_labelled_by_train_not_direction():
    """
    The API marks every standalone booking's only leg OUTBOUND, so a return
    trip booked on its own would otherwise print "outbound" over a
    Stockholm → Linköping card. Real case: booking K883DH2T, 2026-08-31.
    """
    from sj_cli.booking import _segment_label, _train_label

    segment = {
        "direction": "OUTBOUND",  # the API's word for it, even though it is the way home
        "publicServiceName": "543",
        "serviceBrandNameDescription": "X 2000",
    }
    assert _segment_label(segment, [segment]) == "outbound"
    assert _train_label(segment, [segment]) == "X 2000 543"


def test_the_train_label_falls_back_when_the_service_is_unnamed():
    from sj_cli.booking import _train_label

    segment = {"direction": "INBOUND"}
    assert _train_label(segment, [segment]) == "return"


# --- a seat is only changed when a free one is strictly better ----------------


def test_a_seat_no_free_one_outranks_is_kept(capsys):
    """The live bug: an avoid-table run traded a table-free seat for a table."""
    c = FakeClient()
    c.bookings_list = [booking_item()]
    c.seatmaps = {
        "SM-OUTBOUND": a_known_current_seat(["AISLE"], (("73", ["TABLE", "AISLE"], True),))
    }
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["avoid table"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True
    assert not c.seat_updates
    out = capsys.readouterr().out
    assert "keeping carriage 3 seat 39 ·" in out
    assert "nothing free outranks it" in out
    assert "● nothing changed" in out


def test_an_equally_good_free_seat_is_not_taken(capsys):
    """Ties keep: swapping two seats that meet the same wishes gains nothing."""
    c = FakeClient()
    c.bookings_list = [booking_item()]
    c.seatmaps = {"SM-OUTBOUND": a_known_current_seat(["WINDOW"], (("73", ["WINDOW"], False),))}
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True
    assert not c.seat_updates
    assert "keeping carriage 3 seat 39 ·" in capsys.readouterr().out


def test_a_current_seat_the_layout_does_not_know_is_still_replaced(capsys):
    """
    No locatable current seat, nothing to compare: today's behaviour stands.

    fakes.seatmap()'s default assigned seat is not in the carriage layout, so
    assigned_seat() returns None — even a table seat is taken under an
    avoid-table preference rather than guessing what the passenger holds.
    """
    c = FakeClient()
    c.bookings_list = [booking_item()]
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["avoid table"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE]) is True
    _, updates, _ = c.seat_updates[0]
    assert updates[0]["seatNumber"] == "70"
    assert "keeping" not in capsys.readouterr().out


def test_the_dry_run_closing_counts_only_the_legs_that_would_change(capsys):
    c = FakeClient()
    c.bookings_list = [two_day_booking()]
    c.seatmaps = {
        # already the best seat on the map...
        "SM-D1": a_known_current_seat(["WINDOW"], (("73", ["AISLE"], True),)),
        # ...and a genuinely better one free on the other leg
        "SM-D2": a_known_current_seat(["AISLE"], (("73", ["WINDOW"], True),)),
    }
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, booking_numbers=["NUM1"], dry_run=True) is True
    assert not c.seat_updates
    out = capsys.readouterr().out
    assert "keeps carriage 3 seat 39 · window" in out
    assert "would take carriage 3 seat 73 · window" in out
    assert "● dry run · 1 seat(s) would change" in out


def test_a_dry_run_that_would_change_nothing_says_so(capsys):
    c = FakeClient()
    c.bookings_list = [booking_item()]
    c.seatmaps = {"SM-OUTBOUND": a_known_current_seat(["WINDOW"], (("73", ["AISLE"], True),))}
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]

    assert handle_change_seat(c, "TOKEN", cfg, dates=[FUTURE_DATE], dry_run=True) is True
    out = capsys.readouterr().out
    assert "keeps carriage 3 seat 39 ·" in out
    assert "● dry run · nothing to change" in out
