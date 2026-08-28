from sj_cli.seats import (
    _single_seat_numbers,
    assigned_seat,
    assigned_seat_words,
    best_seat,
    current_seat,
    describe_seat,
    free_seats,
    parse_preference,
    satisfies,
    seat_words,
)


def _carriage_dict(number, seat_specs, reversed_):
    # A spec is (number, codes, reversed) or, when a test needs real carriage
    # geometry for single-seat detection, (number, codes, reversed, row, ypos).
    def _seat(spec):
        n, codes, rev, *geometry = spec
        row_number = geometry[0] if len(geometry) > 0 else None
        ypos = geometry[1] if len(geometry) > 1 else None
        seat = {
            "seatNumber": n,
            "reversed": rev,
            "carriageSeatProperties": [{"code": c} for c in codes],
        }
        if row_number is not None:
            seat["rowNumber"] = row_number
        if ypos is not None:
            seat["ypos"] = ypos
        return seat

    return {
        "carriageNumber": number,
        "reversed": reversed_,
        "seats": [_seat(spec) for spec in seat_specs],
    }


def seatmap(
    seats,
    selectable,
    carriage_reversed=True,
    assigned=("3", "50"),
    carriage="3",
    extra_carriages=(),
):
    """
    Minimal seat map: seats is [(number, [codes], reversed)].

    extra_carriages: additional carriages as (carriage_number, seats, selectable,
    reversed) tuples, for tests that need more than one carriage.
    """
    carriages = [_carriage_dict(carriage, seats, carriage_reversed)]
    possible = {carriage: list(selectable)}
    for number, extra_seats, extra_selectable, rev in extra_carriages:
        carriages.append(_carriage_dict(number, extra_seats, rev))
        possible[number] = list(extra_selectable)

    return {
        "carriages": carriages,
        "seatsPossibleToSelect": possible,
        "passengerSeats": [{"carriageNumber": assigned[0], "seatNumber": assigned[1]}],
        "canChangeSeat": True,
        "hasDeparted": False,
    }


def test_parse_preference_accepts_ask_and_word_lists():
    assert parse_preference("ask") == ("ask", [])
    assert parse_preference("  ASK  ") == ("ask", [])
    assert parse_preference(["Window", "easy  access"]) == (["window", "easy access"], [])
    assert parse_preference(None) == (None, [])


def test_parse_preference_rejects_bad_values():
    _, errors = parse_preference("sometimes")
    assert errors and "ask" in errors[0]
    preference, errors = parse_preference(["kitchen"])
    assert errors and "kitchen" in errors[0] and "window" in errors[0]
    assert preference is None
    _, errors = parse_preference([])
    assert errors and "omit" in errors[0]
    _, errors = parse_preference(["window", "aisle"])
    assert errors and "window" in errors[0] and "aisle" in errors[0]
    _, errors = parse_preference(["forward", "backward"])
    assert errors
    _, errors = parse_preference(["window", "window"])
    assert errors and "twice" in errors[0]
    _, errors = parse_preference(42)
    assert errors


def test_free_seats_only_lists_selectable_seats_with_direction():
    m = seatmap(
        [("14", ["WINDOW"], True), ("17", ["AISLE"], False), ("70", ["TABLE", "WINDOW"], True)],
        selectable=["14", "70"],
    )
    seats = free_seats(m)
    assert [s["number"] for s in seats] == ["14", "70"]
    # carriage reversed=True, so a reversed seat faces forward (IDR)
    assert all(s["forward"] for s in seats)
    assert seats[1]["codes"] == ["TABLE", "WINDOW"]


def test_free_seats_computes_backward_when_seat_and_carriage_disagree():
    m = seatmap([("19", ["WINDOW"], False)], selectable=["19"])
    assert free_seats(m)[0]["forward"] is False


def test_free_seats_forward_follows_both_flags_not_just_the_seat():
    m = seatmap(
        [("14", [], False), ("15", [], True)], selectable=["14", "15"], carriage_reversed=False
    )
    assert [s["forward"] for s in free_seats(m)] == [True, False]


def test_free_seats_skips_seat_missing_from_carriage_detail():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14", "99"])
    assert [s["number"] for s in free_seats(m)] == ["14"]


def test_free_seats_yields_nothing_for_unknown_carriage_key():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["seatsPossibleToSelect"] = {"99": ["14"]}
    assert free_seats(m) == []


def test_free_seats_joins_int_carriage_and_seat_numbers():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["carriages"][0]["carriageNumber"] = 3
    m["carriages"][0]["seats"][0]["seatNumber"] = 14
    m["seatsPossibleToSelect"] = {"3": [14]}
    seats = free_seats(m)
    assert (seats[0]["carriage"], seats[0]["number"]) == ("3", "14")


