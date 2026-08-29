from datetime import datetime

import pytest

from sj_cli.booking import (
    _dry_run_note,
    _run_outcome,
    _segment_to_display_row,
    _span_label,
    booking_date_range,
    check_comfort_availability,
    check_existing_booking,
    describe_run,
    drop_departed,
    find_offer_id,
    get_departure_time_minutes,
    is_stale_provisional,
    resolve_class_for_departure,
    select_best_departure,
    time_str_to_minutes,
)
from sj_cli.dates import sweden_now, to_sweden
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


def test_resolve_class_fallback_chain():
    d_b = dep("x", D, "06:00", "07:00", props=("COMFORT-B",))
    assert resolve_class_for_departure(d_b, "2 class calm", allow_fallback=True) == "2 class"
    assert resolve_class_for_departure(d_b, "2 class calm", allow_fallback=False) is None
    assert resolve_class_for_departure(d_b, "1 class", allow_fallback=True) == "2 class"
    d_ab = dep("x", D, "06:00", "07:00", props=("COMFORT-AB",))
    assert resolve_class_for_departure(d_ab, "1 class", allow_fallback=False) == "1 class"


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

    p = base_cfg(dates="2026-09-01..2026-10-30", service_types=["SJ_HIGH"])["search_parameters"]
    assert describe_run(p) == [
        ("route", "Göteborg Central ⇄ Stockholm Central"),
        ("days", "1 sep – 30 oct 2026 · weekdays only"),
        ("times", "out 06:59 · back 17:22"),
        ("ticket", "2 class calm · FULLFLEX · SJ High-speed train"),
    ]
    p = base_cfg(
        roundtrip=False,
        dates="2026-09-01",
        select_closest_ticket_available=False,
        skip_weekends=False,
        allow_class_fallback=False,
        book_partial=True,
    )["search_parameters"]
    assert describe_run(p) == [
        ("route", "Göteborg Central → Stockholm Central"),
        ("days", "1 sep 2026 · every day except red days"),
        ("times", "out 06:59"),
        ("ticket", "2 class calm · FULLFLEX · exact time only · no class fallback · partial ok"),
    ]
    p = base_cfg(skip_holidays=False, service_types=["ALL"])["search_parameters"]
    label, value = describe_run(p)[1]
    assert label == "days"
    assert value.endswith("weekdays incl. red days")
    assert describe_run(p)[3] == ("ticket", "2 class calm · FULLFLEX")
    # non-contiguous: the selection as validation normalised it, then its span
    from datetime import date

    from sj_cli.config import CfgManager

    cfg = base_cfg(dates=" w43 ,w45..46")
    CfgManager().verify_cfg(cfg)  # normalises the written value in place
    assert describe_run(cfg["search_parameters"], today=date(2026, 8, 23))[1] == (
        "days",
        "W43, W45..46 (19 oct – 15 nov 2026) · weekdays only",
    )


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
    from sj_cli.booking import _segment_date

    assert _segment_date("2026-10-24T22:30:00Z") == "2026-10-25"  # 00:30 Swedish
    assert _segment_date("2026-09-01T06:59:00+02:00") == "2026-09-01"
    assert _segment_date("2026-09-01") == "2026-09-01"  # unparsable: raw date part


def test_duplicate_check_matches_a_journey_with_a_change():
    from sj_cli.booking import check_existing_booking

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


# --- explicit JSON nulls are as good as a missing key -----------------------


def test_display_row_and_availability_survive_explicit_nulls():
    from datetime import datetime

    from sj_cli.booking import (
        _journey_endpoints,
        _segment_to_display_row,
        is_active_booking,
    )
    from sj_cli.dates import SWEDEN

    seg = {
        "direction": "OUTBOUND",
        "departureDateTime": f"{D}T06:59:00+02:00",
        "arrivalDateTime": f"{D}T11:36:00+02:00",
        "productFamily": None,
        "departureStation": None,
        "arrivalStation": None,
        "serviceType": None,
        "requiredProducts": None,
    }
    row = _segment_to_display_row(seg, "N", datetime(2026, 1, 1, tzinfo=SWEDEN))
    assert (row["route"], row["comfort_class"], row["train"], row["seat"]) == (
        "— → —",
        "—",
        "—",
        "—",
    )
    assert _journey_endpoints({"segments": None}) == (None, None, "")
    assert _journey_endpoints({"segments": [{**seg, "departureStation": None}]})[0] is None
    assert not check_comfort_availability({"legs": None}, "2 class")
    assert not check_comfort_availability({"legs": [{"serviceProperties": None}]}, "2 class")
    assert not is_stale_provisional({"bookingStatus": "NEW", "possibleActions": None})
    assert is_active_booking({"bookingStatus": "NEW", "possibleActions": None})
    assert find_offer_id({"seatOffers": None}, "2 class calm", "FULLFLEX") is None
    assert find_offer_id({"seatOffers": {"offers": None}}, "2 class calm", "FULLFLEX") is None
    assert check_existing_booking([{"booking": {"journeys": None}}], "1", "2", D) is False


