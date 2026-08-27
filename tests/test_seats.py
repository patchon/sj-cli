from sj_cli.seats import best_seat, current_seat, describe_seat, free_seats, parse_preference


def seatmap(seats, selectable, carriage_reversed=True, assigned=("3", "50")):
    """Minimal seat map: seats is [(number, [codes], reversed)]."""
    return {
        "carriages": [
            {
                "carriageNumber": "3",
                "reversed": carriage_reversed,
                "seats": [
                    {
                        "seatNumber": n,
                        "reversed": rev,
                        "carriageSeatProperties": [{"code": c} for c in codes],
                    }
                    for n, codes, rev in seats
                ],
            }
        ],
        "seatsPossibleToSelect": {"3": list(selectable)},
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
    _, errors = parse_preference(["kitchen"])
    assert errors and "kitchen" in errors[0] and "window" in errors[0]
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
