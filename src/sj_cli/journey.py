"""The interactive journey mode (--book-journey): questions, pick lists, then the Cart."""

import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Literal, NamedTuple

from sj_cli.booking import (
    Cart,
    Leg,
    _class_has_seats,
    _match_departure,
    _release_leg,
    booked_rows,
    booking_date_range,
    describe_departure,
    drop_departed,
    fetch_bookings_with_spinner,
    get_departure_time_minutes,
    is_active_booking,
    pass_validity,
    poll_departures,
    resolve_class_for_departure,
    resolve_offer,
    search,
    time_str_to_minutes,
    train_name,
)
from sj_cli.client import SJClient
from sj_cli.dates import sweden_now, to_sweden
from sj_cli.errors import error_text
from sj_cli.output import (
    ask_optional,
    blank,
    confirm,
    departure_choice_lines,
    indented,
    pdim,
    print_day_cards,
    print_day_header,
    pstatus,
    pwarn,
    select_filtered,
    select_list,
    spinner,
)
from sj_cli.seats import COMFORT_CODES
from sj_cli.stations import Station, StationIndex, parse_stations

logger = logging.getLogger(__name__)


# --- the questions ---------------------------------------------------------------


def _ask_date(
    label: str,
    default: date,
    earliest: date,
    too_early: str,
    valid: tuple[date | None, date | None],
) -> date | None:
    """
    Ask for a date; Enter keeps `default`, Ctrl-D returns None.

    Re-asks until the answer is a YYYY-MM-DD date not before `earliest`
    (`too_early` is the complaint) and inside the pass validity `valid`
    (an unknown bound is not checked).

    `default` is clamped into the pass validity when it falls outside it —
    a pass bought ahead starts after today, and an unreachable default
    would make Enter a dead key.
    """
    first, last = valid
    if first is not None and default < first:
        default = first
    if last is not None and default > last:
        default = last
    while True:
        answer = ask_optional(f"{label} [{default.isoformat()}]: ")
        if answer is None:
            return None
        answer = answer.strip()
        if not answer:
            chosen = default
        else:
            try:
                chosen = date.fromisoformat(answer)
            except ValueError:
                pwarn("not a date, use YYYY-MM-DD")
                continue
        if chosen < earliest:
            pwarn(too_early)
            continue
        if (first is not None and chosen < first) or (last is not None and chosen > last):
            pwarn(f"the pass is valid {first or '?'} – {last or '?'}")
            continue
        return chosen


def _ask_station(
    label: str, default: Station | None, index: StationIndex, other: Station | None
) -> Station | None:
    """Pick a station by typing; Enter keeps `default`; None on Esc/Ctrl-D. Refuses `other`."""
    while True:
        chosen = select_filtered(label, default, index.match, lambda s: s["name"])
        if chosen is None:
            return None
        if other is not None and chosen["code"] == other["code"]:
            pwarn("from and to are the same station")
            default = None  # a refused default must not be offered again
            continue
        return chosen


def _ask_yes_no(question: str, default: bool) -> bool | None:
    """A [Y/n]/[y/N] question: Enter is the default, Ctrl-D is None."""
    answer = ask_optional(f"{question} [{'Y/n' if default else 'y/N'}]: ")
    if answer is None:
        return None
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _default_station(index: StationIndex, client: SJClient, name: str) -> Station:
    """The config station as a live-list entry, or a client-resolved stand-in when it's missing."""
    station = index.exact(name)
    if station is not None:
        return station
    # a name the live list lacks: the search resolves it, as --book does
    return {"name": name, "code": client.resolve_station(name), "synonyms": []}


# --- the lists --------------------------------------------------------------------


class _Held(NamedTuple):
    """A booked segment on a chosen date or the evening before (Swedish wall clock)."""

    number: str
    origin: str
    dest: str
    dep: datetime
    arr: datetime | None
    train: str
    # What _release_leg needs to cancel exactly this journey and no other.
    booking_id: str
    journey: dict
    segment: dict