def test_select_best_departure_survives_null_legs():
    d = dep("a", D, "06:59", "11:36")
    d["legs"] = None
    assert select_best_departure([d], "06:59", "2 class calm", select_closest=True) is None


def test_run_outcome_is_green_only_when_the_run_booked_something():
    # green: state changed
    assert _run_outcome({"days": 2, "booked": 2}, dry_run=False) is True
    assert _run_outcome({"days": 1, "partial": 1}, dry_run=False) is True
    # dim: ran fine, changed nothing
    assert _run_outcome({"days": 3, "already": 2, "skipped": 1}, dry_run=False) is None
    assert _run_outcome({"days": 1, "unavailable": 1}, dry_run=False) is None
    assert _run_outcome({"days": 2, "booked": 2}, dry_run=True) is None
    # red: a failure outranks everything, dry run included
    assert _run_outcome({"days": 2, "booked": 1, "failed": 1}, dry_run=False) is False
    assert _run_outcome({"days": 1, "error": 1}, dry_run=True) is False


def test_segment_label_names_the_return_leg_and_disambiguates_a_change():
    from sj_cli.booking import _segment_label

    out = {"direction": "OUTBOUND", "publicServiceName": "520"}
    ret = {"direction": "INBOUND", "publicServiceName": "543"}
    assert _segment_label(out, [out, ret]) == "outbound"
    assert _segment_label(ret, [out, ret]) == "return"

    # a leg with a change runs two segments in one direction: the train number
    # is what tells the two prompts and warnings apart
    first = {"direction": "OUTBOUND", "publicServiceName": "520"}
    second = {"direction": "OUTBOUND", "serviceName": "1064"}
    assert _segment_label(first, [first, second]) == "outbound 520"
    assert _segment_label(second, [first, second]) == "outbound 1064"

    # neither name given: the label must not end in a stray space
    bare = {"direction": "INBOUND"}
    assert _segment_label(bare, [bare, dict(bare)]) == "return"


def _segment(family):
    return {
        "direction": "OUTBOUND",
        "departureDateTime": "2026-09-01T06:59:00+02:00",
        "arrivalDateTime": "2026-09-01T11:36:00+02:00",
        "productFamily": family,
        "departureStation": {"name": "Göteborg Central"},
        "arrivalStation": {"name": "Stockholm Central"},
    }


def test_booked_row_takes_class_and_flexibility_from_the_codes():
    row = _segment_to_display_row(
        _segment(
            {
                # the name deliberately disagrees with the codes: the codes win
                "name": "1 klass, Kan ej ombokas",
                "salesCategoryComfort": "SECOND_CALM",
                "salesCategoryFlexibility": "SEMIFLEX",
            }
        ),
        "NUM1",
        sweden_now(),
    )
    assert (row["comfort_class"], row["flexibility"]) == ("2 class calm", "SEMIFLEX")


def test_booked_row_translates_the_name_when_the_codes_are_missing():
    row = _segment_to_display_row(
        _segment({"name": "1 klass, Kan ej ombokas"}), "NUM1", sweden_now()
    )
    assert (row["comfort_class"], row["flexibility"]) == ("1 class", "NOFLEX")
    row = _segment_to_display_row(_segment(None), "NUM1", sweden_now())
    assert (row["comfort_class"], row["flexibility"]) == ("—", "")


def test_drop_departed_keeps_now_and_the_unparsable():
    now = to_sweden("2026-09-01T09:00:00+02:00")
    gone = dep("gone", "2026-09-01", "08:59", "10:30")
    at_now = dep("now", "2026-09-01", "09:00", "10:40")
    later = dep("later", "2026-09-01", "09:01", "10:45")
    odd = {"departureId": "odd", "departureDateTime": "soon"}
    kept, dropped = drop_departed([gone, at_now, later, odd, {}], now)
    assert [d.get("departureId") for d in kept] == ["now", "later", "odd", None]
    assert dropped == 1
    assert drop_departed([], now) == ([], 0)
    with pytest.raises(TypeError):  # a naive now must raise, not disable the filter
        drop_departed([later], datetime(2026, 9, 1, 9, 0))
