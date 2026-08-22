"""
Characterisation tests for the booking flow against a scripted client.

They pin the API call sequence, the return contract of handle_booking_process
and the user-facing messages, so the flow can be refactored safely.
"""

from datetime import datetime

from sj_booking import (
    day_route,
    handle_booking_process,
    plan_day,
    process_booking_flow,
    process_date_range,
)
from tests.conftest import FakeClient, FakeTokenManager, base_cfg, dep, offers

D = "2026-09-01"
OUT = [dep("o-early", D, "06:30", "08:10"), dep("o-best", D, "06:59", "11:36"), dep("o-late", D, "07:30", "09:10")]
IN = [dep("i-early", D, "16:50", "18:30"), dep("i-best", D, "17:22", "21:53"), dep("i-late", D, "18:00", "19:40")]
NO_OFFER = offers(calm_price=295, second_price=195)
LKP, STH = "740000009", "740000001"


def flow(client, cfg=None, existing=(), dry_run=False):
    """plan_day + process_booking_flow, as process_date_range does it."""
    cfg = cfg or base_cfg()
    need_out, need_in = plan_day(client, cfg["search_parameters"], list(existing), D)
    return process_booking_flow(client, "tok", cfg, datetime(2026, 9, 1), "TP", "TOK",
                                need_out, need_in, dry_run=dry_run)


def existing(origin, dest):
    return {"bookingId": "U", "booking": {"bookingStatus": "CONFIRMED", "journeys": [{"segments": [
        {"departureStation": {"uicStationCode": origin}, "arrivalStation": {"uicStationCode": dest},
         "departureDateTime": f"{D}T06:59:00+02:00"}]}]}}


def booked(result):
    """The booking-mode result minus the (large) booking object."""
    return {k: v for k, v in result.items() if k != "booking"}


# --- roundtrip, both legs ---------------------------------------------------

def test_roundtrip_books_both_legs_in_one_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c)
    assert booked(result) == {"booking_id": "UUID-1", "booking_number": "NUM1",
                              "legs": ["outbound", "return"], "checked_out": True}
    assert len(result["booking"]["journeys"]) == 2  # final booking object carries both legs
    assert c.calls == [
        ("search", "Linköping Central", "Stockholm Central", D, D),
        ("results", "OUT"), ("offers", "o-best"), ("create", "OFF-calm"),
        ("results", "IN"), ("offers", "i-best"), ("add", "UUID-1", "OFF-calm"),
        ("customer", "UUID-1"), ("checkout", "UUID-1"),
    ]


def test_outbound_no_offer_uses_closest_earlier_alternative(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER})
    result = flow(c)
    assert result["legs"] == ["outbound", "return"]
    creates = [x for x in c.calls if x[0] in ("create", "offers")]
    assert creates[:3] == [("offers", "o-best"), ("offers", "o-early"), ("create", "OFF-calm")]
    out = capsys.readouterr().out
    assert "! no valid offer for outbound at 06:59, trying closest alternative" in out
    assert "found offer at alternative departure 06:30" in out


def test_outbound_no_offer_no_earlier_alternative_books_nothing(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER})
    assert flow(c) is None
    assert not [x for x in c.calls if x[0] in ("create", "add", "checkout")]
    assert "alternative departure 06:30 also unavailable" in capsys.readouterr().out


def test_outbound_unbookable_with_book_partial_books_return_alone(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER})
    result = flow(c, base_cfg(book_partial=True))
    assert result["legs"] == ["return"]
    searches = [x for x in c.calls if x[0] == "search"]
    assert searches == [("search", "Linköping Central", "Stockholm Central", D, D),
                        ("search", "Stockholm Central", "Linköping Central", D, None)]
    assert c.calls[-3:] == [("create", "OFF-calm"), ("customer", "UUID-1"), ("checkout", "UUID-1")]
    assert "trying return leg as a separate booking" in capsys.readouterr().out


def test_return_no_offer_uses_closest_later_alternative():
    c = FakeClient({"OUT": OUT, "IN": IN}, {"i-best": NO_OFFER})
    result = flow(c)
    assert result["legs"] == ["outbound", "return"]
    assert ("offers", "i-late") in c.calls
    assert ("offers", "i-early") not in c.calls
    assert ("add", "UUID-1", "OFF-calm") in c.calls


def test_return_unbookable_keeps_outbound_only(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"i-best": NO_OFFER, "i-late": NO_OFFER})
    result = flow(c)
    assert result["legs"] == ["outbound"]
    assert ("checkout", "UUID-1") in c.calls
    assert "no alternative found, booking outbound only" in capsys.readouterr().out


def test_return_without_departures_keeps_outbound_only(capsys):
    c = FakeClient({"OUT": OUT, "IN": []})
    result = flow(c)
    assert result["legs"] == ["outbound"] and result["checked_out"]
    assert "no departure found for inbound, booking outbound only" in capsys.readouterr().out


