"""
Characterisation tests for the booking flow against a scripted client.

They pin the API call sequence, the return contract of handle_booking_process
and the user-facing messages, so the flow can be refactored safely.
"""

from datetime import datetime

from sj_cli.booking import (
    day_route,
    handle_booking_process,
    plan_day,
    process_booking_flow,
    process_date_range,
)
from sj_cli.errors import SJAPIError
from tests.fakes import FakeClient, FakeTokenManager, base_cfg, dep, offers, seatmap

D = "2026-09-01"
OUT = [
    dep("o-early", D, "06:30", "08:10"),
    dep("o-best", D, "06:59", "11:36"),
    dep("o-late", D, "07:30", "09:10"),
]
IN = [
    dep("i-early", D, "16:50", "18:30"),
    dep("i-best", D, "17:22", "21:53"),
    dep("i-late", D, "18:00", "19:40"),
]
NO_OFFER = offers(calm_price=295, second_price=195)
GBG, STH = "740000002", "740000001"


def flow(client, cfg=None, existing=(), dry_run=False):
    """plan_day + process_booking_flow, as process_date_range does it."""
    cfg = cfg or base_cfg()
    need_out, need_in = plan_day(client, cfg["search_parameters"], list(existing), D)
    return process_booking_flow(
        client, "tok", cfg, datetime(2026, 9, 1), "TP", "TOK", need_out, need_in, dry_run=dry_run
    )


def existing(origin, dest):
    return {
        "bookingId": "U",
        "booking": {
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {
                    "segments": [
                        {
                            "departureStation": {"uicStationCode": origin},
                            "arrivalStation": {"uicStationCode": dest},
                            "departureDateTime": f"{D}T06:59:00+02:00",
                        }
                    ]
                }
            ],
        },
    }


def booked(result):
    """The booking-mode result minus the (large) booking object."""
    return {k: v for k, v in result.items() if k != "booking"}


# --- roundtrip, both legs ---------------------------------------------------


def test_roundtrip_books_both_legs_in_one_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c)
    assert booked(result) == {
        "booking_id": "UUID-1",
        "booking_number": "NUM1",
        "legs": ["outbound", "return"],
        "checked_out": True,
    }
    assert len(result["booking"]["journeys"]) == 2  # final booking object carries both legs
    assert c.calls == [
        ("search", "Göteborg Central", "Stockholm Central", D, D),
        ("results", "OUT"),
        ("offers", "o-best"),
        ("create", "OFF-calm"),
        ("results", "IN"),
        ("offers", "i-best"),
        ("add", "UUID-1", "OFF-calm"),
        ("customer", "UUID-1"),
        ("checkout", "UUID-1"),
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
    assert searches == [
        ("search", "Göteborg Central", "Stockholm Central", D, D),
        ("search", "Stockholm Central", "Göteborg Central", D, None),
    ]
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
    assert flow(c, base_cfg(select_closest_ticket_available=False))["legs"] == [
        "outbound",
        "return",
    ]


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
    assert plan_day(c, p, [existing(GBG, STH)], D) == (False, True)
    assert plan_day(c, p, [existing(STH, GBG)], D) == (True, False)
    assert plan_day(c, p, [existing(GBG, STH), existing(STH, GBG)], D) == (False, False)
    assert plan_day(c, base_cfg(roundtrip=False)["search_parameters"], [existing(STH, GBG)], D) == (
        True,
        False,
    )
    assert day_route(p, True, True) == "Göteborg Central ⇄ Stockholm Central"
    assert day_route(p, True, False) == "Göteborg Central → Stockholm Central"
    assert day_route(p, False, True) == "Stockholm Central → Göteborg Central"


def test_fully_booked_day_is_skipped(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    assert flow(c, existing=[existing(GBG, STH), existing(STH, GBG)]) is None
    assert not [x for x in c.calls if x[0] != "resolve"]
    assert "tickets already booked" in capsys.readouterr().out


def test_only_missing_leg_is_searched_one_way(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(GBG, STH)])
    assert result["legs"] == ["return"]
    assert c.calls[0] == ("search", "Stockholm Central", "Göteborg Central", D, None)
    assert ("create", "OFF-calm") in c.calls
    assert "outbound already booked, searching return only" in capsys.readouterr().out

    c = FakeClient({"OUT": OUT, "IN": IN})
    result = flow(c, existing=[existing(STH, GBG)])
    assert result["legs"] == ["outbound"]
    assert c.calls[0] == ("search", "Göteborg Central", "Stockholm Central", D, None)
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
        "outbound": {
            "departure": "06:59",
            "arrival": "11:36",
            "duration": "4h 37m",
            "train": "X 2000 O-BEST",
            "route": "Göteborg Central → Stockholm Central",
            "class": "2 class calm",
            "flexibility": "FULLFLEX",
            "has_offer": True,
        },
        "inbound": {
            "departure": "17:22",
            "arrival": "21:53",
            "duration": "4h 37m",
            "train": "X 2000 I-BEST",
            "route": "Stockholm Central → Göteborg Central",
            "class": "2 class calm",
            "flexibility": "FULLFLEX",
            "has_offer": True,
        },
    }
    assert not [x for x in c.calls if x[0] in ("create", "add", "customer", "checkout")]