def _held_segments(
    client: SJClient, access_token: str, active_pass: dict, dates: set[str]
) -> list[_Held]:
    """
    The account's active booked segments on `dates` and on the day before each.

    A night train leaving the evening before still runs into a chosen
    date's morning, so the day before is collected too. Every held segment
    is only ever named on the row it refuses, never in a summary: the row
    is where the refusal is decided, and a summary would repeat it.
    Deliberately one-sided: the day *after* is not collected, so a chosen
    departure that runs past midnight cannot see a ticket held the next
    morning — a candidate list is drawn for its own day, and widening this
    would name bookings on a date the user never asked about. A failed
    fetch is only a note.
    """
    try:
        items = fetch_bookings_with_spinner(
            client,
            access_token,
            *booking_date_range(active_pass),
            label="fetching existing bookings",
            trail=False,
        )
    except Exception as e:
        logger.error(f"could not check existing bookings: {e}")
        pwarn(f"could not check existing bookings: {error_text(e)}")
        return []
    wanted = dates | {(date.fromisoformat(d) - timedelta(days=1)).isoformat() for d in dates}
    held: list[_Held] = []
    for item in items:
        booking = item.get("booking") or {}
        if not is_active_booking(booking):
            continue
        number = booking.get("bookingNumber") or "—"
        booking_id = str(
            item.get("bookingId") or booking.get("bookingId") or booking.get("id") or ""
        )
        for jrny in booking.get("journeys") or []:
            for seg in jrny.get("segments") or []:
                try:
                    dep = to_sweden(seg.get("departureDateTime") or "")
                except (ValueError, TypeError):
                    continue
                day = dep.date().isoformat()
                if day not in wanted:
                    continue
                try:
                    arr: datetime | None = to_sweden(seg.get("arrivalDateTime") or "")
                except (ValueError, TypeError):
                    arr = None
                held.append(
                    _Held(
                        number=number,
                        origin=(seg.get("departureStation") or {}).get("name") or "—",
                        dest=(seg.get("arrivalStation") or {}).get("name") or "—",
                        dep=dep,
                        arr=arr,
                        train=train_name(seg),
                        booking_id=booking_id,
                        journey=jrny,
                        segment=seg,
                    )
                )
    return held


def _overlap(departure: dict, held: Sequence[_Held]) -> _Held | None:
    """
    The first held segment whose window intersects this departure's.

    Compared as instants, never by date, so an overnight ticket overlaps
    the next morning's departures (and DST needs no thought). Touching
    edges do not overlap; a held segment without an arrival counts as its
    departure instant, which must fall inside the window.
    """
    if not held:
        return None
    try:
        dep = to_sweden(departure.get("departureDateTime") or "")
        arr = to_sweden(departure.get("arrivalDateTime") or "")
    except (ValueError, TypeError):
        # Search rows always carry both, so this is a shape change worth
        # seeing in a log: the row simply gets no overlap note.
        logger.debug(f"no overlap check for {departure.get('departureId')}: unreadable times")
        return None
    for h in held:
        hit = dep <= h.dep < arr if h.arr is None else dep < h.arr and h.dep < arr
        if hit:
            return h
    return None


def _match_held(
    departure: dict, route: str, held: Sequence[_Held]
) -> tuple[Literal["booked", "overlaps"], _Held] | None:
    """
    ("booked", h) when the departure is a train the account holds, else ("overlaps", h) or None.

    Booked: a held segment leaving at the same instant with the same train
    name (brand + number) or the same route — the row is that ticket, which
    is worth more than "overlaps". Only when no segment is that train does
    the time-intersection rule (`_overlap`) apply. SJ itself does not refuse
    a pure time overlap (it has sold one three minutes into another ticket);
    refusing both here is the user's rule.
    """
    if not held:
        return None
    try:
        dep = to_sweden(departure.get("departureDateTime") or "")
    except (ValueError, TypeError):
        return None
    legs = departure.get("legs") or [{}]
    train = train_name(legs[0])
    for h in held:
        same_train = bool(train) and h.train == train
        if h.dep == dep and (same_train or f"{h.origin} → {h.dest}" == route):
            return "booked", h
    hit = _overlap(departure, held)
    return ("overlaps", hit) if hit is not None else None