def test_no_outbound_departures_books_nothing(capsys):
    c = FakeClient({"OUT": [], "IN": IN})
    assert flow(c) is None
    assert "no departure found for outbound" in capsys.readouterr().out


def test_checkout_failure_is_reported_in_result(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, checkout_ok=False)
    result = flow(c)
    assert result["checked_out"] is False
    assert result["legs"] == ["outbound", "return"]
    assert "checkout failed" in capsys.readouterr().out


def test_exact_time_only_when_select_closest_false():
    c = FakeClient({"OUT": [OUT[0], OUT[2]], "IN": IN})
    assert flow(c, base_cfg(select_closest_ticket_available=False)) is None
    c = FakeClient({"OUT": OUT, "IN": IN})
    assert flow(c, base_cfg(select_closest_ticket_available=False))["legs"] == ["outbound", "return"]


def test_class_fallback_message_when_offer_class_differs(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": offers(calm_price=None)})
    result = flow(c)
    assert result["legs"] == ["outbound", "return"]
    assert ("create", "OFF-second") in c.calls
    assert "! outbound class fallback: 2 class calm → 2 class" in capsys.readouterr().out


def test_booking_number_is_read_from_nested_booking_object():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c)
    assert result["booking_number"] == "NUM1"  # API nests it under "booking"


# --- duplicate handling -----------------------------------------------------

def test_plan_day_and_route():
    c = FakeClient()
    p = base_cfg()["search_parameters"]
    assert plan_day(c, p, [], D) == (True, True)
    assert plan_day(c, p, [existing(LKP, STH)], D) == (False, True)
    assert plan_day(c, p, [existing(STH, LKP)], D) == (True, False)
    assert plan_day(c, p, [existing(LKP, STH), existing(STH, LKP)], D) == (False, False)
    assert plan_day(c, base_cfg(roundtrip=False)["search_parameters"], [existing(STH, LKP)], D) == (True, False)
    assert day_route(p, True, True) == "Linköping Central ⇄ Stockholm Central"
    assert day_route(p, True, False) == "Linköping Central → Stockholm Central"
    assert day_route(p, False, True) == "Stockholm Central → Linköping Central"


def test_fully_booked_day_is_skipped(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    assert flow(c, existing=[existing(LKP, STH), existing(STH, LKP)]) is None
    assert not [x for x in c.calls if x[0] != "resolve"]
    assert "tickets already booked" in capsys.readouterr().out


def test_only_missing_leg_is_searched_one_way(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(LKP, STH)])
    assert result["legs"] == ["return"]
    assert c.calls[0] == ("search", "Stockholm Central", "Linköping Central", D, None)
    assert ("create", "OFF-calm") in c.calls
    assert "outbound already booked, searching return only" in capsys.readouterr().out

    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(STH, LKP)])
    assert result["legs"] == ["outbound"]
    assert c.calls[0] == ("search", "Linköping Central", "Stockholm Central", D, None)
    assert "return already booked, searching outbound only" in capsys.readouterr().out


def test_one_way_config_books_outbound_only(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, base_cfg(roundtrip=False))
    assert result["legs"] == ["outbound"]
    assert ("results", "IN") not in c.calls
    assert "already booked" not in capsys.readouterr().out


# --- dry run -----------------------------------------------------------------

def test_dry_run_collects_both_legs_without_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, dry_run=True)
    assert result == {
        "outbound": {"departure": "06:59", "arrival": "11:36", "duration": "4h 37m", "train": "X 2000 O-BEST",
                     "route": "Linköping Central → Stockholm Central", "class": "2 class calm",
                     "flexibility": "FULLFLEX", "has_offer": True},
        "inbound": {"departure": "17:22", "arrival": "21:53", "duration": "4h 37m", "train": "X 2000 I-BEST",
                    "route": "Stockholm Central → Linköping Central", "class": "2 class calm",
                    "flexibility": "FULLFLEX", "has_offer": True},
    }
    assert not [x for x in c.calls if x[0] in ("create", "add", "customer", "checkout")]


def test_dry_run_reports_missing_offer_and_alternative():
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER, "i-best": NO_OFFER})
    result = flow(c, dry_run=True)
    out, inb = result["outbound"], result["inbound"]
    assert (out["departure"], out["class"], out["flexibility"], out["has_offer"]) == ("06:59", "2 class calm", None, False)
    assert (inb["departure"], inb["arrival"], inb["flexibility"], inb["has_offer"]) == ("18:00", "19:40", "FULLFLEX", True)


def test_dry_run_no_departures_gives_dashes():
    c = FakeClient({"OUT": [], "IN": []})
    result = flow(c, dry_run=True)
    assert result["outbound"]["departure"] == "—" and not result["outbound"]["has_offer"]
    assert result["inbound"]["departure"] == "—"