def test_dry_run_reports_missing_offer_and_alternative():
    c = FakeClient(
        {"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER, "i-best": NO_OFFER}
    )
    result = flow(c, dry_run=True)
    out, inb = result["outbound"], result["inbound"]
    assert (out["departure"], out["class"], out["flexibility"], out["has_offer"]) == (
        "06:59",
        "2 class calm",
        None,
        False,
    )
    # book mode books nothing from this search without book_partial, so the
    # preview must not call the return "bookable" either
    assert (inb["departure"], inb["arrival"], inb["flexibility"], inb["has_offer"]) == (
        "18:00",
        "19:40",
        None,
        False,
    )
    assert inb["blocked"] == "needs book_partial"


def test_dry_run_return_stays_bookable_with_book_partial():
    c = FakeClient(
        {"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER, "o-early": NO_OFFER, "i-best": NO_OFFER}
    )
    inb = flow(c, base_cfg(book_partial=True), dry_run=True)["inbound"]
    assert (inb["flexibility"], inb["has_offer"]) == ("FULLFLEX", True)


def test_return_leg_api_error_still_checks_out_the_outbound(capsys):
    # SPEC §8.2: the outbound provisional is already held — keep it and check
    # it out alone instead of leaving it for the stale cleanup
    class AddFails(FakeClient):
        def add_offer_to_booking(self, *a, **k):
            raise RuntimeError("PATCH 500")

    c = AddFails({"OUT": OUT, "IN": IN})
    result = flow(c)
    assert booked(result) == {
        "booking_id": "UUID-1",
        "booking_number": "NUM1",
        "legs": ["outbound"],
        "checked_out": True,
    }
    assert ("checkout", "UUID-1") in c.calls
    assert "! return leg failed (PATCH 500), booking outbound only" in capsys.readouterr().out


def test_exact_time_only_never_books_another_departure(capsys):
    # select_closest_ticket_available=false: an offer-less exact match ends
    # the leg; the alternative-departure fallback must not run (SPEC §7.1)
    c = FakeClient({"OUT": OUT, "IN": IN}, {"o-best": NO_OFFER})
    assert flow(c, base_cfg(select_closest_ticket_available=False)) is None
    assert ("offers", "o-early") not in c.calls and ("create", "OFF-calm") not in c.calls
    assert "no valid offer for outbound at 06:59 (exact time only)" in capsys.readouterr().out


def test_second_train_at_the_exact_minute_is_tried_as_alternative():
    twins = [dep("o-twin", D, "06:59", "08:40"), dep("o-best", D, "06:59", "11:36")]
    c = FakeClient({"OUT": twins, "IN": IN}, {"o-twin": NO_OFFER})
    result = flow(c)
    assert result["legs"] == ["outbound", "return"]
    assert ("offers", "o-twin") in c.calls and ("offers", "o-best") in c.calls


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
    from datetime import date

    return process_date_range(
        c,
        "tok",
        FakeTokenManager(),
        cfg,
        "TP",
        "TOK",
        list(existing),
        dry_run=dry_run,
        today=date(2026, 8, 1),  # the fixtures live in 2026-09
    )


def test_book_mode_prints_day_cards_notes_and_summary(capsys):
    cfg = base_cfg(dates="2026-09-04..2026-09-08")  # Fri..Tue
    c = FakeClient({"OUT": OUT, "IN": IN})

    def on_monday(item):
        for seg in item["booking"]["journeys"][0]["segments"]:
            seg["departureDateTime"] = "2026-09-07T06:59:00+02:00"
        return item

    existing_mon = [on_monday(existing(GBG, STH)), on_monday(existing(STH, GBG))]
    assert run_range(c, cfg, dry_run=False, existing=existing_mon) == {
        "days": 5,
        "booked": 2,
        "already": 1,
        "skipped": 2,
    }
    out = capsys.readouterr().out
    # day card: header, indented progress, indented legs with number
    assert "fri 04 sep 2026   Göteborg Central ⇄ Stockholm Central\n" in out
    assert "  ✓ searching outbound at 06:59\n" in out
    assert "  ✓ checking out booking NUM1\n" in out
    assert (
        "  → 06:59 – 11:36   4h 37m   X 2000 520   carriage 3 seat 17   2 klass Lugn   NUM1\n"
        in out
    )
    assert (
        "  ← 17:22 – 21:53   4h 37m   X 2000 543   carriage 3 seat 17   2 klass Lugn   NUM1\n"
        in out
    )
    # one-line days
    assert "sat 05 sep 2026   weekend\n" in out and "sun 06 sep 2026   weekend\n" in out
    assert "mon 07 sep 2026   tickets already booked\n" in out
    assert "tue 08 sep 2026   Göteborg Central ⇄ Stockholm Central\n" in out
    # no date/route repetition inside the card, no 'done', summary footer instead
    assert "searching 2026-09-04" not in out and "done" not in out
    assert "\n ● 5 day(s) · 2 booked · 1 already booked · 2 skipped" in out


def test_book_mode_card_for_failed_day(capsys):
    c = FakeClient({"OUT": [], "IN": IN})
    run_range(c, base_cfg(), dry_run=False)
    out = capsys.readouterr().out
    assert (
        "tue 01 sep 2026   Göteborg Central ⇄ Stockholm Central\n   ✓ searching outbound at 06:59\n"
        in out
    )
    assert "  ! no departure found for outbound\n   nothing booked\n" in out
    assert "\n ● 1 day(s) · 1 not booked" in out


def test_book_mode_checkout_failure_is_counted(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, checkout_ok=False)
    assert run_range(c, base_cfg(), dry_run=False) == {"days": 1, "failed": 1}
    out = capsys.readouterr().out
    assert "  ! checkout failed, provisional left (cleaned up on next --book run)\n" in out
    assert "\n ● 1 day(s) · 1 checkout failed" in out


def test_dry_run_prints_cards_with_notes_and_returns_counts(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN}, {"i-best": NO_OFFER, "i-late": NO_OFFER})
    counts = run_range(c, base_cfg(), dry_run=True)
    out = capsys.readouterr().out
    # the blank before the first card is the caller's (printed before the
    # bookings fetch); process_date_range starts with the card itself
    assert not out.startswith("\n")
    assert counts == {"days": 1, "partial": 1}
    assert "  → 06:59 – 11:36   4h 37m   X 2000 O-BEST   2 class calm   FULLFLEX\n" in out
    assert "  ← 17:22 – 21:53   4h 37m   X 2000 I-BEST   2 class calm   no 0-price offer\n" in out
    assert "\n ● dry run · 1 day(s) · 1 partly bookable" in out
    assert not [x for x in c.calls if x[0] in ("create", "add", "customer", "checkout")]


def test_process_date_range_survives_per_date_exception(capsys):
    class Boom(FakeClient):
        def search_journey(self, *a, **k):
            raise RuntimeError("api down")

    counts = run_range(Boom(), base_cfg(), dry_run=True)
    assert counts == {"days": 1, "error": 1}  # an error is not "unavailable"
    out = capsys.readouterr().out
    assert "  error: api down\n" in out
    assert "\n ● dry run · 1 day(s) · 1 error(s)" in out


# --- customer details at checkout -------------------------------------------


def test_checkout_reuses_the_phone_number_the_api_put_on_the_booking():
    c = FakeClient({"OUT": OUT, "IN": IN})
    flow(c)
    # never a placeholder: the holder's number from the create response
    assert c.customer_updates == [("UUID-1", "a@b.se", "+46701112233")]


def test_checkout_sends_no_phone_when_the_booking_carries_none():
    class NoCustomer(FakeClient):
        def create_provisional_booking(self, *a, **k):
            resp = super().create_provisional_booking(*a, **k)
            resp["booking"].pop("customer")
            return resp

    c = NoCustomer({"OUT": OUT, "IN": IN})
    flow(c)
    assert c.customer_updates == [("UUID-1", "a@b.se", None)]


# --- process_date_range: robustness -----------------------------------------


def test_past_dates_are_clamped_to_today_with_a_note(capsys):
    from datetime import date

    cfg = base_cfg(dates="2026-09-01..2026-09-04")  # Tue..Fri
    c = FakeClient({"OUT": OUT, "IN": IN})
    counts = process_date_range(
        c, "tok", FakeTokenManager(), cfg, "TP", "TOK", [], dry_run=True, today=date(2026, 9, 3)
    )
    assert counts["days"] == 2
    out = capsys.readouterr().out
    assert "! 2 selected day(s) have passed, starting from 2026-09-03\n\n" in out
    assert "tue 01 sep 2026" not in out and "thu 03 sep 2026" in out


def test_all_selected_dates_passed_ends_with_a_red_verdict(capsys):
    from datetime import date

    cfg = base_cfg(dates="2026-09-01..2026-09-04")
    c = FakeClient({"OUT": OUT, "IN": IN})
    counts = process_date_range(
        c, "tok", FakeTokenManager(), cfg, "TP", "TOK", [], dry_run=True, today=date(2026, 9, 10)
    )
    assert counts == {"days": 0, "error": 1}
    out = capsys.readouterr().out
    assert "● all selected dates have passed" in out
    assert c.calls == []  # nothing searched


def test_non_contiguous_selection_walks_only_the_selected_days(capsys):
    cfg = base_cfg(dates="2026-09-01, 2026-09-03..2026-09-04")  # Tue, Thu..Fri
    c = FakeClient({"OUT": OUT, "IN": IN})
    counts = run_range(c, cfg, dry_run=True)
    assert counts["days"] == 3
    out = capsys.readouterr().out
    assert "tue 01 sep 2026" in out and "thu 03 sep 2026" in out and "fri 04 sep 2026" in out
    assert "wed 02 sep 2026" not in out


def test_render_failure_after_checkout_does_not_lose_the_booking(monkeypatch, capsys):
    from sj_cli import booking as m

    def explode(*_a, **_k):
        raise TypeError("'NoneType' object has no attribute 'get'")

    monkeypatch.setattr(m, "_booked_rows", explode)
    c = FakeClient({"OUT": OUT, "IN": IN})
    assert run_range(c, base_cfg(), dry_run=False) == {"days": 1, "booked": 1}
    out = capsys.readouterr().out
    assert "! booked as NUM1, but the legs could not be shown" in out
    assert "● 1 day(s) · 1 booked" in out


def test_midrun_refresh_failure_stops_the_run_with_a_summary(capsys):
    from sj_cli.errors import SJAuthError

    class ExpiredTokenManager(FakeTokenManager):
        def is_valid(self):
            return False

        def has_refresh_token(self):
            return True

    class RefreshDown(FakeClient):
        def refresh_token(self, _rt):
            raise SJAuthError("token refresh failed: timed out")

    from datetime import date

    cfg = base_cfg(dates="2026-09-01..2026-09-02")
    counts = process_date_range(
        RefreshDown({"OUT": OUT, "IN": IN}),
        "tok",
        ExpiredTokenManager(),
        cfg,
        "TP",
        "TOK",
        [],
        today=date(2026, 8, 1),
    )
    assert counts == {"days": 1, "error": 1}  # the second day is never attempted
    out = capsys.readouterr().out
    assert "tue 01 sep 2026" in out
    assert "  error: token refresh failed: timed out\n" in out
    assert "! stopping: no valid session for the remaining dates\n" in out
    assert "\n ● 1 day(s) · 1 error(s)" in out


# --- seat selection at book time ---------------------------------------------


def test_book_mode_sets_seats_before_checkout():
    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window", "table"]
    run_range(c, cfg, dry_run=False)

    kinds = [call[0] for call in c.calls]
    assert kinds.index("seats") < kinds.index("checkout")
    _, updates, provisional = c.seat_updates[0]
    assert provisional is True
    assert {u["direction"] for u in updates} == {"OUTBOUND", "INBOUND"}
    assert updates[0]["seatStrategy"] == "EXACT"
    assert updates[0]["serviceIdentifier"] == "SI-OUTBOUND"


def test_book_mode_without_the_key_never_touches_the_seat_endpoints():
    c = FakeClient({"OUT": OUT, "IN": IN})
    run_range(c, base_cfg(), dry_run=False)
    assert not [call for call in c.calls if call[0] in ("seatmap", "seats")]


def test_book_mode_skips_the_patch_when_the_seat_is_already_best():
    c = FakeClient({"OUT": OUT, "IN": IN})
    c.seatmaps = {
        "SM-OUTBOUND": seatmap(free=(("70", ["TABLE", "WINDOW"], True),), assigned=("3", "70")),
        "SM-INBOUND": seatmap(free=(("70", ["TABLE", "WINDOW"], True),), assigned=("3", "70")),
    }
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]
    run_range(c, cfg, dry_run=False)
    assert not c.seat_updates