def _replaceable(departure: dict, route: str, held: Sequence[_Held]) -> _Held | None:
    """
    The held ticket on this list's own route that `departure` overlaps, if any.

    Such a row is a change of train — the traveller wants this departure
    instead of the one they hold — and may be picked once a pass-free probe
    shows SJ sells a seat on it. An overlap with a ticket on another route
    is almost always a mistake, never a change, and stays refused; so does a
    row that *is* the held train (`_match_held`'s "booked").
    """
    match = _match_held(departure, route, held)
    if match is None or match[0] != "overlaps":
        return None
    hit = match[1]
    return hit if f"{hit.origin} → {hit.dest}" == route else None


def _as_segment(departure: dict) -> dict:
    """A search departure in the shape _match_departure reads a booked segment in."""
    legs = departure.get("legs") or [{}]
    return {
        "departureDateTime": departure.get("departureDateTime"),
        "publicServiceName": legs[0].get("publicServiceName"),
    }


def _sold_class(offer_response: dict, wanted: str, allow_fallback: bool) -> str | None:
    """The first class of the config's chain SJ sells a seat in, or None for none."""
    chain = [wanted] + (
        [c for c in ("2 class calm", "2 class") if c != wanted] if allow_fallback else []
    )
    for class_ in chain:
        if _class_has_seats(offer_response, COMFORT_CODES[class_]):
            return class_
    return None


def _probe_replacements(
    client: SJClient,
    access_token: str,
    params: dict,
    origin_code: str,
    dest_code: str,
    date_str: str,
    departures: list[dict],
    route: str,
    held: Sequence[_Held],
) -> dict[str, str | None]:
    """
    What SJ really sells on the departures that could replace a held ticket.

    The pass search that built the list hides availability on a departure
    overlapping a ticket the account holds, so its class column cannot be
    trusted there. This is --upgrade-class's probe applied to the list: one
    search on the same route and date WITHOUT the pass, then each
    replaceable departure re-found in it (`_match_departure`) and its offers
    read. Returns {departureId: class SJ sells, or None for no seats}; a
    departure the pass-free search did not show is left out — an unknown
    answer, which must never release a ticket. A probe that fails is a note,
    and every replaceable row stays refused as if nothing had been asked.
    Proves only that SJ sells a seat: the pass's 0-price offer is a separate
    quota, known only after the release.
    """
    candidates = [
        (dep, hit) for dep in departures if (hit := _replaceable(dep, route, held)) is not None
    ]
    if not candidates:
        return {}
    numbers = sorted({hit.number for _, hit in candidates})
    n = len(candidates)
    label = (
        f"checking seats on {n} departure{'' if n == 1 else 's'} overlapping {', '.join(numbers)}"
    )
    answers: dict[str, str | None] = {}
    try:
        with spinner(label):
            found = search(
                client,
                access_token,
                origin_code,
                dest_code,
                date_str,
                None,
                tp_product_id=None,
                tp_token_id="",
                service_types=params.get("service_types"),
            )
            if not found["out_id"]:
                return {}
            seen = poll_departures(client, access_token, found["out_id"])
            for dep, _ in candidates:
                sold = _match_departure(seen, _as_segment(dep))
                dep_id = dep.get("departureId")
                if sold is None or not sold.get("departureId") or not dep_id:
                    continue
                offer_response = client.get_offers(
                    access_token, sold["departureId"], found["passenger_token"]
                )
                answers[dep_id] = _sold_class(
                    offer_response,
                    params["comfort_class"],
                    params.get("allow_class_fallback", True),
                )
    except Exception as e:
        logger.error(f"replacement probe failed: {e}")
        pwarn(f"could not check those seats ({error_text(e)}) · the overlapping rows stay refused")
        return {}
    return answers


