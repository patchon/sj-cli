from datetime import datetime

from sj_booking import (
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
from tests.conftest import dep, offers

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
    assert check_comfort_availability(d_ab, "1 class") and check_comfort_availability(d_ab, "2 class")
    assert not check_comfort_availability(d_b, "1 class")
    assert not check_comfort_availability(d_b, "2 class calm")
    assert check_comfort_availability(d_calm, "2 class calm")
    assert not check_comfort_availability(d_calm, "business")


def test_find_departure_by_time_closest_vs_exact():
    deps = [dep("a", D, "06:30", "08:00"), dep("b", D, "07:10", "08:40"), dep("c", D, "07:00", "08:30")]
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
    assert select_best_departure(deps, "06:35", "2 class calm", select_closest=True,
                                 allow_fallback=False) is None


def test_find_offer_id_prefers_calm_then_second():
    assert find_offer_id(offers(), "2 class calm", "FULLFLEX") == ("OFF-calm", "2 class calm")
    assert find_offer_id(offers(calm_price=None), "2 class calm", "FULLFLEX") == ("OFF-second", "2 class")
    assert find_offer_id(offers(), "2 class", "FULLFLEX") == ("OFF-second", "2 class")
    assert find_offer_id(offers(first_price=0), "1 class", "FULLFLEX") == ("OFF-first", "1 class")
    assert find_offer_id(offers(calm_price=295, second_price=195), "2 class calm", "FULLFLEX") is None
    assert find_offer_id(offers(flex="SEMIFLEX"), "2 class calm", "FULLFLEX") is None
    assert find_offer_id(offers(available=False), "2 class calm", "FULLFLEX") is None
    assert find_offer_id({}, "2 class calm", "FULLFLEX") is None


def _booking(status, origin, dest, date, actions=()):
    return {"bookingId": "U", "booking": {
        "bookingNumber": "N", "bookingStatus": status, "possibleActions": list(actions),
        "journeys": [{"segments": [{"departureStation": {"uicStationCode": origin},
                                     "arrivalStation": {"uicStationCode": dest},
                                     "departureDateTime": f"{date}T06:59:00+02:00"}]}]}}


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
    start, end = booking_date_range({"endTravelValidityDateTime": "2027-03-18T01:00:00+01:00"},
                                    start_offset_days=1)
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
    from tests.conftest import base_cfg
    p = base_cfg(date_start="2026-09-01", date_end="2026-10-30", service_types=["SJ_HIGH"])["search_parameters"]
    assert describe_run(p) == [
        "Linköping Central ⇄ Stockholm Central · 1 sep – 30 oct 2026 · weekdays",
        "out 06:59 · back 17:22 · 2 class calm · FULLFLEX · SJ High-speed train",
    ]
    p = base_cfg(roundtrip=False, date_end="2026-09-01", select_closest_ticket_available=False,
                 skip_weekends=False, allow_class_fallback=False, book_partial=True)["search_parameters"]
    assert describe_run(p) == [
        "Linköping Central → Stockholm Central · 1 sep 2026 · every day except red days",
        "out 06:59 · 2 class calm · FULLFLEX · exact time only · no class fallback · partial ok",
    ]
    p = base_cfg(skip_holidays=False, service_types=["ALL"])["search_parameters"]
    assert describe_run(p)[0].endswith("weekdays incl. red days")
    assert describe_run(p)[1] == "out 06:59 · back 17:22 · 2 class calm · FULLFLEX"
