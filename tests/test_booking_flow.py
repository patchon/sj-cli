"""
Characterisation tests for the booking flow against a scripted client.

They pin the API call sequence, the return contract of handle_booking_process
and the user-facing messages, so the flow can be refactored safely.
"""

from datetime import datetime

from sj_booking import handle_booking_process, process_booking_flow, process_date_range
from tests.conftest import FakeClient, FakeTokenManager, base_cfg, dep, offers

D = "2026-09-01"
OUT = [dep("o-early", D, "06:30", "08:10"), dep("o-best", D, "06:59", "11:36"), dep("o-late", D, "07:30", "09:10")]
IN = [dep("i-early", D, "16:50", "18:30"), dep("i-best", D, "17:22", "21:53"), dep("i-late", D, "18:00", "19:40")]
NO_OFFER = offers(calm_price=295, second_price=195)
ONLY_OUT = "only-out"


def flow(client, cfg=None, existing=(), dry_run=False):
    return process_booking_flow(client, "tok", cfg or base_cfg(), datetime(2026, 9, 1), "TP", "TOK",
                                list(existing), dry_run=dry_run)


def existing(origin, dest):
    return {"bookingId": "U", "booking": {"bookingStatus": "CONFIRMED", "journeys": [{"segments": [
        {"departureStation": {"uicStationCode": origin}, "arrivalStation": {"uicStationCode": dest},
         "departureDateTime": f"{D}T06:59:00+02:00"}]}]}}


LKP, STH = "740000009", "740000001"


# --- roundtrip, both legs ---------------------------------------------------

def test_roundtrip_books_both_legs_in_one_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c)
    assert result == {"booking_id": "UUID-1", "booking_number": "NUM1", "legs": ["outbound", "return"],
                      "checked_out": True}
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
    assert ("no valid offer found for outbound linköping central → stockholm central at 06:59, "
            "looking for closest alternative") in out
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
    assert "outbound class fallback: 2 class calm → 2 class" in capsys.readouterr().out


# --- duplicate handling -----------------------------------------------------

def test_fully_booked_day_is_skipped(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    assert flow(c, existing=[existing(LKP, STH), existing(STH, LKP)]) is None
    assert c.calls == []
    assert "already fully booked" in capsys.readouterr().out


def test_only_missing_leg_is_searched_one_way(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(LKP, STH)])
    assert result["legs"] == ["return"]
    assert c.calls[0] == ("search", "Stockholm Central", "Linköping Central", D, None)
    assert ("create", "OFF-calm") in c.calls
    assert "outbound already booked, searching inbound only" in capsys.readouterr().out

    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(STH, LKP)])
    assert result["legs"] == ["outbound"]
    assert c.calls[0] == ("search", "Linköping Central", "Stockholm Central", D, None)


def test_one_way_config_books_outbound_only():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, base_cfg(roundtrip=False))
    assert result["legs"] == ["outbound"]
    assert ("results", "IN") not in c.calls


# --- dry run -----------------------------------------------------------------

def test_dry_run_collects_both_legs_without_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, dry_run=True)
    assert result == {
        "outbound": {"departure": "06:59", "arrival": "11:36", "class": "2 class calm",
                     "flexibility": "FULLFLEX", "has_offer": True},
        "inbound": {"departure": "17:22", "arrival": "21:53", "class": "2 class calm",
                    "flexibility": "FULLFLEX", "has_offer": True},
    }
    assert not [x for x in c.calls if x[0] in ("create", "add", "customer", "checkout")]


def test_dry_run_reports_missing_offer_and_alternative():
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER, "i-best": NO_OFFER})
    result = flow(c, dry_run=True)
    assert result["outbound"] == {"departure": "06:59", "arrival": "11:36", "class": "2 class calm",
                                  "flexibility": None, "has_offer": False}
    assert result["inbound"] == {"departure": "18:00", "arrival": "19:40", "class": "2 class calm",
                                 "flexibility": "FULLFLEX", "has_offer": True}


def test_dry_run_no_departures_gives_dashes():
    c = FakeClient({"OUT": [], "IN": []})
    result = flow(c, dry_run=True)
    assert result["outbound"]["departure"] == "—" and not result["outbound"]["has_offer"]
    assert result["inbound"]["departure"] == "—"


# --- handle_booking_process direct contract ---------------------------------

def test_handle_booking_process_inbound_only_creates_booking():
    c = FakeClient({"IN": IN})
    result = handle_booking_process(c, "tok", base_cfg(), "PT", None, "IN", False, D)
    assert result["legs"] == ["return"] and result["booking_number"] == "NUM1"
    assert c.calls[:3] == [("results", "IN"), ("offers", "i-best"), ("create", "OFF-calm")]


# --- process_date_range ------------------------------------------------------

def test_process_date_range_prints_per_day_line_and_skips_weekends(capsys):
    cfg = base_cfg(date_start="2026-09-04", date_end="2026-09-07")  # Fri..Mon
    c = FakeClient({"OUT": OUT, "IN": IN})
    results = process_date_range(c, "tok", FakeTokenManager(), cfg, "TP", "TOK", [], dry_run=False)
    out = capsys.readouterr().out
    assert results == []
    assert "2026-09-04: booked outbound + return · num1" in out
    assert "skipping 2026-09-05 (weekend)" in out and "skipping 2026-09-06 (weekend)" in out
    assert "2026-09-07: booked outbound + return · num2" in out


def test_process_date_range_dry_run_rows(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"i-best": NO_OFFER, "i-late": NO_OFFER})
    rows = process_date_range(c, "tok", FakeTokenManager(), base_cfg(), "TP", "TOK", [], dry_run=True)
    assert [r["direction"] for r in rows] == ["Outbound", "Return"]
    assert rows[0]["note"] == "" and rows[1]["note"] == "no 0-price offer"
    assert rows[1]["flexibility"] == "—"


def test_process_date_range_survives_per_date_exception(capsys):
    class Boom(FakeClient):
        def search_journey(self, *a, **k):
            raise RuntimeError("api down")
    rows = process_date_range(Boom(), "tok", FakeTokenManager(), base_cfg(), "TP", "TOK", [], dry_run=True)
    assert rows == []
    assert "error processing 2026-09-01: api down" in capsys.readouterr().out
