from datetime import datetime

from sj_api_client.booking import (
    _dry_run_note,
    _find_departure_by_time,
    _resolve_class_for_departure,
    _span_label,
    booking_date_range,
    check_comfort_availability,
    check_existing_booking,
    describe_run,
    find_offer_id,
    get_departure_time_minutes,
    is_stale_provisional,
    select_best_departure,
    time_str_to_minutes,
)
from tests.fakes import dep, offers

D = "2026-09-01"


def test_time_helpers():
    assert time_str_to_minutes("06:59") == 419
    assert time_str_to_minutes("bad") == 0
    assert get_departure_time_minutes(dep("x", D, "17:22", "19:01")) == 1042
    assert get_departure_time_minutes({"departureDateTime": "nope"}) == -1


def test_comfort_availability():
    d_ab = dep("x", D, "06:00", "07:00", props=("COMFORT-AB",))
    d_b = dep("x", D, "06:00", "07:00", props=("COMFORT-B",))
    d_calm = dep("x", D, "06:00", "07:00", props=("COMFORT-B", "COMFORT-CALM"))
    assert check_comfort_availability(d_ab, "1 class") and check_comfort_availability(
        d_ab, "2 class"
    )
    assert not check_comfort_availability(d_b, "1 class")
    assert not check_comfort_availability(d_b, "2 class calm")
    assert check_comfort_availability(d_calm, "2 class calm")
    assert not check_comfort_availability(d_calm, "business")


def test_find_departure_by_time_closest_vs_exact():
    deps = [
        dep("a", D, "06:30", "08:00"),
        dep("b", D, "07:10", "08:40"),
        dep("c", D, "07:00", "08:30"),
    ]
    assert _find_departure_by_time(deps, "06:59", select_closest=True)["departureId"] == "c"
    assert _find_departure_by_time(deps, "06:59", select_closest=False) is None
    assert _find_departure_by_time(deps, "07:10", select_closest=False)["departureId"] == "b"
    assert _find_departure_by_time([], "06:59", select_closest=True) is None


def test_resolve_class_fallback_chain():
    d_b = dep("x", D, "06:00", "07:00", props=("COMFORT-B",))
    assert _resolve_class_for_departure(d_b, "2 class calm", allow_fallback=True) == "2 class"
    assert _resolve_class_for_departure(d_b, "2 class calm", allow_fallback=False) is None
    assert _resolve_class_for_departure(d_b, "1 class", allow_fallback=True) == "2 class"
    d_ab = dep("x", D, "06:00", "07:00", props=("COMFORT-AB",))
    assert _resolve_class_for_departure(d_ab, "1 class", allow_fallback=False) == "1 class"


def test_select_best_departure_shape(capsys):
    deps = [dep("a", D, "06:30", "08:00", props=("COMFORT-B",)), dep("b", D, "07:10", "08:40")]
    best = select_best_departure(deps, "06:59", "2 class calm", select_closest=True)
    assert best["id"] == "b"
    assert best["class"] == "2 class calm"
    assert best["diff"] == 11
    assert best["time_str"] == "07:10"
    # class fallback on the chosen departure is announced
    best = select_best_departure(deps, "06:35", "2 class calm", select_closest=True)
    assert best["id"] == "a" and best["class"] == "2 class"
    assert "2 class calm unavailable, using 2 class" in capsys.readouterr().out
    # a closest departure without the class does not end the leg: the next
    # closest that carries it is taken, and the skip is announced
    best = select_best_departure(
        deps, "06:35", "2 class calm", select_closest=True, allow_fallback=False
    )
    assert best["id"] == "b" and best["class"] == "2 class calm"
    assert "departure at 06:30: no matching class available" in capsys.readouterr().out
    assert (
        select_best_departure(
            [deps[0]], "06:35", "2 class calm", select_closest=True, allow_fallback=False
        )
        is None
    )
    # exact time only: the single exact match is tried, nothing else
    assert select_best_departure(deps, "06:35", "2 class calm", select_closest=False) is None


def test_find_offer_id_prefers_calm_then_second():
    assert find_offer_id(offers(), "2 class calm", "FULLFLEX") == ("OFF-calm", "2 class calm")
    assert find_offer_id(offers(calm_price=None), "2 class calm", "FULLFLEX") == (
        "OFF-second",
        "2 class",
    )
    assert find_offer_id(offers(), "2 class", "FULLFLEX") == ("OFF-second", "2 class")
    assert find_offer_id(offers(first_price=0), "1 class", "FULLFLEX") == ("OFF-first", "1 class")
    assert (
        find_offer_id(offers(calm_price=295, second_price=195), "2 class calm", "FULLFLEX") is None
    )
    assert find_offer_id(offers(flex="SEMIFLEX"), "2 class calm", "FULLFLEX") is None
    assert find_offer_id(offers(available=False), "2 class calm", "FULLFLEX") is None
    assert find_offer_id({}, "2 class calm", "FULLFLEX") is None


def _booking(status, origin, dest, date, actions=()):
    return {
        "bookingId": "U",
        "booking": {
            "bookingNumber": "N",
            "bookingStatus": status,
            "possibleActions": list(actions),
            "journeys": [
                {
                    "segments": [
                        {
                            "departureStation": {"uicStationCode": origin},
                            "arrivalStation": {"uicStationCode": dest},
                            "departureDateTime": f"{date}T06:59:00+02:00",
                        }
                    ]
                }
            ],
        },
    }