def test_free_seats_drops_null_and_missing_property_codes():
    m = seatmap([("14", [], True)], selectable=["14"])
    m["carriages"][0]["seats"][0]["carriageSeatProperties"] = [{"code": None}, {}]
    assert free_seats(m)[0]["codes"] == []


def test_free_seats_tolerates_malformed_carriages_list():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["carriages"] = [None, *m["carriages"]]
    assert [s["number"] for s in free_seats(m)] == ["14"]


def test_free_seats_tolerates_null_entry_in_seats_list():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["carriages"][0]["seats"].insert(0, None)
    assert [s["number"] for s in free_seats(m)] == ["14"]


def test_free_seats_tolerates_null_entry_in_carriage_seat_properties():
    m = seatmap([("14", [], True)], selectable=["14"])
    m["carriages"][0]["seats"][0]["carriageSeatProperties"] = [None, {"code": "WINDOW"}]
    assert free_seats(m)[0]["codes"] == ["WINDOW"]


def test_free_seats_tolerates_non_list_selectable_value():
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["seatsPossibleToSelect"] = {"3": 14}
    assert free_seats(m) == []


def test_free_seats_detects_single_seats_in_a_two_plus_one_carriage():
    # Row 1 of a 2+1 carriage: seat 11 sits alone on one side of the aisle
    # (the widest gap between the carriage's distinct ypos values, 5 and
    # {108, 144}); seats 13/14 are the paired seats on the other side.
    m = seatmap(
        [
            ("11", ["WINDOW"], True, 1, 5),
            ("13", ["AISLE"], True, 1, 108),
            ("14", ["WINDOW"], True, 1, 144),
        ],
        selectable=["11", "13", "14"],
    )
    singles = {s["number"]: s["single"] for s in free_seats(m)}
    assert singles == {"11": True, "13": False, "14": False}


def test_free_seats_finds_no_singles_in_a_two_plus_two_carriage():
    # Two pairs, one on each side of the aisle (the widest gap is between
    # 40 and 108): no seat is ever alone in its (row, side) group.
    m = seatmap(
        [
            ("1", ["WINDOW"], True, 1, 5),
            ("2", ["AISLE"], True, 1, 40),
            ("3", ["AISLE"], True, 1, 108),
            ("4", ["WINDOW"], True, 1, 144),
        ],
        selectable=["1", "2", "3", "4"],
    )
    assert not any(s["single"] for s in free_seats(m))


def test_single_seat_numbers_treats_one_ypos_value_as_no_aisle():
    # A single distinct ypos value means no gap can be found, so no aisle
    # can be identified — never guess one from a single data point.
    assert _single_seat_numbers([{"seatNumber": "1", "rowNumber": 1, "ypos": 5}]) == set()
    assert (
        _single_seat_numbers(
            [
                {"seatNumber": "1", "rowNumber": 1, "ypos": 5},
                {"seatNumber": "2", "rowNumber": 2, "ypos": 5},
            ]
        )
        == set()
    )


def test_single_seat_numbers_on_no_seats_finds_nothing():
    assert _single_seat_numbers([]) == set()


def test_satisfies_and_best_seat_honour_single():
    m = seatmap(
        [
            ("11", ["WINDOW"], True, 1, 5),
            ("13", ["AISLE"], True, 1, 108),
            ("14", ["WINDOW"], True, 1, 144),
        ],
        selectable=["11", "13", "14"],
    )
    seats = {s["number"]: s for s in free_seats(m)}
    assert satisfies(seats["11"], "single") is True
    assert satisfies(seats["13"], "single") is False
    # "single" ranks like any other wish: an earlier wish outweighs a later one.
    assert best_seat(m, ["single"])["number"] == "11"
    assert best_seat(m, ["aisle", "single"])["number"] == "13"


def test_best_seat_prefers_an_earlier_wish_over_every_later_one():
    m = seatmap(
        [("14", ["WINDOW"], True), ("69", ["AISLE", "TABLE"], True)],
        selectable=["14", "69"],
    )
    # window alone beats aisle+table, because "window" is ranked first
    assert best_seat(m, ["window", "table"])["number"] == "14"
    assert best_seat(m, ["table", "window"])["number"] == "69"


def test_best_seat_breaks_ties_on_the_lowest_seat_number():
    m = seatmap(
        [("70", ["TABLE", "WINDOW"], True), ("9", ["TABLE", "WINDOW"], True)],
        selectable=["70", "9"],
    )
    assert best_seat(m, ["window"])["number"] == "9"


def test_best_seat_breaks_ties_on_the_lowest_carriage_number():
    # Identical window seats in carriages "10" and "2": numeric ordering must
    # put "2" first (a naive string sort would put "10" first) and the
    # carriage level must be consulted before the tied seat number is.
    m = seatmap(
        [("14", ["WINDOW"], True)],
        selectable=["14"],
        carriage="10",
        extra_carriages=[("2", [("14", ["WINDOW"], True)], ["14"], True)],
    )
    assert best_seat(m, ["window"])["carriage"] == "2"


