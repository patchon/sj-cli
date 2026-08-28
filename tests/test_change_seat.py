"""Tests for handle_change_seat: re-seating existing bookings by date or booking number."""

from datetime import timedelta

from sj_cli.booking import handle_change_seat
from sj_cli.dates import sweden_now
from tests.fakes import FakeClient, base_cfg, seatmap

# Computed from the real clock at test-run time (not hardcoded) so these
# dates are never accidentally in the past — a booking dated in the past
# would be reported as "already departed" and every test below would pass
# vacuously (no seat map read, no PATCH, for the wrong reason).
FUTURE_DATE = (sweden_now() + timedelta(days=30)).date().isoformat()
PAST_DATE = (sweden_now() - timedelta(days=2)).date().isoformat()


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
    assert "would take" in capsys.readouterr().out


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
