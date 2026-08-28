"""
Seat selection: the config vocabulary, the seat-map join and the ranking.

Pure logic — no HTTP and no user-facing output. The SJ seat map hands us
``seatsPossibleToSelect`` (free seats, already filtered to the booked comfort
class) plus a ``carriages[].seats[]`` list carrying each seat's properties;
this module joins the two and ranks the result against the user's wish list.
"""

import logging
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

# Config word -> API property code. The direction words and "single" are
# computed (from the map's `reversed` flags and seat geometry respectively)
# rather than read from a property, hence None. "single" is not the same as
# "solo": SOLO is SJ's first-class Singelplats product (a marketed code);
# single is any seat with no neighbour, from a 2+1 carriage's geometry —
# carriage 1 of a real X 2000 has 16 SOLO seats but only 14 geometric
# singles, and carriage 3's singles carry no SOLO marking at all.
SEAT_WORDS: dict[str, str | None] = {
    "window": "WINDOW",
    "aisle": "AISLE",
    "table": "TABLE",
    "solo": "SOLO",
    "easy access": "EASY_ACCESS",
    "no animals": "WITHOUT_ANIMALS",
    "forward": None,
    "backward": None,
    "single": None,
}

# Wishes that cannot both be met by one seat
_CONTRADICTIONS = (("window", "aisle"), ("forward", "backward"))

# API comfort code -> display name. The single source for both the offer
# lookup (booking.find_offer_id) and the seat picker's carriage header, the
# way SERVICE_TYPE_NAMES is the single source for service types.
COMFORT_NAMES: dict[str, str] = {
    "SECOND": "2 class",
    "SECOND_CALM": "2 class calm",
    "FIRST": "1 class",
}

ASK: Literal["ask"] = "ask"

Preference = list[str] | Literal["ask"] | None


class Seat(TypedDict):
    """One selectable seat, joined from the map's two halves."""

    carriage: str
    number: str
    codes: list[str]
    forward: bool
    single: bool


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
        together with everything else. A None preference with a non-empty
        error list means "invalid", not "absent" — that distinction is safe
        only because the config layer aborts the run on any error.

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
        quoted = ", ".join(f'"{w}"' for w in unknown)
        errors.append(f"seat_preference: unknown {quoted}. Valid words: {words}")
    duplicates = sorted({w for w in wishes if wishes.count(w) > 1})
    if duplicates:
        errors.append(f"seat_preference lists {', '.join(duplicates)} twice")
    for a, b in _CONTRADICTIONS:
        if a in wishes and b in wishes:
            errors.append(f"seat_preference cannot ask for both {a} and {b}")
    return (None, errors) if errors else (wishes, [])


def _alone_by(seats: list[dict[str, Any]], field: str) -> set[str]:
    """
    Seat numbers alone on their side of the aisle, judged by one coordinate.

    Works for any field that increases across the carriage, because the aisle
    always shows up as the widest gap between consecutive distinct values:
    `ypos` (drawing coordinates) and `rowPosition` (physical slots, which
    number the aisle too) both qualify. Seats are grouped by (row, side of
    that gap); a group of exactly one seat is a single seat.

    Degenerate input — fewer than two distinct values, so no gap can be
    identified, or no seats at all — yields no singles rather than a guess.
    """
    values = sorted({s[field] for s in seats if isinstance(s.get(field), (int, float))})
    if len(values) < 2:
        return set()
    gaps = [(values[i + 1] - values[i], values[i]) for i in range(len(values) - 1)]
    _, boundary = max(gaps)

    groups: dict[tuple[Any, bool], list[str]] = {}
    for s in seats:
        value = s.get(field)
        number = s.get("seatNumber")
        if not isinstance(value, (int, float)) or number is None:
            continue
        groups.setdefault((s.get("rowNumber"), value > boundary), []).append(str(number))
    return {numbers[0] for numbers in groups.values() if len(numbers) == 1}