def test_best_seat_with_no_wishes_returns_lowest_numbered_free_seat():
    m = seatmap(
        [("70", ["WINDOW"], True), ("9", ["AISLE"], True)],
        selectable=["70", "9"],
    )
    assert best_seat(m, [])["number"] == "9"


def test_best_seat_is_best_effort_when_no_wish_is_satisfiable():
    m = seatmap([("14", ["AISLE"], True)], selectable=["14"])
    assert best_seat(m, ["window"])["number"] == "14"


def test_best_seat_honours_direction_wishes():
    m = seatmap(
        [("14", ["WINDOW"], True), ("19", ["WINDOW"], False)],
        selectable=["14", "19"],
    )
    assert best_seat(m, ["forward"])["number"] == "14"
    assert best_seat(m, ["backward"])["number"] == "19"


def test_best_seat_returns_none_when_nothing_is_selectable():
    assert best_seat(seatmap([("14", ["WINDOW"], True)], selectable=[]), ["window"]) is None


def test_current_seat_and_describe_seat():
    m = seatmap([("70", ["TABLE", "WINDOW"], True)], selectable=["70"], assigned=("3", "50"))
    assert current_seat(m) == ("3", "50")
    assert describe_seat(free_seats(m)[0]) == "carriage 3 seat 70 · table, window, forward"


def test_current_seat_with_no_assigned_seat_returns_none_none():
    # SJ has not assigned a seat yet: absent key, empty list, and a null
    # entry in the list must all resolve the same way.
    m = seatmap([("14", ["WINDOW"], True)], selectable=["14"])
    m["passengerSeats"] = []
    assert current_seat(m) == (None, None)
    del m["passengerSeats"]
    assert current_seat(m) == (None, None)
    m["passengerSeats"] = [None]
    assert current_seat(m) == (None, None)


def test_assigned_seat_finds_it_in_the_carriage_layout():
    m = seatmap(
        [("11", ["WINDOW"], True, 1, 5), ("13", ["AISLE"], True, 1, 108)],
        selectable=["11", "13"],
        assigned=("3", "11"),
    )
    seat = assigned_seat(m)
    assert seat is not None
    assert (seat["carriage"], seat["number"], seat["single"]) == ("3", "11", True)
    assert seat_words(seat) == ["single", "window", "forward"]


def test_assigned_seat_returns_none_when_seat_missing_from_carriage_detail():
    # The passenger's seat number is not among this carriage's seats — an
    # unfamiliar/incomplete map, not a crash. Callers fall back to
    # carriageSeatProperties on the assigned entry itself.
    m = seatmap([("11", ["WINDOW"], True, 1, 5)], selectable=["11"], assigned=("3", "99"))
    assert assigned_seat(m) is None


def test_assigned_seat_returns_none_when_carriage_is_missing():
    m = seatmap([("11", ["WINDOW"], True, 1, 5)], selectable=["11"], assigned=("9", "11"))
    assert assigned_seat(m) is None


def test_seat_words_skips_unknown_property_code():
    m = seatmap([("14", ["WINDOW", "SOME_UNKNOWN_CODE"], True)], selectable=["14"])
    assert seat_words(free_seats(m)[0]) == ["window", "forward"]


def test_seat_words_places_single_between_no_animals_and_solo():
    m = seatmap(
        [
            ("11", ["WINDOW", "WITHOUT_ANIMALS", "SOLO"], True, 1, 5),
            ("13", ["AISLE"], True, 1, 108),
            ("14", ["WINDOW"], True, 1, 144),
        ],
        selectable=["11", "13", "14"],
    )
    seat_11 = next(s for s in free_seats(m) if s["number"] == "11")
    assert seat_words(seat_11) == ["no animals", "single", "solo", "window", "forward"]


def test_assigned_seat_words_orders_like_seat_words():
    assert assigned_seat_words(["IDR", "TABLE", "WINDOW"]) == ["table", "window", "forward"]
    assert assigned_seat_words(["ODR", "AISLE"]) == ["aisle", "backward"]


def test_assigned_seat_words_skips_seat_std_and_unknown_codes():
    assert assigned_seat_words(["SEAT_STD"]) == []
    assert assigned_seat_words(["SOME_UNKNOWN_CODE"]) == []
    assert assigned_seat_words([]) == []
    assert assigned_seat_words(["SEAT_STD", "WINDOW", "SOME_UNKNOWN_CODE"]) == ["window"]


def test_assigned_seat_words_with_no_direction_code_omits_direction():
    assert assigned_seat_words(["TABLE"]) == ["table"]