def test_a_seat_failure_still_books_the_day(capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    c.seatmap_error = SJAPIError("seat map unavailable")
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]
    counts = run_range(c, cfg, dry_run=False)
    assert counts["booked"] == 1
    out = capsys.readouterr().out
    assert "! could not read the seat map" in out
    assert "✓ checking out booking" in out


def test_dry_run_never_reads_a_seat_map():
    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]
    run_range(c, cfg, dry_run=True)
    assert not [call for call in c.calls if call[0] in ("seatmap", "seats")]


def test_a_seat_the_api_did_not_give_us_is_reported(capsys, monkeypatch):
    """The API is free to hand back a different seat than the one asked for."""
    c = FakeClient({"OUT": OUT, "IN": IN})
    monkeypatch.setattr(
        c,
        "update_seats",
        lambda _token, bid, _updates, **_kw: {
            "bookingId": bid,
            "booking": c._bookings.get(bid, {}),
        },
    )
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]
    counts = run_range(c, cfg, dry_run=False)
    assert counts["booked"] == 1
    assert "! asked for carriage" in capsys.readouterr().out


def test_a_wish_that_cannot_be_met_is_reported(capsys):
    """best_seat is best-effort: say so when the top wish was not honoured."""
    c = FakeClient({"OUT": OUT, "IN": IN})
    c.seatmaps = {
        "SM-OUTBOUND": seatmap(free=(("17", ["AISLE"], True),), assigned=("3", "39")),
        "SM-INBOUND": seatmap(free=(("17", ["AISLE"], True),), assigned=("3", "39")),
    }
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = ["window"]
    run_range(c, cfg, dry_run=False)
    assert "! outbound: no window seat free, taking carriage 3 seat 17" in capsys.readouterr().out