def _single_seat_numbers(seats: list[dict[str, Any]]) -> set[str]:
    """
    Seat numbers that sit alone across the aisle, within one carriage.

    X 2000 carriages are laid out either 2+2 or 2+1: on a 2+1 side, one seat
    per row has no neighbour. SJ publishes no property code for this, so it is
    recovered from the map's own geometry (`ypos`) via `_alone_by`.

    `rowPosition` encodes the same layout independently — a 2+1 carriage uses
    positions 1 | 3, 4 (position 2 *is* the aisle), a 2+2 one 1, 2 | 4, 5 — so
    it serves as a cross-check. Both signals agreed on every carriage of every
    train captured so far. Geometry stays authoritative: on a disagreement
    neither field is more trustworthy than the other, and preferring silence
    would hide real single seats. The disagreement is logged instead, so a
    seat that turns out to have a neighbour can be diagnosed with
    LOG_LEVEL=DEBUG rather than guessed at.

    Args:
        seats: one carriage's `seats` list (raw API dicts).

    Returns:
        The `seatNumber`s (as given by the API, not necessarily numeric)
        that are single seats.

    """
    geometry = _alone_by(seats, "ypos")
    by_position = _alone_by(seats, "rowPosition")
    if by_position != geometry:
        logger.debug(
            "single-seat signals disagree: ypos says %s, rowPosition says %s "
            "(using ypos; SJ's map may describe a different unit than the train)",
            sorted(geometry),
            sorted(by_position),
        )
    return geometry


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
    carriages = {
        str(c.get("carriageNumber")): c
        for c in seatmap.get("carriages") or []
        if isinstance(c, dict)
    }
    seats: list[Seat] = []
    for carriage, numbers in (seatmap.get("seatsPossibleToSelect") or {}).items():
        c = carriages.get(str(carriage)) or {}
        c_seats = [s for s in c.get("seats") or [] if isinstance(s, dict)]
        by_number = {str(s.get("seatNumber")): s for s in c_seats}
        singles = _single_seat_numbers(c_seats)
        for number in numbers if isinstance(numbers, list) else []:
            seat = by_number.get(str(number))
            if seat is None:
                continue
            seats.append(
                Seat(
                    carriage=str(carriage),
                    number=str(number),
                    # API nulls a property's code sometimes; drop those rather
                    # than let a None sneak into codes (see satisfies() below).
                    codes=[
                        code
                        for p in seat.get("carriageSeatProperties") or []
                        if isinstance(p, dict) and (code := p.get("code"))
                    ],
                    # IDR/ODR is not in the map: a seat faces forward when its
                    # own reversed flag matches its carriage's (verified
                    # against the live API 2026-08-27).
                    forward=bool(seat.get("reversed")) == bool(c.get("reversed")),
                    single=str(number) in singles,
                )
            )
    return seats


def satisfies(seat: Seat, wish: str) -> bool:
    """Whether one free seat meets one vocabulary wish."""
    if wish == "forward":
        return seat["forward"]
    if wish == "backward":
        return not seat["forward"]
    if wish == "single":
        return seat["single"]
    code = SEAT_WORDS.get(wish)
    # SEAT_WORDS.get() returns None for a non-vocabulary wish and for the
    # computed words above (handled already) — never treat that as a code match.
    return code is not None and code in seat["codes"]


def number_key(value: str) -> tuple[int, int, str]:
    """Numeric carriage/seat numbers sort numerically; anything odd sorts last."""
    if value.isascii() and value.isdigit():
        return (0, int(value), value)
    return (1, 0, value)


