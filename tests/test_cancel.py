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


class RecordingCancelClient:
    """Accepts every cancel call and records the PATCH payload."""

    def __init__(self):
        self.patches = []

    def cancel_booking_with_patch(self, token, booking_id, payload):
        self.patches.append((booking_id, payload))
        return True

    def finalize_cancellation(self, token, booking_id):
        return True


def _answers(monkeypatch, *replies):
    it = iter(replies)
    monkeypatch.setattr(booking, "ask", lambda _t: next(it))


def test_cancel_selection_deduplicates_and_is_validated_first(monkeypatch, capsys):
    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: _bookings())
    c = RecordingCancelClient()
    _answers(monkeypatch, "1, 1", "y")
    assert handle_cancel_booking(c, "tok", {}, "NUM1") is True
    assert c.patches == [("U1", [{"serviceIdentifier": "s1", "passengerIds": ["p1"]}])]
    assert "● booking NUM1 cancelled" not in capsys.readouterr().out  # 1 of 2 journeys

    c = RecordingCancelClient()
    _answers(monkeypatch, "1,x")  # an unknown token aborts instead of being dropped
    assert handle_cancel_booking(c, "tok", {}, "NUM1") is False
    assert c.patches == [] and "invalid selection, aborting" in capsys.readouterr().out


def test_cancel_return_values_drive_the_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: _bookings())
    assert handle_cancel_booking(NoCallClient(), "tok", {}, "NUM1", dry_run=True) is True
    assert handle_cancel_booking(NoCallClient(), "tok", {}, "NOPE") is False
    _answers(monkeypatch, "1", "n")  # picked a journey, then declined to confirm
    assert handle_cancel_booking(NoCallClient(), "tok", {}, "NUM1") is False
    assert "cancellation aborted" in capsys.readouterr().out


def test_stale_provisional_is_not_a_booking_for_list_and_cancel(monkeypatch, capsys):
    from sj_api_client.booking import handle_list_bookings

    stale = _bookings("PROV1")
    stale[0]["booking"]["bookingStatus"] = "NEW"  # NEW + CANCEL_JOURNEY = leftover
    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: stale)
    handle_list_bookings(NoCallClient(), "tok", {})
    assert "no bookings found" in capsys.readouterr().out
    assert handle_cancel_booking(NoCallClient(), "tok", {}, "PROV1", dry_run=True) is False
    assert "no active booking found with number PROV1" in capsys.readouterr().out


def test_cancel_date_with_nothing_to_cancel_is_not_a_failure(monkeypatch, capsys):
    from sj_api_client.booking import handle_cancel_mode
    from tests.fakes import FakeClient, base_cfg

    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: [])
    assert handle_cancel_mode(FakeClient(), "tok", base_cfg(), "2026-10-05") is True
    assert "no bookings found for 2026-10-05" in capsys.readouterr().out


class RefusingCancelClient:
    """The API refuses the PATCH (or the confirmation) with a typed error."""

    def __init__(self, *, refuse_patch=True):
        self.refuse_patch = refuse_patch

    def cancel_booking_with_patch(self, token, booking_id, payload):
        if self.refuse_patch:
            from sj_api_client.errors import SJAPIError

            raise SJAPIError({"errorCode": "E42", "message": "segment already departed"})

    def finalize_cancellation(self, token, booking_id):
        from sj_api_client.errors import SJAPIError

        raise SJAPIError({"errorCode": "E43", "message": "checkout refused"})


def test_cancel_failures_name_the_cause(monkeypatch, capsys):
    monkeypatch.setattr(booking, "fetch_all_bookings", lambda *_a, **_k: _bookings())
    _answers(monkeypatch, "a", "y")
    assert handle_cancel_booking(RefusingCancelClient(), "tok", {}, "NUM1") is False
    out = capsys.readouterr().out
    assert "✗ cancelling booking NUM1" in out
    assert "● failed to cancel booking NUM1: E42 · segment already departed" in out

    _answers(monkeypatch, "a", "y")
    assert (
        handle_cancel_booking(RefusingCancelClient(refuse_patch=False), "tok", {}, "NUM1") is False
    )
    out = capsys.readouterr().out
    assert (
        "● cancellation initiated but confirmation failed for NUM1: E43 · checkout refused" in out
    )


def test_stale_provisional_cleanup_reports_the_cause_and_continues(capsys):
    from sj_api_client.booking import cleanup_stale_provisionals

    class RefusingProvisionalClient:
        def cancel_provisional_booking(self, token, booking_id):
            from sj_api_client.errors import SJAPIError

            raise SJAPIError({"errorCode": "E7", "message": "not cancellable"})

    stale = _bookings("PROV1")
    stale[0]["booking"]["bookingStatus"] = "NEW"
    kept = cleanup_stale_provisionals(RefusingProvisionalClient(), "tok", stale + _bookings("NUM2"))
    assert [i["booking"]["bookingNumber"] for i in kept] == ["NUM2"]
    out = capsys.readouterr().out
    assert "✗ cancelling stale provisional booking PROV1" in out
    assert (
        "! could not cancel stale provisional booking PROV1 (E7 · not cancellable), continuing"
        in out
    )
