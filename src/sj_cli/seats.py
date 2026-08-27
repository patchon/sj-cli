"""
Seat selection: the config vocabulary, the seat-map join and the ranking.

Pure logic — no HTTP and no printing. The SJ seat map hands us
``seatsPossibleToSelect`` (free seats, already filtered to the booked comfort
class) plus a ``carriages[].seats[]`` list carrying each seat's properties;
this module joins the two and ranks the result against the user's wish list.
"""

from typing import Any, Literal, TypedDict

# Config word -> API property code. The two direction words are computed from
# the map's `reversed` flags rather than read from a property, hence None.
SEAT_WORDS: dict[str, str | None] = {
    "window": "WINDOW",
    "aisle": "AISLE",
    "table": "TABLE",
    "solo": "SOLO",
    "easy access": "EASY_ACCESS",
    "no animals": "WITHOUT_ANIMALS",
    "forward": None,
    "backward": None,
}

# Wishes that cannot both be met by one seat
_CONTRADICTIONS = (("window", "aisle"), ("forward", "backward"))

ASK: Literal["ask"] = "ask"

Preference = list[str] | Literal["ask"] | None


class Seat(TypedDict):
    """One selectable seat, joined from the map's two halves."""

    carriage: str
    number: str
    codes: list[str]
    forward: bool


def _word(value: str) -> str:
    """Normalise a config word: lowercase, single spaces."""
    return " ".join(value.lower().split())


def parse_preference(value: Any) -> tuple[Preference, list[str]]:
    """
    Validate the `seat_preference` config value.

    Args:
        value: the raw `seat_preference` config value — expected to be the
            string "ask", a list of vocabulary words, or absent (None).

    Returns:
        (preference, errors) — the preference is the literal "ask", a
        normalised list of vocabulary words, or None when the key is absent.
        Errors are collected, never raised, so config.py can report them
        together with everything else.

    """
    if value is None:
        return None, []

    words = ", ".join(sorted(SEAT_WORDS))
    if isinstance(value, str):
        if _word(value) == ASK:
            return ASK, []
        return None, [f'seat_preference must be "ask" or a list of: {words}']

    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, [f'seat_preference must be "ask" or a list of: {words}']
    if not value:
        return None, ["seat_preference is empty — omit the key to let SJ assign the seat"]

    wishes = [_word(v) for v in value]
    errors: list[str] = []
    unknown = [w for w in wishes if w not in SEAT_WORDS]
    if unknown:
        errors.append(f"seat_preference: unknown {', '.join(unknown)}. Valid words: {words}")
    duplicates = sorted({w for w in wishes if wishes.count(w) > 1})
    if duplicates:
        errors.append(f"seat_preference lists {', '.join(duplicates)} twice")
    for a, b in _CONTRADICTIONS:
        if a in wishes and b in wishes:
            errors.append(f"seat_preference cannot ask for both {a} and {b}")
    return (None, errors) if errors else (wishes, [])


def free_seats(seatmap: dict[str, Any]) -> list[Seat]:
    """
    Every selectable seat in the map, with its properties and facing.

    Args:
        seatmap: the API's seat-map response for one segment.

    Returns:
        One Seat per entry in `seatsPossibleToSelect`, in that dict's
        iteration order, joined against `carriages[].seats[]` for the
        seat's properties and facing. A listed seat missing from the
        carriage detail is skipped.

    """
    carriages = {str(c.get("carriageNumber")): c for c in seatmap.get("carriages") or []}
    seats: list[Seat] = []
    for carriage, numbers in (seatmap.get("seatsPossibleToSelect") or {}).items():
        c = carriages.get(str(carriage)) or {}
        by_number = {str(s.get("seatNumber")): s for s in c.get("seats") or []}
        for number in numbers or []:
            seat = by_number.get(str(number))
            if seat is None:
                continue
            seats.append(
                Seat(
                    carriage=str(carriage),
                    number=str(number),
                    codes=[p.get("code", "") for p in seat.get("carriageSeatProperties") or []],
                    # IDR/ODR is not in the map: a seat faces forward when its
                    # own reversed flag matches its carriage's (verified
                    # against the live API 2026-08-27).
                    forward=bool(seat.get("reversed")) == bool(c.get("reversed")),
                )
            )
    return seats


def _satisfies(seat: Seat, wish: str) -> bool:
    if wish == "forward":
        return seat["forward"]
    if wish == "backward":
        return not seat["forward"]
    return SEAT_WORDS.get(wish) in seat["codes"]


def _number_key(value: str) -> tuple[int, int, str]:
    """Numeric seats sort numerically; anything odd sorts last, alphabetically."""
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def best_seat(seatmap: dict[str, Any], wishes: list[str]) -> Seat | None:
    """
    The best free seat for a ranked wish list, or None when none is selectable.

    Args:
        seatmap: the API's seat-map response for one segment.
        wishes: vocabulary words in priority order, as returned by
            `parse_preference` (empty list is fine — any free seat wins,
            tie-broken by carriage/seat number).

    Returns:
        The best-ranked Seat, or None when no seat is selectable.

    Ranking is lexicographic: an earlier wish outweighs every later wish
    combined, so ["window", "table"] takes a plain window seat over an aisle
    seat at a table. Ties break on lowest carriage, then lowest seat number,
    which keeps the choice deterministic.
    """
    seats = free_seats(seatmap)
    if not seats:
        return None
    return min(
        seats,
        key=lambda s: (
            [not _satisfies(s, w) for w in wishes],
            _number_key(s["carriage"]),
            _number_key(s["number"]),
        ),
    )


def current_seat(seatmap: dict[str, Any]) -> tuple[str | None, str | None]:
    """The seat the passenger holds right now, as (carriage, seat)."""
    assigned = (seatmap.get("passengerSeats") or [{}])[0]
    carriage = assigned.get("carriageNumber")
    number = assigned.get("seatNumber")
    return (str(carriage) if carriage else None, str(number) if number else None)


def seat_words(seat: Seat) -> list[str]:
    """A seat's properties as config vocabulary, for display. Unknown codes are skipped."""
    by_code = {code: word for word, code in SEAT_WORDS.items() if code}
    words = sorted(by_code[c] for c in seat["codes"] if c in by_code)
    return [*words, "forward" if seat["forward"] else "backward"]


def describe_seat(seat: Seat) -> str:
    """'carriage 3 seat 70 · table, window, forward'."""
    return f"carriage {seat['carriage']} seat {seat['number']} · {', '.join(seat_words(seat))}"