def test_check_existing_booking_ignores_cancelled_and_stale():
    confirmed = _booking("CONFIRMED", "1", "2", D)
    assert check_existing_booking([confirmed], "1", "2", D)
    assert not check_existing_booking([confirmed], "2", "1", D)
    assert not check_existing_booking([confirmed], "1", "2", "2026-09-02")
    assert not check_existing_booking([_booking("CANCELLED", "1", "2", D)], "1", "2", D)
    stale = _booking("NEW", "1", "2", D, actions=["CANCEL_JOURNEY"])
    assert is_stale_provisional(stale["booking"])
    assert not check_existing_booking([stale], "1", "2", D)
    assert not is_stale_provisional(_booking("NEW", "1", "2", D)["booking"])


def test_dry_run_note():
    assert _dry_run_note({"has_offer": True}) == ""
    assert _dry_run_note({"has_offer": False, "departure": "—"}) == "no departure found"
    assert _dry_run_note({"has_offer": False, "departure": "06:59"}) == "no 0-price offer"


def test_booking_date_range():
    start, end = booking_date_range(
        {"endTravelValidityDateTime": "2027-03-18T01:00:00+01:00"}, start_offset_days=1
    )
    assert end == "2027-03-19"
    start0, end0 = booking_date_range(None, fallback_days=10)
    assert start0 <= start
    assert end0 > start0


def test_span_label():
    d = datetime
    assert _span_label(d(2026, 9, 18), d(2026, 9, 21)) == "18 – 21 sep 2026"
    assert _span_label(d(2026, 9, 1), d(2026, 10, 30)) == "1 sep – 30 oct 2026"
    assert _span_label(d(2026, 12, 29), d(2027, 1, 9)) == "29 dec 2026 – 9 jan 2027"
    assert _span_label(d(2026, 9, 15), d(2026, 9, 15)) == "15 sep 2026"


def test_describe_run():
    from tests.fakes import base_cfg

    p = base_cfg(date_start="2026-09-01", date_end="2026-10-30", service_types=["SJ_HIGH"])[
        "search_parameters"
    ]
    assert describe_run(p) == [
        ("route", "Linköping Central ⇄ Stockholm Central"),
        ("days", "1 sep – 30 oct 2026 · weekdays only"),
        ("times", "out 06:59 · back 17:22"),
        ("ticket", "2 class calm · FULLFLEX · SJ High-speed train"),
    ]
    p = base_cfg(
        roundtrip=False,
        date_end="2026-09-01",
        select_closest_ticket_available=False,
        skip_weekends=False,
        allow_class_fallback=False,
        book_partial=True,
    )["search_parameters"]
    assert describe_run(p) == [
        ("route", "Linköping Central → Stockholm Central"),
        ("days", "1 sep 2026 · every day except red days"),
        ("times", "out 06:59"),
        ("ticket", "2 class calm · FULLFLEX · exact time only · no class fallback · partial ok"),
    ]
    p = base_cfg(skip_holidays=False, service_types=["ALL"])["search_parameters"]
    label, value = describe_run(p)[1]
    assert label == "days"
    assert value.endswith("weekdays incl. red days")
    assert describe_run(p)[3] == ("ticket", "2 class calm · FULLFLEX")


def test_confirm_uses_question_prompt(monkeypatch, capsys):
    from sj_api_client.booking import _confirm

    monkeypatch.setattr("builtins.input", lambda: "Y")
    assert _confirm("cancel booking ERU0HWB2? [y/n]: ") is True
    assert capsys.readouterr().out.startswith(" ? cancel booking ERU0HWB2? [y/n]: ")
    monkeypatch.setattr("builtins.input", lambda: "nope")
    assert _confirm("cancel? [y/n]: ") is False


def test_find_offer_id_falls_through_class_chain_on_same_departure():
    # 1 class requested but the pass only covers 2 class (e.g. Årskort Silver):
    # the FIRST offer exists at a price > 0, calm/second are 0-price. The §7.2
    # chain must fall through on the same departure instead of giving up.
    o = offers(first_price=295)
    assert find_offer_id(o, "1 class", "FULLFLEX") == ("OFF-calm", "2 class calm")
    # 2 class falls up to calm when plain second has no 0-price offer
    assert find_offer_id(offers(second_price=195), "2 class", "FULLFLEX") == (
        "OFF-calm",
        "2 class calm",
    )


def test_find_offer_id_honours_allow_fallback_flag():
    # exact class only when the flag is off — at the offer level too (SPEC §7.2)
    assert (
        find_offer_id(offers(first_price=295), "1 class", "FULLFLEX", allow_fallback=False) is None
    )
    assert (
        find_offer_id(offers(calm_price=None), "2 class calm", "FULLFLEX", allow_fallback=False)
        is None
    )


def test_segment_date_is_the_swedish_calendar_date():
    from sj_api_client.booking import _segment_date

    assert _segment_date("2026-10-24T22:30:00Z") == "2026-10-25"  # 00:30 Swedish
    assert _segment_date("2026-09-01T06:59:00+02:00") == "2026-09-01"
    assert _segment_date("2026-09-01") == "2026-09-01"  # unparsable: raw date part


def test_duplicate_check_matches_a_journey_with_a_change():
    from sj_api_client.booking import check_existing_booking

    def seg(origin, dest, when):
        return {
            "departureStation": {"uicStationCode": origin},
            "arrivalStation": {"uicStationCode": dest},
            "departureDateTime": when,
        }

    connection = {
        "booking": {
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {
                    "segments": [
                        seg("A", "C", f"{D}T06:59:00+02:00"),
                        seg("C", "B", f"{D}T07:40:00+02:00"),
                    ]
                }
            ],
        }
    }
    assert check_existing_booking([connection], "A", "B", D)
    assert not check_existing_booking([connection], "A", "C", D)  # the leg, not the journey
    assert not check_existing_booking([connection], "B", "A", D)
