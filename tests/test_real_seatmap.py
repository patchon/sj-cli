"""
Ground truth: the seat logic against a real SJ seat map.

`tests/data/seatmap_x2000.json` is a scrubbed capture of a live response for an
X 2000 (Linköping → Stockholm, 2026-09-28), taken during the reverse-engineering
of the seat endpoints. Names and booking-scoped ids are replaced; every
coordinate, carriage flag and property code is exactly what SJ sent.

The synthetic fixtures in `tests/fakes.py` prove the code does what we *think*
the geometry looks like. This file proves it against what the API actually
sends — the two together are what keep the derived rules (`forward`, `single`)
honest, since neither is a field SJ publishes.
"""

import json
from pathlib import Path

from sj_cli.seats import assigned_seat, best_seat, free_seats, seat_words

SEATMAP = Path(__file__).parent / "data" / "seatmap_x2000.json"


def seatmap() -> dict:
    return json.loads(SEATMAP.read_text())


def every_seat_selectable(data: dict) -> dict:
    """The same map with availability removed, to inspect the layout alone."""
    data["seatsPossibleToSelect"] = {
        str(c["carriageNumber"]): [s["seatNumber"] for s in c["seats"]] for c in data["carriages"]
    }
    return data


def test_the_fixture_carries_no_personal_data():
    """The capture is scrubbed: a real name here would be a leak in the repo."""
    passenger = seatmap()["passengerSeats"][0]
    assert (passenger["firstName"], passenger["lastName"]) == ("Test", "Passenger")
    assert passenger["passengerId"] == "passenger_1"


def test_single_seats_match_the_real_carriage_layouts():
    """
    Carriages 1 and 3 are 2+1 (a pair one side, a single the other); 4-7 are 2+2.

    These counts were derived by hand from the floor plan before the detection
    existed, so they are ground truth for the aisle-gap rule rather than a
    recording of whatever the code happens to do.
    """
    seats = free_seats(every_seat_selectable(seatmap()))
    singles: dict[str, int] = {}
    for seat in seats:
        singles[seat["carriage"]] = singles.get(seat["carriage"], 0) + seat["single"]
    assert singles == {"1": 14, "3": 15, "4": 0, "5": 0, "6": 0, "7": 0}


def test_availability_is_already_filtered_to_the_booked_class():
    """seatsPossibleToSelect holds only 2 klass Lugn seats — carriage 3 alone."""
    seats = free_seats(seatmap())
    assert len(seats) == 26
    assert {s["carriage"] for s in seats} == {"3"}
    assert sorted((s["number"] for s in seats if s["single"]), key=int) == [
        "19",
        "27",
        "35",
        "39",
    ]


def test_ranking_is_best_effort_on_real_data():
    """
    None of the free single seats face forwards on this departure.

    So ["single", "forward"] must return a single seat that faces backwards —
    the higher-ranked wish wins and the caller reports the missed one. A seat
    satisfying both would beat it, and there is none in this map.
    """
    seat = best_seat(seatmap(), ["single", "forward"])
    assert seat is not None
    assert (seat["carriage"], seat["number"]) == ("3", "19")
    assert seat["single"] is True
    assert seat["forward"] is False


def test_the_computed_direction_agrees_with_the_api():
    """
    `forward` is computed (seat.reversed == carriage.reversed) because the map
    publishes no direction on its seats — but the *assigned* seat carries SJ's
    own IDR ("i din riktning") / ODR codes. They must agree, or the rule the
    whole feature rests on is wrong.
    """
    data = seatmap()
    seat = assigned_seat(data)
    assert seat is not None
    codes = [p["code"] for p in data["passengerSeats"][0]["carriageSeatProperties"]]
    assert ("IDR" in codes) is seat["forward"]
    assert ("ODR" in codes) is not seat["forward"]


def test_the_assigned_seat_renders_the_way_the_cards_show_it():
    seat = assigned_seat(seatmap())
    assert seat is not None
    assert seat_words(seat) == ["window", "forward"]
