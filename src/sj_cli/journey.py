"""The interactive journey mode (--book-journey): questions, pick lists, then the Cart."""

import logging
import sys
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

from sj_cli.booking import (
    Cart,
    Leg,
    booked_rows,
    booking_date_range,
    describe_departure,
    drop_departed,
    fetch_all_bookings,
    get_departure_time_minutes,
    is_active_booking,
    pass_validity,
    poll_departures,
    resolve_class_for_departure,
    resolve_offer,
    search,
    time_str_to_minutes,
)
from sj_cli.client import SJClient
from sj_cli.dates import sweden_now, to_sweden
from sj_cli.errors import error_text
from sj_cli.output import (
    ask_optional,
    blank,
    confirm,
    departure_choice_lines,
    group_route,
    indented,
    pdim,
    print_day_header,
    print_leg_lines,
    pstatus,
    pwarn,
    select_filtered,
    select_list,
    spinner,
)
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
    day: str
    origin: str
    dest: str
    dep: datetime
    arr: datetime | None


def _held_segments(
    client: SJClient, access_token: str, active_pass: dict, dates: set[str]
) -> list[_Held]:
    """
    The account's active booked segments on `dates` and on the day before each.

    A night train leaving the evening before still runs into a chosen
    date's morning, so the day before is collected too; `_Held.day` stays
    the segment's own departure day, which is what the summary filters on.
    A failed fetch is only a note.
    """
    try:
        with spinner("fetching existing bookings", trail=False):
            items = fetch_all_bookings(client, access_token, *booking_date_range(active_pass))
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
                        day=day,
                        origin=(seg.get("departureStation") or {}).get("name") or "—",
                        dest=(seg.get("arrivalStation") or {}).get("name") or "—",
                        dep=dep,
                        arr=arr,
                    )
                )
    return held


def _warn_held_bookings(held: Sequence[_Held], dates: set[str]) -> None:
    """
    Name every ticket the account holds on the chosen dates.

    A pass search reports every class unavailable on a departure that
    overlaps a held ticket, so the list will show "no seats" there: say why.
    Segments picked up from the evening before are not on a chosen date and
    stay out of this summary — the row they overlap still names them.
    """
    for h in held:
        if h.day not in dates:
            continue
        pwarn(
            f"you hold booking {h.number} on {h.day} ({h.origin} → {h.dest} "
            f"{h.dep.strftime('%H:%M')}) · departures overlapping it show no seats"
        )


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
        return None
    for h in held:
        hit = dep <= h.dep < arr if h.arr is None else dep < h.arr and h.dep < arr
        if hit:
            return h
    return None


def _departure_rows(
    departures: list[dict], route: str, params: dict, held: Sequence[_Held] = ()
) -> list[dict[str, Any]]:
    """
    One pick-list row per departure: describe_departure plus the class column.

    class_ is the class the pass would get on it (the configured one, a
    fallback, or None = no seats); the row is disabled when there is none.
    A row overlapping a held ticket says which one (with the held route
    when it differs from this list's) instead of "no seats", and a
    class-less overlapping row is disabled with that booking's number.
    """
    wanted = params["comfort_class"]
    allow_fallback = params.get("allow_class_fallback", True)
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
        hit = _overlap(dep, held)
        if hit is not None:
            span = f"{hit.dep:%H:%M}–{hit.arr:%H:%M}" if hit.arr else f"{hit.dep:%H:%M}"
            held_route = f"{hit.origin} → {hit.dest}"
            where = "" if held_route == route else f"{held_route} "
            # Deliberately in place of a "fallback" note: resolve_offer's
            # caller says the fallback again after the pick, the overlap is
            # only ever said here.
            row["note"] = f"overlaps {hit.number} · {where}{span}"
            if class_ is None:
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
) -> Leg | None:
    """
    Let the user pick a departure and resolve its offer; None when they abort.

    A pick without a 0-price offer is said, disabled, and the list opened
    again — the frame is redrawn, the day header is not repeated. `departed`
    is how many of the day's departures were already gone: said under the
    day header, so a short list on a same-day run explains itself. `held`
    are the tickets the account already holds: a row overlapping one names
    it, and is disabled when it has no class either.
    """
    rows = _departure_rows(departures, route, params, held)
    for row, text in zip(rows, departure_choice_lines(rows), strict=True):
        row["text"] = text
    default = _closest_enabled(rows, target_time)
    print_day_header(date_str, route)
    if departed:
        with indented():
            pdim(f"{departed} already departed")
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
        leg = resolve_offer(
            client, access_token, params, passenger_token, picked["dep"], route, class_, label
        )
        if leg is not None:
            if leg["comfort_class"] != class_:
                # Said here, not in resolve_offer: --book words the same
                # fallback per day and _rebook_released_leg per released leg.
                pwarn(f"{label} class fallback: {class_} → {leg['comfort_class']}")
            return leg
        complaint = f"no 0-price offer at {picked['departure']} · pick another"
        pwarn(complaint)
        picked["disabled"] = complaint
        # Not rows.index(picked): highlighting the row just disabled would
        # make Enter a dead key on the re-opened list.
        default = _closest_enabled(rows, target_time)