def rank(
    seat: Seat, wishes: list[str]
) -> tuple[list[bool], tuple[int, int, str], tuple[int, int, str]]:
    """
    The sort key `best_seat` picks by, exposed so callers can compare seats.

    Ranking is lexicographic: an earlier wish outweighs every later wish
    combined, so ["window", "table"] ranks a plain window seat ahead of an
    aisle seat at a table. Ties break on lowest carriage, then lowest seat
    number, which keeps the choice deterministic.

    Lower sorts better — compare two seats' ranks with `<` to ask "is the
    first strictly better than the second?" This is the only correct way to
    ask that: `best_seat` is best-effort and returns the lowest-numbered free
    seat even when it satisfies no wish at all, so comparing seat identities
    (`best_seat(...) != current`) instead of ranks would flag a seat as an
    improvement when it is merely a different, equally-unsatisfying one — or
    even a worse one the current seat outranks.

    Args:
        seat: the seat to rank.
        wishes: vocabulary words in priority order, as returned by
            `parse_preference` (empty list is fine — every seat ties on
            wishes and the order falls back to carriage/seat number alone).

    Returns:
        A key comparable with `<`/`<=`/etc.

    """
    return (
        [not satisfies(seat, w) for w in wishes],
        number_key(seat["carriage"]),
        number_key(seat["number"]),
    )


def best_seat(seatmap: dict[str, Any], wishes: list[str]) -> Seat | None:
    """
    The best free seat for a ranked wish list, or None when none is selectable.

    Args:
        seatmap: the API's seat-map response for one segment.
        wishes: vocabulary words in priority order, as returned by
            `parse_preference` (empty list is fine — any free seat wins,
            tie-broken by carriage/seat number).

    Returns:
        The best-ranked Seat, or None when no seat is selectable. The pick is
        best-effort: when no free seat satisfies any wish, this still returns
        the top tie-broken seat rather than None — None means only that the
        map has nothing selectable at all.

    Ranking is `rank()`, so a listing that wants to know whether some other
    seat outranks the one a passenger already holds can use the exact same
    comparison this uses to choose one.
    """
    seats = free_seats(seatmap)
    if not seats:
        return None
    return min(seats, key=lambda s: rank(s, wishes))


def current_seat(seatmap: dict[str, Any]) -> tuple[str | None, str | None]:
    """The seat the passenger holds right now, as (carriage, seat)."""
    # [0]: this tool books one passenger per journey, so at most one entry.
    assigned = next((s for s in seatmap.get("passengerSeats") or [] if isinstance(s, dict)), {})
    carriage = assigned.get("carriageNumber")
    number = assigned.get("seatNumber")
    return (
        str(carriage) if carriage is not None else None,
        str(number) if number is not None else None,
    )


def assigned_seat(seatmap: dict[str, Any]) -> Seat | None:
    """
    The passenger's assigned seat, looked up in the carriage layout.

    `free_seats()` joins `seatsPossibleToSelect` against `carriages[].seats[]`
    for every free seat; this does the same join for one specific seat — the
    one `current_seat()` names — so it can carry the same computed
    properties (`single`, `forward`) a --seat-details listing needs but a
    property code alone cannot give it.

    Args:
        seatmap: the API's seat-map response for one segment.

    Returns:
        A full Seat for the assigned seat, or None when the carriage or the
        seat number cannot be found in `carriages[].seats[]` (an unfamiliar
        map shape) — callers fall back to the assigned seat's own
        `carriageSeatProperties` in that case.

    """
    carriage, number = current_seat(seatmap)
    if carriage is None or number is None:
        return None
    c = next(
        (
            c
            for c in seatmap.get("carriages") or []
            if isinstance(c, dict) and str(c.get("carriageNumber")) == carriage
        ),
        None,
    )
    if c is None:
        return None
    c_seats = [s for s in c.get("seats") or [] if isinstance(s, dict)]
    seat = next((s for s in c_seats if str(s.get("seatNumber")) == number), None)
    if seat is None:
        return None
    singles = _single_seat_numbers(c_seats)
    return Seat(
        carriage=carriage,
        number=number,
        codes=[
            code
            for p in seat.get("carriageSeatProperties") or []
            if isinstance(p, dict) and (code := p.get("code"))
        ],
        forward=bool(seat.get("reversed")) == bool(c.get("reversed")),
        single=number in singles,
    )