# --- handle_booking_process direct contract ---------------------------------

def test_handle_booking_process_inbound_only_creates_booking():
    c = FakeClient({"IN": IN})
    result = handle_booking_process(c, "tok", base_cfg(), "PT", None, "IN", False)
    assert result["legs"] == ["return"] and result["booking_number"] == "NUM1"
    assert c.calls[:3] == [("results", "IN"), ("offers", "i-best"), ("create", "OFF-calm")]


# --- process_date_range: day cards -----------------------------------------

def run_range(c, cfg, dry_run, existing=()):
    return process_date_range(c, "tok", FakeTokenManager(), cfg, "TP", "TOK", list(existing), dry_run=dry_run)


def test_book_mode_prints_day_cards_notes_and_summary(capsys):
    cfg = base_cfg(date_start="2026-09-04", date_end="2026-09-08")  # Fri..Tue
    c = FakeClient({"OUT": OUT, "IN": IN})
    def on_monday(item):
        for seg in item["booking"]["journeys"][0]["segments"]:
            seg["departureDateTime"] = "2026-09-07T06:59:00+02:00"
        return item
    existing_mon = [on_monday(existing(LKP, STH)), on_monday(existing(STH, LKP))]
    assert run_range(c, cfg, dry_run=False, existing=existing_mon) == []
    out = capsys.readouterr().out
    # day card: header, indented progress, indented legs with number
    assert "fri 04 sep 2026   Linköping Central ⇄ Stockholm Central\n" in out
    assert "  ✓ searching outbound at 06:59\n" in out
    assert "  ✓ checking out booking NUM1\n" in out
    assert "  → 06:59 – 11:36   4h 37m   X 2000 520   carriage 3 seat 17   2 klass Lugn   NUM1\n" in out
    assert "  ← 17:22 – 21:53   4h 37m   X 2000 543   carriage 3 seat 17   2 klass Lugn   NUM1\n" in out
    # one-line days
    assert "sat 05 sep 2026   weekend\n" in out and "sun 06 sep 2026   weekend\n" in out
    assert "mon 07 sep 2026   tickets already booked\n" in out
    assert "tue 08 sep 2026   Linköping Central ⇄ Stockholm Central\n" in out
    # no date/route repetition inside the card, no 'done', summary footer instead
    assert "searching 2026-09-04" not in out and "done" not in out
    assert "\n ● 5 day(s) · 2 booked · 1 already booked · 2 skipped" in out


def test_book_mode_card_for_failed_day(capsys):
    c = FakeClient({"OUT": [], "IN": IN})
    run_range(c, base_cfg(), dry_run=False)
    out = capsys.readouterr().out
    assert "tue 01 sep 2026   Linköping Central ⇄ Stockholm Central\n   ✓ searching outbound at 06:59\n" in out
    assert "  ! no departure found for outbound\n   nothing booked\n" in out
    assert "\n ● 1 day(s) · 1 not booked" in out


def test_book_mode_checkout_failure_is_counted(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, checkout_ok=False)
    run_range(c, base_cfg(), dry_run=False)
    out = capsys.readouterr().out
    assert "  ! checkout failed, provisional left (cleaned up on next --book run)\n" in out
    assert "\n ● 1 day(s) · 1 checkout failed" in out


def test_dry_run_prints_cards_with_notes_and_returns_rows(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"i-best": NO_OFFER, "i-late": NO_OFFER})
    rows = run_range(c, base_cfg(), dry_run=True)
    out = capsys.readouterr().out
    # the blank before the first card is the caller's (printed before the
    # bookings fetch); process_date_range starts with the card itself
    assert not out.startswith("\n")
    assert [r["direction"] for r in rows] == ["Outbound", "Return"]
    assert rows[0]["note"] == "" and rows[1]["note"] == "no 0-price offer"
    assert "  → 06:59 – 11:36   4h 37m   X 2000 O-BEST   2 class calm   FULLFLEX\n" in out
    assert "  ← 17:22 – 21:53   4h 37m   X 2000 I-BEST   2 class calm   no 0-price offer\n" in out
    assert "\n ● dry run · 1 day(s) · 1 partly bookable" in out
    assert not [x for x in c.calls if x[0] in ("create", "add", "customer", "checkout")]


def test_process_date_range_survives_per_date_exception(capsys):
    class Boom(FakeClient):
        def search_journey(self, *a, **k):
            raise RuntimeError("api down")
    rows = run_range(Boom(), base_cfg(), dry_run=True)
    assert rows == []
    out = capsys.readouterr().out
    assert "  error: api down\n" in out
    assert "\n ● dry run · 1 day(s) · 1 unavailable" in out
