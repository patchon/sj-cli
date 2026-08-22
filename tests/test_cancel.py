"""Cancel dry-run: show the cards and what would be cancelled — no prompts,
no cancellation API calls (the client below forbids every call)."""

import pytest

from sj_api_client import booking
from sj_api_client.booking import handle_cancel_booking


def _seg(direction, service_id):
    return {
        "direction": direction,
        "departureDateTime": "2099-09-24T21:54:00+02:00",
        "arrivalDateTime": "2099-09-24T23:36:00+02:00",
        "duration": "PT1H42M",
        "departureStation": {"name": "Stockholm Central", "uicStationCode": "1"},
        "arrivalStation": {"name": "Linköping Central", "uicStationCode": "2"},
        "serviceIdentifier": service_id,
        "passengers": [{"id": "p1"}],
    }


def _bookings(number="NUM1"):
    return [
        {
            "bookingId": "U1",
            "booking": {
                "bookingId": "U1",
                "bookingNumber": number,
                "bookingStatus": "CONFIRMED",
                "possibleActions": ["CANCEL_JOURNEY"],
                "journeys": [
                    {"segments": [_seg("OUTBOUND", "s1")]},
                    {"segments": [_seg("INBOUND", "s2")]},
                ],
            },
        }
    ]


class NoCallClient:
    def __getattr__(self, name):
        raise AssertionError(f"client.{name} must not be called during a dry-run cancel")


def test_cancel_booking_dry_run_shows_cards_and_touches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: _bookings())
    monkeypatch.setattr(
        "builtins.input", lambda *_a: pytest.fail("prompted during a dry-run cancel")
    )
    handle_cancel_booking(NoCallClient(), "tok", {}, "NUM1", dry_run=True)
    out = capsys.readouterr().out
    assert "21:54" in out  # the day card is shown
    assert "● dry run · 2 journey(s) would be cancelled from booking NUM1" in out