# --- the cards ----------------------------------------------------------------------


def _summary_rows(chosen: list[tuple[str, str, Leg]], flexibility: str) -> list[dict]:
    """Card rows for the picked legs: (direction, date, leg) → the leg_lines shape."""
    return [
        {
            "date": day,
            "direction": direction,
            "departure": leg["departure"],
            "arrival": leg["arrival"],
            "duration": leg["duration"],
            "train": leg["train"],
            "route": leg["route"],
            "comfort_class": leg["comfort_class"],
            "flexibility": flexibility,
            "note": "",
            "has_offer": True,
        }
        for direction, day, leg in chosen
    ]


def _print_cards(rows: list[dict]) -> None:
    """One day card per date in the rows, in date order."""
    days: dict[str, list[dict]] = {}
    for row in rows:
        days.setdefault(row.get("date") or "—", []).append(row)
    for day in sorted(days):
        print_day_header(day, group_route(days[day]))
        with indented():
            print_leg_lines(days[day])


def _aborted() -> bool:
    pstatus(False, "booking aborted, nothing was booked")
    return False


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
        pstatus(False, f"no departures {why} for {out_route} {when}")
        return False
    if return_str and not in_deps:
        why = "left" if in_gone else "found"
        when = "today" if return_str == today_str else f"on {return_str}"
        pstatus(False, f"no departures {why} for {in_route} {when}")
        return False
    dates = {date_str, *([return_str] if return_str else [])}
    held = _held_segments(client, access_token, active_pass, dates)
    _warn_held_bookings(held, dates)

    # The picks
    passenger_token = found["passenger_token"]
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
    )
    if outbound is None:
        return _aborted()
    inbound = None
    if return_str:
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
        )
        if inbound is None:
            return _aborted()

    # The summary and the consent
    blank()
    chosen = [("Outbound", date_str, outbound)]
    if inbound is not None and return_str:
        chosen.append(("Return", return_str, inbound))
    _print_cards(_summary_rows(chosen, params.get("flexibility", "FULLFLEX")))
    blank()
    if dry_run:
        pstatus(None, "dry run · nothing booked")
        return True
    if not confirm("book? [y/N]: "):
        return _aborted()

    # The write
    cart = Cart(client, access_token, cfg, passenger_token)
    try:
        try:
            cart.add(outbound, "outbound")
        except Exception as e:
            # The offer was resolved while the user browsed the lists, so it
            # may have gone stale; a failed first add leaves the cart empty
            # (nothing is held), which is a plain end to the run, not a crash.
            logger.error(f"outbound leg failed: {e}")
            pstatus(False, f"could not create the booking ({error_text(e)}) · nothing was booked")
            return False
        if inbound is not None:
            try:
                cart.add(inbound, "return")
            except Exception as e:
                # SPEC §8.2: the outbound is held — keep it rather than lose both.
                logger.error(f"return leg failed: {e}")
                pwarn(f"return leg failed ({error_text(e)}), booking outbound only")
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
        _print_cards(booked_rows(result["booking"], result["booking_number"]))
    except Exception as e:  # a rendering slip must not hide a booked ticket
        logger.error(f"could not render booking {number}: {e}")
        pwarn(f"booked as {number}, but the legs could not be shown ({error_text(e)})")
    blank()
    if not result["checked_out"]:
        pstatus(
            False,
            f"booking {number} not checked out · provisional left, "
            "SJ releases it or cancel it on sj.se",
        )
        return False
    pstatus(True, f"booked {number}")
    return True