def carriage_comfort(seatmap: dict[str, Any], carriage: str) -> str:
    """
    The display comfort of one carriage, or "" when the map does not say.

    There is no `comfortDescription` field: comfort lives in
    `carriages[].carriageComforts` (e.g. ["SECOND_CALM"]). A carriage can
    list several codes (["SECOND_WHEELCHAIR", "SECOND"]) — the first one we
    have a name for wins. When the carriage says nothing we recognise, fall
    back to the class the passenger is booked in
    (`passengerSeats[].inventoryClass`). An unrecognised code renders as
    nothing rather than as a raw API word.
    """
    for c in seatmap.get("carriages") or []:
        if isinstance(c, dict) and str(c.get("carriageNumber")) == str(carriage):
            for code in c.get("carriageComforts") or []:
                if code in COMFORT_NAMES:
                    return COMFORT_NAMES[code]
            break
    assigned = next((s for s in seatmap.get("passengerSeats") or [] if isinstance(s, dict)), {})
    return COMFORT_NAMES.get(str(assigned.get("inventoryClass")), "")


_WORD_BY_CODE = {code: word for word, code in SEAT_WORDS.items() if code}

# The seat map does not carry IDR/ODR on `carriages[].seats[]` (free_seats()
# computes forward/backward from the reversed flags instead), but it does put
# one of the two directly on `passengerSeats[0].carriageSeatProperties` — the
# one seat that map is scoped to. assigned_seat_words() reads it from there.
_DIRECTION_WORD_BY_CODE = {"IDR": "forward", "ODR": "backward"}


def seat_words(seat: Seat) -> list[str]:
    """
    A seat's properties as config vocabulary, for display. Unknown codes are skipped.

    "single" (computed, not a code) sorts in alphabetically with the code
    words — between "no animals" and "solo" — same as everywhere else in the
    vocabulary; direction stays last.
    """
    words = {_WORD_BY_CODE[c] for c in seat["codes"] if c in _WORD_BY_CODE}
    if seat["single"]:
        words.add("single")
    return [*sorted(words), "forward" if seat["forward"] else "backward"]


def assigned_seat_words(codes: list[str]) -> list[str]:
    """
    Display words for an assigned seat's property codes, for --seat-details.

    Fallback path only: used when `assigned_seat()` cannot find the seat in
    the carriage layout. Reuses SEAT_WORDS as the vocabulary (WINDOW, AISLE,
    TABLE, SOLO, EASY_ACCESS, WITHOUT_ANIMALS) plus the two direction codes a
    seat map gives directly for the assigned seat (IDR -> forward, ODR ->
    backward; see the module note above). SEAT_STD ("standard seat" — no
    information) and any code this tool has no word for are skipped.
    Ordered exactly like seat_words() — alphabetical, direction last — so a
    free seat's rendering and an assigned seat's rendering never disagree.
    Codes carry no "single" information, so this path never reports it —
    only the carriage-layout lookup can.

    Args:
        codes: property codes as given by the seat map, e.g.
            ["IDR", "TABLE", "WINDOW"].

    Returns:
        Vocabulary words, e.g. ["table", "window", "forward"]. Empty when no
        code is recognised (including an all-unknown or empty input).

    """
    words = sorted({_WORD_BY_CODE[c] for c in codes if c in _WORD_BY_CODE})
    for code in codes:
        if code in _DIRECTION_WORD_BY_CODE:
            words.append(_DIRECTION_WORD_BY_CODE[code])
            break
    return words


def describe_seat(seat: Seat) -> str:
    """'carriage 3 seat 70 · table, window, forward'."""
    return f"carriage {seat['carriage']} seat {seat['number']} · {', '.join(seat_words(seat))}"