# --- seat_preference = "ask" -------------------------------------------------


def test_ask_mode_prompts_per_leg_and_takes_the_answer(monkeypatch):
    from sj_cli import booking

    answers = iter(["70", ""])
    monkeypatch.setattr(booking, "ask_optional", lambda _: next(answers))
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)

    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = "ask"
    run_range(c, cfg, dry_run=False)

    # outbound took 70; the empty answer kept the return leg's seat
    assert [u["seatNumber"] for _, updates, _ in c.seat_updates for u in updates] == ["70"]


def test_ask_mode_re_asks_an_unlisted_seat(monkeypatch):
    from sj_cli import booking

    answers = iter(["999", "70", ""])
    monkeypatch.setattr(booking, "ask_optional", lambda _: next(answers))
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)

    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg()
    cfg["search_parameters"]["seat_preference"] = "ask"
    run_range(c, cfg, dry_run=False)
    assert [u["seatNumber"] for _, updates, _ in c.seat_updates for u in updates] == ["70"]


def test_ask_mode_stops_prompting_after_eof(monkeypatch):
    from sj_cli import booking

    calls = []

    def _eof(text):
        calls.append(text)

    monkeypatch.setattr(booking, "ask_optional", _eof)
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)

    # Two booking days (both weekdays, Sep 2026 has no red days) so the run
    # covers four legs in total: only the very first prompt should fire.
    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg(dates="2026-09-01..2026-09-02")
    cfg["search_parameters"]["seat_preference"] = "ask"
    run_range(c, cfg, dry_run=False)
    assert len(calls) == 1  # asked once, then gave up for the whole run
    assert not c.seat_updates


def test_ask_mode_without_a_terminal_warns_once_and_keeps_the_seats(capsys, monkeypatch):
    from sj_cli import booking

    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: False)
    # Multi-day run: the module-level flag must silence every later leg, not
    # just the ones on day one, or a long run would print the warning again
    # and again for no reason.
    c = FakeClient({"OUT": OUT, "IN": IN})
    cfg = base_cfg(dates="2026-09-01..2026-09-02")
    cfg["search_parameters"]["seat_preference"] = "ask"
    run_range(c, cfg, dry_run=False)
    out = capsys.readouterr().out
    assert out.count("! seat selection needs a terminal") == 1
    assert not c.seat_updates