def _departure_rows(
    departures: list[dict],
    route: str,
    params: dict,
    held: Sequence[_Held] = (),
    probed: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """
    One pick-list row per departure: describe_departure plus the class column.

    class_ is the class the pass would get on it (the configured one, a
    fallback, or None = no seats); the row is disabled when there is none.
    A row the account already holds a ticket on says `already booked in NUM`
    and is refused whatever class the search reported. A row overlapping a
    held ticket on this list's own route may replace it: with `probed`
    (from _probe_replacements) naming the class SJ sells on it, the row
    reads `replaces NUM · HH:MM–HH:MM` in that class, `replaces` carries the
    held ticket, and it can be picked; a probed row without seats is a
    plain refused `no seats`; one the probe did not answer for, and any
    overlap with a ticket on another route, says `overlaps NUM · route
    HH:MM–HH:MM` and is refused. `held_by` names the booking on every one of
    those rows — the caller says so when no row is left.
    """
    wanted = params["comfort_class"]
    allow_fallback = params.get("allow_class_fallback", True)
    probed = probed or {}
    rows: list[dict[str, Any]] = []
    for dep in departures:
        row: dict[str, Any] = dict(describe_departure(dep, route))
        class_ = resolve_class_for_departure(dep, wanted, allow_fallback)
        row["dep"] = dep
        row["class_"] = class_
        row["comfort_class"] = class_ or "—"
        row["note"] = "" if class_ == wanted else ("fallback" if class_ else "no seats")
        row["minutes"] = get_departure_time_minutes(dep)
        row["disabled"] = "" if class_ else f"no seats at {row['departure']} · pick another"
        row["held_by"] = ""
        row["replaces"] = None
        match = _match_held(dep, route, held)
        if match is not None:
            kind, hit = match
            row["held_by"] = hit.number
            span = f"{hit.dep:%H:%M}–{hit.arr:%H:%M}" if hit.arr else f"{hit.dep:%H:%M}"
            dep_id = dep.get("departureId")
            if kind == "booked":
                row["note"] = f"already booked in {hit.number}"
                row["disabled"] = f"this journey is already booked in {hit.number} · pick another"
            elif _replaceable(dep, route, held) is not None and dep_id in probed:
                sold = probed[dep_id]
                row["class_"] = sold
                row["comfort_class"] = sold or "—"
                if sold:
                    row["note"] = f"replaces {hit.number} · {span}"
                    row["disabled"] = ""
                    row["replaces"] = hit
                else:
                    row["note"] = "no seats"
                    row["disabled"] = f"no seats at {row['departure']} · pick another"
            else:
                # In place of a "fallback" note: the fallback is said again
                # after the pick, the held ticket only here. The route is named
                # even when it is this list's own — the note reads as the
                # ticket it points at, not as a diff against the header.
                row["note"] = f"overlaps {hit.number} · {hit.origin} → {hit.dest} {span}"
                row["disabled"] = f"overlaps booking {hit.number} · pick another"
        rows.append(row)
    return rows


def _closest_enabled(rows: list[dict[str, Any]], hhmm: str) -> int:
    """Index of the enabled row closest to hhmm (first row when none is enabled)."""
    target = time_str_to_minutes(hhmm)
    candidates = [
        (abs(row["minutes"] - target), i)
        for i, row in enumerate(rows)
        if not row["disabled"] and row["minutes"] != -1
    ]
    return min(candidates)[1] if candidates else 0


class Pick(NamedTuple):
    """A chosen departure: its leg, the departure itself, and the held journey it replaces."""

    leg: Leg
    dep: dict
    # The held journey to release before this leg can be booked; None for a
    # plain pick. A replacing leg carries no offer_id — the pass search has
    # no offer on it until the release, so _add_pick resolves one then.
    replaces: _Held | None


def _choose_leg(
    client: SJClient,
    access_token: str,
    params: dict,
    passenger_token: str,
    departures: list[dict],
    route: str,
    date_str: str,
    label: str,
    target_time: str,
    departed: int = 0,
    held: Sequence[_Held] = (),
    probed: dict[str, str | None] | None = None,
) -> Pick | None:
    """
    Let the user pick a departure and resolve its offer; None when they abort.

    A pick without a 0-price offer is said, disabled, and the list opened
    again — the frame is redrawn, the day header is not repeated. `departed`
    is how many of the day's departures were already gone: said under the
    day header, so a short list on a same-day run explains itself. `held`
    are the tickets the account already holds: a row that is one, or
    that overlaps one, names it and cannot be picked — unless `probed` (see
    _probe_replacements) says SJ sells a seat on a row overlapping a ticket
    on this very route, which then reads `replaces NUM` and can be picked:
    its offer is not read here (the pass search has none until the held
    journey is released), so the pick carries the held journey instead.
    When every row is refused, the list still opens, so each refusal is
    visible, behind a line saying the leg is a dead end.
    """
    rows = _departure_rows(departures, route, params, held, probed)
    for row, text in zip(rows, departure_choice_lines(rows), strict=True):
        row["text"] = text
    default = _closest_enabled(rows, target_time)
    print_day_header(date_str, route)
    if departed:
        with indented():
            pdim(f"{departed} already departed")
    if rows and all(row["held_by"] and row["disabled"] for row in rows):
        pwarn("every departure is held or overlaps a held ticket · Esc aborts")
    while True:
        picked = select_list(
            label,
            rows,
            lambda r: r["text"],
            default_index=default,
            reject=lambda r: r["disabled"] or None,
        )
        if picked is None:
            return None
        # The row dict is untyped, so this is also what tells the type checker
        # the class is a str; the widget's reject() already refuses a
        # class-less row, so the guard is unreachable in practice — and a
        # None class must never reach find_offer_id, which reads it as "any
        # offer will do" and would book a class the search said had no seats.
        class_: str | None = picked["class_"]
        if class_ is None:
            continue
        if picked["replaces"] is not None:
            facts = describe_departure(picked["dep"], route)
            held_leg = Leg(**facts, comfort_class=class_, offer_id="", alternative=False)
            return Pick(held_leg, picked["dep"], picked["replaces"])
        leg = resolve_offer(
            client, access_token, params, passenger_token, picked["dep"], route, class_, label
        )
        if leg is not None:
            if leg["comfort_class"] != class_:
                # Said here, not in resolve_offer: --book words the same
                # fallback per day and _rebook_released_leg per released leg.
                pwarn(f"{label} class fallback: {class_} → {leg['comfort_class']}")
            return Pick(leg, picked["dep"], None)
        complaint = f"no 0-price offer at {picked['departure']} · pick another"
        pwarn(complaint)
        picked["disabled"] = complaint
        # Not rows.index(picked): highlighting the row just disabled would
        # make Enter a dead key on the re-opened list.
        default = _closest_enabled(rows, target_time)


# --- the cards ----------------------------------------------------------------------


def _summary_rows(chosen: list[tuple[str, str, Pick]], flexibility: str) -> list[dict]:
    """Card rows for the picked legs: (direction, date, pick) → the leg_lines shape."""
    return [
        {
            "date": day,
            "direction": direction,
            "departure": pick.leg["departure"],
            "arrival": pick.leg["arrival"],
            "duration": pick.leg["duration"],
            "train": pick.leg["train"],
            "route": pick.leg["route"],
            "comfort_class": pick.leg["comfort_class"],
            "flexibility": flexibility,
            "note": f"replaces {pick.replaces.number}" if pick.replaces else "",
            "has_offer": True,
        }
        for direction, day, pick in chosen
    ]


def _replacement_line(label: str, pick: Pick) -> str:
    """The `!` line under the cards for a leg that cancels a held journey first."""
    assert pick.replaces is not None
    return (
        f"{label} replaces booking {pick.replaces.number} · its {pick.replaces.dep:%H:%M} journey "
        f"is cancelled first, the pass offer on {pick.leg['departure']} is only known after that"
    )


def _aborted() -> bool:
    """The red closing line of an abort, after the blank every closing gets."""
    blank()
    pstatus(False, "booking aborted, nothing was booked")
    return False


# --- the write ----------------------------------------------------------------------


def _unticketed() -> None:
    """The lines for a leg whose held ticket is gone and nothing was booked back."""
    pwarn("no ticket for this leg: the old one is cancelled and nothing was booked back")
    pdim("recover: book it again with sj-cli --book-journey, or on sj.se")


def _add_pick(
    cart: Cart,
    client: SJClient,
    access_token: str,
    params: dict,
    pick: Pick,
    label: str,
    origin_code: str,
    dest_code: str,
    date_str: str,
    tp_product_id: str,
    tp_token_id: str,
) -> str | None:
    """
    Put one pick into the cart, releasing the held journey first when it replaces one.

    A plain pick is a Cart.add; its exceptions are the caller's, as before.
    A replacing pick is --upgrade-class's step: _release_leg on the held
    journey (one serviceIdentifier, the booking's other journeys kept),
    then at once a pass search, the picked departure re-found in it
    (_match_departure — never the closest to a config time), its offer
    resolved and added. The pass cannot hold two overlapping tickets, so the
    order is fixed: release, then book.

    Returns:
        None when the leg is in the cart; "cancel_failed" or "pending"
        (from _release_leg: the held ticket is intact, or left in a pending
        cancellation only the user can resolve — nothing was booked for
        this leg); "lost" when the journey was released and nothing could
        be booked back, said with _unticketed() — this leg now has no
        ticket, and the caller must say so in its closing line and exit 1.

    """
    held = pick.replaces
    if held is None:
        cart.add(pick.leg, label)
        return None
    target = {
        "booking_id": held.booking_id,
        "booking_number": held.number,
        "segment": held.segment,
        "journey": held.journey,
    }
    failed = _release_leg(client, access_token, target)
    if failed:
        return failed
    try:
        with spinner("searching the same departure with the travel pass"):
            found = search(
                client,
                access_token,
                origin_code,
                dest_code,
                date_str,
                None,
                tp_product_id=tp_product_id,
                tp_token_id=tp_token_id,
                service_types=params.get("service_types"),
            )
            seen = poll_departures(client, access_token, found["out_id"]) if found["out_id"] else []
            departure = _match_departure(seen, _as_segment(pick.dep))
        if not departure or not departure.get("departureId"):
            pwarn("the travel pass search no longer shows this departure")
            _unticketed()
            return "lost"
        leg = resolve_offer(
            client,
            access_token,
            params,
            found["passenger_token"],
            departure,
            pick.leg["route"],
            pick.leg["comfort_class"],
            label,
        )
        if leg is None:
            pwarn("the travel pass has no offer left on this departure")
            _unticketed()
            return "lost"
        if leg["comfort_class"] != pick.leg["comfort_class"]:
            pwarn(f"{label} class fallback: {pick.leg['comfort_class']} → {leg['comfort_class']}")
        cart.add(leg, label)
    except Exception as e:
        # The held ticket is already gone: anything from here must end in a
        # report about this leg, never in an unwound run that says nothing.
        logger.error(f"{label}: re-booking after the release failed: {e}")
        pwarn(f"re-booking failed: {error_text(e)}")
        _unticketed()
        return "lost"
    return None


# --- the mode -----------------------------------------------------------------------


def handle_book_journey(
    client: SJClient,
    access_token: str,
    cfg: dict,
    active_pass: dict,
    tp_product_id: str,
    tp_token_id: str,
    dry_run: bool = False,
) -> bool:
    """
    Book one journey interactively: questions, pick lists, one confirmation, the Cart.

    Needs a terminal on both ends (the pick lists draw). Everything not
    typed comes from config: the date defaults to today, the stations to
    station_from/station_to, the return question to roundtrip, the
    highlighted row to the departure closest to time_leave/time_return.
    Class, flexibility, service types and seat preference apply as in
    --book. A dry run stops after the summary and writes nothing.

    Returns:
        True when a booking was checked out (or a dry run completed); False
        when refused, aborted, declined, nothing was found, or the checkout
        failed (the provisional is left behind: --book's cleanup only
        covers the configured route, so SJ's own expiry ends it).

    """
    params = cfg["search_parameters"]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        pstatus(False, "not a terminal · --book-journey asks questions")
        return False

    try:
        with spinner("fetching stations", trail=False):
            index = StationIndex(parse_stations(client.get_stations()))
    except Exception as e:
        logger.error(f"station list: {e}")
        pstatus(False, f"could not fetch the station list: {error_text(e)}")
        return False

    # The questions
    today = sweden_now().date()
    valid = pass_validity(active_pass)
    day = _ask_date("date", today, today, "date is in the past", valid)
    if day is None:
        return _aborted()
    from_default = _default_station(index, client, params["station_from"])
    origin = _ask_station("from", from_default, index, None)
    if origin is None:
        return _aborted()
    dest = _ask_station("to", _default_station(index, client, params["station_to"]), index, origin)
    if dest is None:
        return _aborted()
    wants_return = _ask_yes_no("return?", bool(params.get("roundtrip", False)))
    if wants_return is None:
        return _aborted()
    return_day = None
    if wants_return:
        return_day = _ask_date(
            "return date", day, day, "return date is before the outbound date", valid
        )
        if return_day is None:
            return _aborted()
    blank()

    # The search
    date_str = day.isoformat()
    return_str = return_day.isoformat() if return_day else None
    out_route = f"{origin['name']} → {dest['name']}"
    in_route = f"{dest['name']} → {origin['name']}"
    with spinner(f"searching {out_route} on {date_str}"):
        found = search(
            client,
            access_token,
            origin["code"],
            dest["code"],
            date_str,
            return_str,
            tp_product_id=tp_product_id,
            tp_token_id=tp_token_id,
            service_types=params.get("service_types"),
        )
        out_deps = poll_departures(client, access_token, found["out_id"]) if found["out_id"] else []
        in_deps = (
            poll_departures(client, access_token, found["in_id"])
            if return_str and found["in_id"]
            else []
        )
    # Read the clock here, not before the questions: the user may have spent
    # minutes on them, and a train that left meanwhile must not stay listed.
    now = sweden_now()
    today_str = now.date().isoformat()
    out_deps, out_gone = drop_departed(out_deps, now)
    in_deps, in_gone = drop_departed(in_deps, now)
    if not out_deps:
        why = "left" if out_gone else "found"
        when = "today" if date_str == today_str else f"on {date_str}"
        blank()
        pstatus(False, f"no departures {why} for {out_route} {when}")
        return False
    if return_str and not in_deps:
        why = "left" if in_gone else "found"
        when = "today" if return_str == today_str else f"on {return_str}"
        blank()
        pstatus(False, f"no departures {why} for {in_route} {when}")
        return False
    dates = {date_str, *([return_str] if return_str else [])}
    held = _held_segments(client, access_token, active_pass, dates)

    # The picks
    passenger_token = found["passenger_token"]
    probed = _probe_replacements(
        client,
        access_token,
        params,
        origin["code"],
        dest["code"],
        date_str,
        out_deps,
        out_route,
        held,
    )
    outbound = _choose_leg(
        client,
        access_token,
        params,
        passenger_token,
        out_deps,
        out_route,
        date_str,
        "outbound",
        params["time_leave"],
        departed=out_gone,
        held=held,
        probed=probed,
    )
    if outbound is None:
        return _aborted()
    inbound = None
    if return_str:
        probed = _probe_replacements(
            client,
            access_token,
            params,
            dest["code"],
            origin["code"],
            return_str,
            in_deps,
            in_route,
            held,
        )
        inbound = _choose_leg(
            client,
            access_token,
            params,
            passenger_token,
            in_deps,
            in_route,
            return_str,
            "return",
            params.get("time_return", "17:00"),
            departed=in_gone,
            held=held,
            probed=probed,
        )
        if inbound is None:
            return _aborted()

    # The summary and the consent
    blank()
    chosen = [("Outbound", date_str, outbound)]
    if inbound is not None and return_str:
        chosen.append(("Return", return_str, inbound))
    print_day_cards(_summary_rows(chosen, params.get("flexibility", "FULLFLEX")))
    replacing = [(direction.lower(), pick) for direction, _, pick in chosen if pick.replaces]
    for label, pick in replacing:
        pwarn(_replacement_line(label, pick))
    blank()
    if dry_run:
        pstatus(
            None,
            "dry run · nothing cancelled, nothing booked"
            if replacing
            else "dry run · nothing booked",
        )
        return True
    if not confirm("cancel and book? [y/N]: " if replacing else "book? [y/N]: "):
        return _aborted()

    # The write
    cart = Cart(client, access_token, cfg, passenger_token)
    lost = ""
    try:
        try:
            failed = _add_pick(
                cart,
                client,
                access_token,
                params,
                outbound,
                "outbound",
                origin["code"],
                dest["code"],
                date_str,
                tp_product_id,
                tp_token_id,
            )
        except Exception as e:
            # The offer was resolved while the user browsed the lists, so it
            # may have gone stale; a failed first add leaves the cart empty
            # (nothing is held), which is a plain end to the run, not a crash.
            logger.error(f"outbound leg failed: {e}")
            blank()
            pstatus(False, f"could not create the booking ({error_text(e)}) · nothing was booked")
            return False
        if failed:
            # The cause was said by _add_pick; the return is not attempted —
            # a lone return was never what the traveller asked for.
            blank()
            tail = " · the outbound has no ticket" if failed == "lost" else ""
            pstatus(False, f"nothing was booked{tail}")
            return False
        if inbound is not None and return_str:
            try:
                failed = _add_pick(
                    cart,
                    client,
                    access_token,
                    params,
                    inbound,
                    "return",
                    dest["code"],
                    origin["code"],
                    return_str,
                    tp_product_id,
                    tp_token_id,
                )
            except Exception as e:
                # SPEC §8.2: the outbound is held — keep it rather than lose both.
                logger.error(f"return leg failed: {e}")
                pwarn(f"return leg failed ({error_text(e)}), booking outbound only")
                failed = None
            if failed == "lost":
                lost = " · the return has no ticket"
            elif failed:
                pwarn("return leg not booked, booking outbound only")
        result = cart.finish()
    except KeyboardInterrupt:
        # main() prints "interrupted by user" and exits 130, which would say
        # nothing about the provisional SJ is now holding — and --book's
        # cleanup only sweeps the configured route, which this mode leaves.
        if cart.held:
            pwarn(
                f"booking {cart.booking_number or cart.booking_id} left as a provisional, "
                "SJ releases it or cancel it on sj.se"
            )
        raise
    number = result["booking_number"] or result["booking_id"]
    try:
        print_day_cards(booked_rows(result["booking"], result["booking_number"]))
    except Exception as e:  # a rendering slip must not hide a booked ticket
        logger.error(f"could not render booking {number}: {e}")
        pwarn(f"booked as {number}, but the legs could not be shown ({error_text(e)})")
    blank()
    if not result["checked_out"]:
        pstatus(
            False,
            f"booking {number} not checked out · provisional left, "
            f"SJ releases it or cancel it on sj.se{lost}",
        )
        return False
    if lost:
        # A ticket was booked, but a leg this run released has none: exit 1,
        # as --upgrade-class does for a leg it leaves unticketed.
        pstatus(False, f"booked {number}{lost}")
        return False
    pstatus(True, f"booked {number}")
    return True
