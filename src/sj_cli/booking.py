"""Booking business logic for the SJ API client."""

import logging
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from sj_cli.auth import ensure_valid_token
from sj_cli.client import SJClient
from sj_cli.config import SERVICE_TYPE_NAMES
from sj_cli.dates import (
    booking_dates,
    selected_dates,
    skip_reason,
    sweden_now,
    to_sweden,
)
from sj_cli.errors import SJAuthError, error_text
from sj_cli.output import (
    ask,
    ask_optional,
    blank,
    format_class_name,
    format_duration,
    indented,
    leg_lines,
    pdim,
    pinfo,
    print_bookings_table,
    print_day_header,
    print_day_note,
    print_leg_lines,
    print_seat_choices,
    pstatus,
    pwarn,
    spinner,
)
from sj_cli.seats import (
    COMFORT_NAMES,
    Seat,
    assigned_seat,
    assigned_seat_words,
    best_seat,
    carriage_comfort,
    current_seat,
    describe_seat,
    free_seats,
    number_key,
    satisfies,
    seat_words,
)
from sj_cli.tokens import TokenManager

logger = logging.getLogger(__name__)

# A provisional booking younger than this is left alone by the stale cleanup:
# it may be a cart the user is checking out right now, or another run of this
# tool between its create and checkout calls. Provisionals expire server-side
# after ~30 minutes anyway.
STALE_PROVISIONAL_GRACE = timedelta(minutes=10)


def booking_date_range(
    travel_pass: dict | None,
    start_offset_days: int = 0,
    fallback_days: int = 90,
) -> tuple[str, str]:
    """
    Calculate booking date range from travel pass validity.

    Args:
        travel_pass: Travel pass dict with endTravelValidityDateTime, or None.
        start_offset_days: Days from today for the start date (0=today, 1=tomorrow).
        fallback_days: Days from now to use as end date if no travel pass.

    Returns:
        Tuple of (start_date, end_date) as YYYY-MM-DD strings.

    """
    now_date = sweden_now()
    b_start = (now_date + timedelta(days=start_offset_days)).strftime("%Y-%m-%d")

    valid_end = travel_pass.get("endTravelValidityDateTime") if travel_pass else None
    if valid_end:
        vp_end = to_sweden(valid_end)
        b_end = (vp_end + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        b_end = (now_date + timedelta(days=fallback_days)).strftime("%Y-%m-%d")

    return b_start, b_end


def _segment_to_display_row(segment: dict, booking_number: str, now: datetime) -> dict:
    """
    Transform a raw booking segment into a display row dict.

    Args:
        segment: Segment dict from the API.
        booking_number: The parent booking number.
        now: Current aware datetime for past-detection (e.g. sweden_now()).

    Returns:
        Display row dict with keys: date, direction, departure, arrival,
        duration, comfort_class, route, booking_number, past, train, seat.

    """
    # API says OUTBOUND/INBOUND; the UI (and the dry-run table) says Outbound/Return
    direction = "Return" if segment.get("direction") == "INBOUND" else "Outbound"
    dep_dt = segment.get("departureDateTime", "")
    arr_dt = segment.get("arrivalDateTime", "")
    duration = segment.get("duration", "")
    # The API writes explicit nulls for absent sub-objects: `or {}` covers
    # both a missing key and a null value (`.get(key, {})` only the former).
    prod_name = (segment.get("productFamily") or {}).get("name") or "—"
    dep_station = (segment.get("departureStation") or {}).get("name") or "—"
    arr_station = (segment.get("arrivalStation") or {}).get("name") or "—"

    # Train: brand + public service number, e.g. "X 2000 537"
    brand = segment.get("serviceBrandNameDescription") or (
        (segment.get("serviceType") or {}).get("name") or ""
    )
    number = segment.get("publicServiceName") or segment.get("serviceName") or ""
    train = " ".join(part for part in (brand, number) if part) or "—"

    # Seat: carriage + seat number from the first required product
    seat = "—"
    products = segment.get("requiredProducts") or []
    if products:
        seat_info = products[0].get("seat") or {}
        carriage = seat_info.get("carriageNumber")
        seat_no = seat_info.get("number")
        if carriage or seat_no:
            seat = f"carriage {carriage or '?'} seat {seat_no or '?'}"

    in_past = "N"
    try:
        dep_local = to_sweden(dep_dt)
        if dep_local < now:
            in_past = "Y"
        date_str = dep_local.strftime("%Y-%m-%d")
        dep_time = dep_local.strftime("%H:%M")
    except (ValueError, TypeError):
        date_str = dep_dt
        dep_time = dep_dt
    try:
        arr_time = to_sweden(arr_dt).strftime("%H:%M")
    except (ValueError, TypeError):
        arr_time = arr_dt

    return {
        "date": date_str,
        "direction": direction,
        "departure": dep_time,
        "arrival": arr_time,
        "duration": format_duration(duration),
        "comfort_class": format_class_name(prod_name),
        "route": f"{dep_station} → {arr_station}",
        "booking_number": booking_number,
        "past": in_past,
        "train": train,
        "seat": seat,
    }


def time_str_to_minutes(hhmm: str) -> int:
    """Convert HH:MM string to minutes since midnight."""
    try:
        parts = hhmm.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return 0


def get_departure_time_minutes(dep: dict) -> int:
    """Extract departure time as minutes since midnight from a departure dict."""
    try:
        dt_str = dep.get("departureDateTime", "")
        time_part = dt_str.split("T")[1][:5]
        return time_str_to_minutes(time_part)
    except (IndexError, AttributeError):
        return -1


def check_comfort_availability(departure: dict, requested_class: str) -> bool:
    """Check if a departure supports the requested class based on serviceProperties."""
    props = [
        p.get("code")
        for leg in departure.get("legs") or []
        for p in leg.get("serviceProperties") or []
    ]

    has_1_class = "COMFORT-AB" in props
    has_calm = "COMFORT-CALM" in props
    has_2_class = "COMFORT-AB" in props or "COMFORT-B" in props

    if requested_class == "1 class":
        return has_1_class
    if requested_class == "2 class calm":
        return has_calm
    if requested_class == "2 class":
        return has_2_class

    return False


def _find_departure_by_time(
    departures: list,
    target_time_str: str,
    select_closest: bool,
) -> dict | None:
    """
    Find a departure matching the target time.

    Args:
        departures: List of departure dicts from the API.
        target_time_str: Target time as HH:MM.
        select_closest: If True, pick closest time. If False, exact match only.

    Returns:
        The matching departure dict or None.

    """
    target_minutes = time_str_to_minutes(target_time_str)
    timed = []

    for dep in departures:
        dep_minutes = get_departure_time_minutes(dep)
        if dep_minutes == -1:
            continue
        timed.append((dep, dep_minutes - target_minutes))

    if not timed:
        return None

    if select_closest:
        timed.sort(key=lambda x: abs(x[1]))
        return timed[0][0]

    # Exact match only
    for dep, diff in timed:
        if diff == 0:
            return dep

    logger.info(f"no exact match found for {target_time_str}")
    return None


def _resolve_class_for_departure(
    departure: dict,
    requested_class: str,
    allow_fallback: bool,
) -> str | None:
    """
    Determine the comfort class to use for a departure.

    Checks the requested class first, then falls back through the chain
    if allowed.

    Returns:
        The class string to use, or None if unavailable.

    """
    if check_comfort_availability(departure, requested_class):
        return requested_class

    if not allow_fallback:
        return None

    # Fallback chain: requested → 2 class calm → 2 class
    fallback_chain = ["2 class calm", "2 class"]
    for fallback_class in fallback_chain:
        if fallback_class != requested_class and check_comfort_availability(
            departure, fallback_class
        ):
            return fallback_class

    return None


def select_best_departure(
    departures: list,
    target_time_str: str,
    requested_class: str,
    select_closest: bool,
    allow_fallback: bool = True,
) -> dict | None:
    """
    Find the best departure by time, then resolve comfort class.

    Steps:
        1. Find departure matching target time (exact or closest).
        2. If not found and select_closest is False, return None.
        3. Resolve comfort class (primary → fallback chain).

    Args:
        departures: List of departure dicts from the API.
        target_time_str: Target time as HH:MM.
        requested_class: Requested comfort class.
        select_closest: If True, pick closest time. If False, exact match only.
        allow_fallback: If True, fall back through class chain.

    Returns:
        Best candidate dict or None.

    """
    # Candidates in preference order: exact matches only, or every departure
    # by distance from the target (stable, so API order breaks ties). The
    # first one that carries the class wins — a closest departure without
    # the class (a bus, a regional without calm) must not end the leg.
    target_minutes = time_str_to_minutes(target_time_str)
    timed = []
    for dep in departures:
        dep_minutes = get_departure_time_minutes(dep)
        if dep_minutes == -1:
            continue
        diff = dep_minutes - target_minutes
        if select_closest or diff == 0:
            timed.append((abs(diff), diff, dep))
    timed.sort(key=lambda x: x[0])
    if not timed:
        if not select_closest:
            logger.info(f"no exact match found for {target_time_str}")
        return None

    for _, diff, dep in timed:
        time_str = dep.get("departureDateTime", "").split("T")[1][:5]
        valid_class = _resolve_class_for_departure(dep, requested_class, allow_fallback)
        if not valid_class:
            pwarn(f"departure at {time_str}: no matching class available")
            continue
        if valid_class != requested_class:
            pwarn(f"departure at {time_str}: {requested_class} unavailable, using {valid_class}")
        return {
            "departure": dep,
            "class": valid_class,
            "diff": diff,
            "time_str": time_str,
            "id": dep.get("departureId"),
        }
    return None


def find_offer_id(
    offer_response: dict,
    requested_class: str,
    requested_flexibility: str,
    allow_fallback: bool = True,
) -> tuple[str, str] | None:
    """
    Parse the get_offers response and find the offerId for a 0-price offer.

    The SPEC §7.2 class-fallback chain applies here too, not only to seat
    availability: a departure can carry seats in the requested class while
    the pass has no 0-price offer for it (e.g. 1 class on an Årskort
    Silver). With allow_fallback, fall through requested → 2 class calm →
    2 class on this same departure before giving up.

    Args:
        offer_response: The offers API response dict.
        requested_class: Requested comfort class.
        requested_flexibility: Requested flexibility (FULLFLEX, SEMIFLEX, NOFLEX).
        allow_fallback: If False, match only the exact requested class.

    Returns:
        Tuple of (offer_id, matched_class) if a 0-price offer is found,
        None otherwise. matched_class is the display name (e.g. "2 class calm").

    """
    seat_offers = offer_response.get("seatOffers") or {}
    offers = seat_offers.get("offers") or {}
    if not offers:
        return None

    class_map = {
        "2 class": ["SECOND"],
        "2 class calm": ["SECOND_CALM"],
        "1 class": ["FIRST"],
    }
    if allow_fallback:
        class_map = {
            cls: keys + [fb for fb in ("SECOND_CALM", "SECOND") if fb not in keys]
            for cls, keys in class_map.items()
        }

    # Iterate in preference order, not API dict order — the API lists SECOND
    # before SECOND_CALM, so walking offers.items() would always pick plain
    # 2 class even when calm is available.
    target_classes = class_map.get(requested_class, list(offers.keys()))

    for comf_key in target_classes:
        offer_data = offers.get(comf_key)
        if offer_data:
            flexibilities = offer_data.get("flexibilities") or {}

            for flex_type, flex_data in flexibilities.items():
                if flex_type != requested_flexibility:
                    continue

                if not flex_data.get("available"):
                    logger.warning(f"found {comf_key}/{flex_type} but it is not available")
                    continue

                prices: dict[str, Any] = flex_data.get("journeyPrices") or {}
                price_obj: dict[str, Any] = prices.get("price") or {}
                amount: Any = price_obj.get("amount")

                try:
                    amount_val = float(amount)
                except (TypeError, ValueError):
                    amount_val = -1

                if amount_val == 0:
                    matched_class = COMFORT_NAMES.get(comf_key, requested_class)
                    logger.info(f"found offer: {comf_key} - {flex_type}")
                    specific_offer_id = flex_data.get("offerId")
                    if specific_offer_id:
                        return (specific_offer_id, matched_class)
                    logger.warning("found match but no offerId in flex object")
                    return None
                logger.warning(
                    f"offer found {comf_key}/{flex_type} but price is {amount} "
                    f"(expected 0), skipping"
                )

    return None


def _try_alternative_departure(
    client: SJClient,
    access_token: str,
    search_id: str,
    target_time: str,
    requested_class: str,
    flexibility: str,
    allow_fallback: bool,
    passenger_token: str,
    skip_departure_id: str,
    prefer_earlier: bool = True,
) -> dict | None:
    """
    Try one alternative departure when the closest one has no 0-price offer.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        search_id: Search ID from the original search.
        target_time: Target time as HH:MM.
        requested_class: Requested comfort class.
        flexibility: Requested flexibility type.
        allow_fallback: If True, fall back through class chain.
        passenger_token: Passenger token for offers.
        skip_departure_id: Departure ID to skip (the one that failed).
        prefer_earlier: If True, pick the closest departure BEFORE the target
            time (outbound — arrive on time). If False, pick the closest
            departure AFTER the target time (inbound — don't leave early).

    Returns:
        Dict with offer_id, time_str, arrival, class, and departure data
        if found, None otherwise.

    """
    results = client.get_search_results(access_token, search_id)
    travels = results.get("travels") or []
    if not travels:
        return None
    departures = travels[0].get("departures") or []

    target_minutes = time_str_to_minutes(target_time)
    candidates = []
    for dep in departures:
        dep_id = dep.get("departureId")
        if dep_id == skip_departure_id:
            continue
        dep_minutes = get_departure_time_minutes(dep)
        if dep_minutes == -1:
            continue
        diff = dep_minutes - target_minutes
        # Outbound must not be later, inbound must not be earlier than the
        # target; a second train at the exact minute is fine (the one that
        # failed is already excluded by skip_departure_id).
        if prefer_earlier and diff > 0:
            continue
        if not prefer_earlier and diff < 0:
            continue
        valid_class = _resolve_class_for_departure(dep, requested_class, allow_fallback)
        if not valid_class:
            continue
        candidates.append((dep, valid_class, abs(diff)))

    if not candidates:
        pinfo("no alternative departures found")
        return None

    # Sort by proximity and try only the closest one
    candidates.sort(key=lambda x: x[2])
    dep, valid_class, _ = candidates[0]
    time_str = dep.get("departureDateTime", "").split("T")[1][:5]
    dep_id = dep.get("departureId")

    with spinner(f"checking alternative departure at {time_str}"):
        offers = client.get_offers(access_token, dep_id, passenger_token)
    result = find_offer_id(offers, valid_class, flexibility, allow_fallback)
    if result:
        offer_id, matched_class = result
        pinfo(f"found offer at alternative departure {time_str}")
        if matched_class != valid_class:
            pinfo(f"class fallback: {valid_class} → {matched_class}")
        return {
            "offer_id": offer_id,
            "time_str": time_str,
            "arrival": _get_arrival_time(dep),
            "class": matched_class,
            "departure": dep,
        }

    pwarn(f"alternative departure {time_str} also unavailable, skipping")
    return None


def poll_and_select(
    client: SJClient,
    access_token: str,
    search_id: str,
    target_time: str,
    requested_class: str,
    select_closest: bool,
    allow_fallback: bool = True,
) -> dict | None:
    """
    Poll for search results and select the best departure.

    Polls up to 5 times, 1 second apart, until departures appear.
    """
    departures: list[dict[str, Any]] = []
    for attempt in range(5):
        if attempt:
            time.sleep(1.0)
        results = client.get_search_results(access_token, search_id)
        travels = results.get("travels") or []
        if travels:
            departures = travels[0].get("departures") or []
            if departures:
                break

    if not departures:
        logger.warning(f"no departures found for search {search_id}")
        return None

    return select_best_departure(
        departures, target_time, requested_class, select_closest, allow_fallback
    )


def is_stale_provisional(booking: dict) -> bool:
    """
    True for a leftover provisional booking from an interrupted run.

    Such bookings have status NEW and can still be cancelled (CANCEL_JOURNEY).
    cleanup_stale_provisionals() cancels them in --book mode; the duplicate
    check ignores them in both modes so dry-run and --book agree.
    """
    status = booking.get("bookingStatus") or booking.get("status")
    return status == "NEW" and "CANCEL_JOURNEY" in (booking.get("possibleActions") or [])


def _booking_id(item: dict, booking: dict) -> str:
    """The id the booking endpoints take: the listing wrapper's first, then the object's."""
    return str(item.get("bookingId") or booking.get("bookingId") or booking.get("id") or "")


def is_active_booking(booking: dict) -> bool:
    """True for a booking that counts: not cancelled and not a stale provisional."""
    return booking.get("bookingStatus") != "CANCELLED" and not is_stale_provisional(booking)


def _segment_date(dt_str: str) -> str:
    """Swedish calendar date (YYYY-MM-DD) of an API timestamp; raw date part if unparsable."""
    try:
        return to_sweden(dt_str).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return dt_str.split("T", maxsplit=1)[0] if isinstance(dt_str, str) else ""


def _journey_endpoints(journey: dict) -> tuple[str | None, str | None, str]:
    """
    (origin uic, destination uic, Swedish departure date) of a whole journey.

    A journey with a change (A → C → B) is the route A → B: matching on
    single segments would miss it, both for the duplicate check and for
    --cancel-date.
    """
    segments = [s for s in journey.get("segments") or [] if s.get("departureDateTime")]
    if not segments:
        return None, None, ""
    first, last = segments[0], segments[-1]
    return (
        (first.get("departureStation") or {}).get("uicStationCode"),
        (last.get("arrivalStation") or {}).get("uicStationCode"),
        _segment_date(first["departureDateTime"]),
    )


def check_existing_booking(bookings: list, origin_id: str, dest_id: str, date_str: str) -> bool:
    """Check if a (non-cancelled, non-provisional) booking exists for the route and date."""
    for item in bookings:
        booking_details = item.get("booking") or {}
        if not is_active_booking(booking_details):
            continue
        for journey in booking_details.get("journeys") or []:
            j_origin, j_dest, j_date = _journey_endpoints(journey)
            if j_origin == origin_id and j_dest == dest_id and j_date == date_str:
                return True
    return False


def fetch_all_bookings(client: SJClient, access_token: str, start_date: str, end_date: str) -> list:
    """
    Fetch all bookings with pagination.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        start_date: Start date string (YYYY-MM-DD).
        end_date: End date string (YYYY-MM-DD).

    Returns:
        List of all booking items across all pages.

    """
    bookings_list: list[dict[str, Any]] = []
    page = 0
    while True:
        bookings_resp = client.get_bookings(access_token, start_date, end_date, page)
        page_bookings = bookings_resp.get("bookings") or []
        bookings_list.extend(page_bookings)

        next_page = bookings_resp.get("nextPage")
        if next_page is None or next_page == page:
            break

        page = next_page
        time.sleep(0.5)

    return bookings_list


def _on_route(booking: dict, route: tuple[str, str] | None) -> bool:
    """True when a journey of the booking runs the route in either direction (or no route given)."""
    if route is None:
        return True
    origin, dest = route
    for journey in booking.get("journeys") or []:
        j_origin, j_dest, _ = _journey_endpoints(journey)
        if (j_origin, j_dest) in {(origin, dest), (dest, origin)}:
            return True
    return False


def _provisional_age(booking: dict, now: datetime) -> timedelta | None:
    """How long ago the provisional was created; None when the API gave no usable timestamp."""
    created = booking.get("created")
    if not created:
        return None
    try:
        return now - to_sweden(created)
    except (ValueError, TypeError):
        return None


def cleanup_stale_provisionals(
    client: SJClient,
    access_token: str,
    bookings: list,
    route: tuple[str, str] | None = None,
    now: datetime | None = None,
) -> list:
    """
    Cancel stale provisional bookings and return the filtered list.

    A booking with status "NEW" and "CANCEL_JOURNEY" in possibleActions is
    a provisional. Only the ones that look like this tool's own leftovers
    are cancelled: on the configured route (either direction — a cart the
    user has open on sj.se for another trip is not ours to touch) and older
    than STALE_PROVISIONAL_GRACE (a younger one may be a checkout in
    progress, the user's or a concurrent run's). A provisional without a
    usable ``created`` timestamp on the route is treated as stale.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        bookings: Booking items from the bookings API.
        route: (origin uic, destination uic) of the configured route; None
            matches any route.
        now: Aware current time (defaults to sweden_now()), for the age test.

    Returns:
        Filtered list: neither provisionals (cancelled or spared) nor
        cancelled bookings — a provisional is never a booking.

    """
    now = now or sweden_now()
    valid_bookings = []
    printed_trail = False
    for item in bookings:
        booking = item.get("booking") or {}
        b_id = _booking_id(item, booking)
        b_num = booking.get("bookingNumber") or b_id

        if is_stale_provisional(booking):
            if not _on_route(booking, route):
                logger.info(
                    f"provisional booking {b_num} is not on the configured route, leaving it"
                )
                continue
            age = _provisional_age(booking, now)
            if age is not None and age < STALE_PROVISIONAL_GRACE:
                printed_trail = True
                minutes = max(0, int(age.total_seconds() // 60))
                pwarn(f"leaving recent provisional booking {b_num} alone (created {minutes}m ago)")
                continue
            # A failure shows as ✗ on the trail and a warning with the cause;
            # the run goes on (the provisional is retried next time).
            printed_trail = True
            try:
                with spinner(f"cancelling stale provisional booking {b_num}"):
                    client.cancel_provisional_booking(access_token, b_id)
            except Exception as e:
                logger.warning(f"failed to cancel provisional booking {b_id}: {e}")
                pwarn(
                    f"could not cancel stale provisional booking {b_num} "
                    f"({error_text(e)}), continuing"
                )
            continue

        if booking.get("bookingStatus") == "CANCELLED":
            continue

        valid_bookings.append(item)

    if printed_trail:
        blank()  # separate the cleanup trail from the day cards that follow
    logger.info(f"found {len(valid_bookings)} active bookings after cleanup")
    return valid_bookings


def _resolve_leg(
    client: SJClient,
    access_token: str,
    params: dict,
    passenger_token: str,
    search_id: str,
    leg: str,
) -> dict:
    """
    Search one leg, pick the best departure and find a 0-price offer for it.

    Shared by dry-run and booking mode: everything up to (but excluding) the
    actual booking call is identical. If the closest departure has no offer,
    one alternative is tried (earlier for outbound, later for inbound).

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        params: cfg["search_parameters"].
        passenger_token: Passenger token for the offers call.
        search_id: Departure search ID for this leg.
        leg: "outbound" or "inbound".

    Returns:
        {"found": False} when no departure exists at all; otherwise
        {"found": True, "has_offer": bool, "departure": "HH:MM",
         "arrival": "HH:MM", "duration": "4h 37m", "train": "X 2000 520",
         "route": "A → B", "class": str, "offer_id": str | None,
         "alternative": bool} describing the chosen departure (the
         alternative one if that is where the offer was found).

    """
    outbound = leg == "outbound"
    label = "outbound" if outbound else "return"
    origin, dest = params["station_from"], params["station_to"]
    route = f"{origin} → {dest}" if outbound else f"{dest} → {origin}"
    target_time = params["time_leave"] if outbound else params.get("time_return", "17:00")
    requested_class = params["comfort_class"]
    flexibility = params.get("flexibility", "FULLFLEX")
    allow_fallback = params.get("allow_class_fallback", True)

    def describe(dep: dict) -> dict:
        legs = dep.get("legs") or [{}]
        brand = legs[0].get("serviceBrandNameDescription") or ""
        number = legs[0].get("publicServiceName") or legs[0].get("serviceName") or ""
        return {
            "duration": format_duration(dep.get("duration", "")),
            "train": " ".join(x for x in (brand, number) if x),
            "route": route,
        }

    with spinner(f"searching {label} at {target_time}"):
        best = poll_and_select(
            client,
            access_token,
            search_id,
            target_time,
            requested_class,
            params.get("select_closest_ticket_available", False),
            allow_fallback,
        )
    if not best:
        return {"found": False}

    if best["diff"] != 0:
        pwarn(
            f"no exact match for {target_time}, closest is {best['time_str']} ({best['diff']:+d}m)"
        )
    logger.info(f"selected {label}: {best['time_str']}")

    with spinner(f"checking offers for {label} at {best['time_str']}"):
        offers = client.get_offers(access_token, best["id"], passenger_token)
    offer_result = find_offer_id(offers, best["class"], flexibility, allow_fallback)
    if offer_result:
        offer_id, matched_class = offer_result
        if matched_class != best["class"]:
            pwarn(f"{label} class fallback: {best['class']} → {matched_class}")
        return {
            "found": True,
            "has_offer": True,
            "departure": best["time_str"],
            "arrival": _get_arrival_time(best["departure"]),
            **describe(best["departure"]),
            "class": matched_class,
            "offer_id": offer_id,
            "alternative": False,
        }

    # Another departure is only an option when the config allows a different
    # time at all; with exact time only, an offer-less exact match is the end.
    alt = None
    if params.get("select_closest_ticket_available", False):
        pwarn(f"no valid offer for {label} at {best['time_str']}, trying closest alternative")
        alt = _try_alternative_departure(
            client,
            access_token,
            search_id,
            target_time,
            requested_class,
            flexibility,
            allow_fallback,
            passenger_token,
            best["id"],
            prefer_earlier=outbound,
        )
    else:
        pwarn(f"no valid offer for {label} at {best['time_str']} (exact time only)")
    if alt:
        return {
            "found": True,
            "has_offer": True,
            "departure": alt["time_str"],
            "arrival": alt["arrival"],
            **describe(alt["departure"]),
            "class": alt["class"],
            "offer_id": alt["offer_id"],
            "alternative": True,
        }
    return {
        "found": True,
        "has_offer": False,
        "departure": best["time_str"],
        "arrival": _get_arrival_time(best["departure"]),
        **describe(best["departure"]),
        "class": best["class"],
        "offer_id": None,
        "alternative": False,
    }


def _dry_run_leg(resolved: dict, flexibility: str) -> dict:
    """Dry-run row for a resolved leg (see _resolve_leg)."""
    if not resolved["found"]:
        return {
            "departure": "—",
            "arrival": "—",
            "class": "—",
            "flexibility": "—",
            "has_offer": False,
        }
    return {
        "departure": resolved["departure"],
        "arrival": resolved["arrival"],
        "duration": resolved.get("duration", ""),
        "train": resolved.get("train", ""),
        "route": resolved.get("route", ""),
        "class": resolved["class"],
        "flexibility": flexibility if resolved["has_offer"] else None,
        "has_offer": resolved["has_offer"],
    }


def _booking_from_response(resp: dict) -> tuple[str | None, str | None, dict]:
    """
    Pull (booking_id, booking_number, booking) out of a booking API response.

    The API wraps the booking: {"bookingId": ..., "booking": {"bookingNumber": ...,
    "journeys": [...]}}; older/other shapes put the fields at the top level.
    """
    inner = resp.get("booking")
    booking = inner if isinstance(inner, dict) else resp
    booking_id = resp.get("bookingId") or booking.get("bookingId") or resp.get("id")
    number = booking.get("bookingNumber") or resp.get("bookingNumber")
    return booking_id, number, booking


def _create_provisional(
    client: SJClient, access_token: str, passenger_token: str, resolved: dict, leg: str
) -> tuple[str | None, str | None, dict]:
    """Create a provisional booking from a resolved leg. Returns (id, number, booking)."""
    alt = "alternative " if resolved["alternative"] else ""
    with spinner(f"creating booking with {alt}{leg} at {resolved['departure']}"):
        b_resp = client.create_provisional_booking(
            access_token, resolved["offer_id"], passenger_token
        )
    return _booking_from_response(b_resp)


def _segment_label(segment: dict, segments: list[dict]) -> str:
    """'outbound' / 'return', plus the train number when a direction has several."""
    direction = segment.get("direction")
    label = "outbound" if direction == "OUTBOUND" else "return"
    same = [s for s in segments if s.get("direction") == direction]
    if len(same) > 1:
        label = f"{label} {segment.get('publicServiceName') or segment.get('serviceName') or ''}"
    return label.strip()


def _train_label(segment: dict, segments: list[dict]) -> str:
    """
    Name a leg by its train, for bookings we did not create.

    `direction` is only meaningful within one booking: a return trip booked on
    its own is `OUTBOUND` too, so labelling an existing booking's legs by it
    prints "outbound" over a Stockholm → Linköping card. The day card already
    names the route, so the useful thing to add is which train it is.
    """
    name = segment.get("publicServiceName") or segment.get("serviceName") or ""
    brand = segment.get("serviceBrandNameDescription") or ""
    return f"{brand} {name}".strip() or _segment_label(segment, segments)


def _seats_locked(seatmap: dict) -> bool:
    """
    Whether this seat map refuses a change.

    API-null rule: `canChangeSeat` is a tri-state — true, false, or absent /
    an explicit null. Only an explicit false (or a departed train) blocks the
    attempt; "not stated" is deliberately read as "try anyway", because a
    refusal costs one `!` line while reading it as "locked" would silently
    skip every seat the API happened to null.
    """
    return bool(seatmap.get("hasDeparted")) or seatmap.get("canChangeSeat") is False


def _seat_update(segment: dict, seat: Seat) -> dict:
    """One updateSegmentSeats entry.

    Keys: seatNumber, seatStrategy, direction, carriageNumber, serviceIdentifier.
    """
    return {
        "seatNumber": seat["number"],
        "seatStrategy": "EXACT",
        "direction": segment.get("direction"),
        "carriageNumber": seat["carriage"],
        "serviceIdentifier": segment.get("serviceIdentifier"),
    }


# Module state for one run: EOF or a missing terminal stops all further
# prompts, so a 40-day run cannot hang or ask the same dead question twice.
_ask_disabled = False


def _reset_seat_prompts() -> None:
    """Re-enable seat prompts (called once per run)."""
    global _ask_disabled  # noqa: PLW0603
    _ask_disabled = False


def _ask_for_seat(seatmap: dict, label: str) -> Seat | None:
    """Prompt for one leg's seat. None keeps the seat SJ assigned."""
    global _ask_disabled  # noqa: PLW0603
    if _ask_disabled:
        return None
    if not sys.stdin.isatty():
        _ask_disabled = True
        pwarn("seat selection needs a terminal, keeping the seats SJ assigned")
        return None

    seats = free_seats(seatmap)
    if not seats:
        pwarn(f"no free seat to choose from for {label}")
        return None

    # Seat numbers repeat across carriages (74 of them on one X 2000 map), so
    # the seat is keyed on the pair and the prompt's default shows the pair —
    # which is also how the user types one: "3-31".
    by_key = {(s["carriage"], s["number"]): s for s in seats}
    carriage, number = current_seat(seatmap)
    default = f"{carriage}-{number}" if carriage and number else number or "?"
    comforts = {s["carriage"]: carriage_comfort(seatmap, s["carriage"]) for s in seats}
    print_seat_choices(seats, comforts)
    while True:
        answer = ask_optional(f"{label} seat [{default}]: ")
        if answer is None:  # Ctrl-D: keep this seat and stop asking for the run
            _ask_disabled = True
            return None
        answer = answer.strip()
        if not answer:  # empty line: keep this seat, keep asking on later legs
            return None
        wanted_carriage, sep, wanted_number = (p.strip() for p in answer.partition("-"))
        if sep and wanted_carriage and wanted_number:
            seat = by_key.get((wanted_carriage, wanted_number))
            if seat is not None:
                return seat
            pwarn(f"seat {answer} is not free, pick one from the list")
            continue
        matches = [s for s in seats if s["number"] == answer]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            pwarn(f"seat {answer} is not free, pick one from the list")
            continue
        where = sorted({s["carriage"] for s in matches}, key=number_key)
        pwarn(f"seat {answer} is in carriages {', '.join(where)}, answer like {where[0]}-{answer}")


def _choose_seat(seatmap: dict, preference: list[str] | str, label: str) -> Seat | None:
    """The seat to take for one segment, or None to keep the current one."""
    # Any string is the "ask" literal — parse_preference normalises it and
    # rejects every other string. Guarding on the type rather than on the
    # value keeps a stray string from being iterated as a list of wishes.
    if isinstance(preference, str):
        return _ask_for_seat(seatmap, label)
    wishes = list(preference)
    seat = best_seat(seatmap, wishes)
    if seat is None:
        pwarn(f"no free seat to choose from for {label}")
        return None
    # best_seat is best-effort — name the top wish it could not honour
    missed = [w for w in wishes if not satisfies(seat, w)]
    if missed:
        pwarn(f"{label}: no {missed[0]} seat free, taking {describe_seat(seat)}")
    return seat


def _apply_seat_preference(
    client: SJClient,
    access_token: str,
    booking_id: str,
    booking: dict,
    preference: list[str] | str,
    provisional: bool = True,
    label_fn: Callable[[dict, list[dict]], str] = _segment_label,
) -> tuple[dict, int]:
    """
    Choose seats for every segment of a booking and write them in one PATCH.

    Shared by --book (provisional path) and, from Task 6, the change-seat
    modes (confirmed path). Any failure degrades to "keep the seat SJ
    assigned": a seat is never worth losing a booking over, so nothing here
    raises — every error is caught, logged and reported with `pwarn`.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        booking_id: The booking's UUID.
        booking: The booking object as returned by the legs (journeys/segments).
        preference: The `seat_preference` config value: "ask" or a ranked
            word list. "ask" prompts once per segment; not a terminal (or
            Ctrl-D) degrades to keeping whatever seat SJ assigned.
        provisional: True while the booking is still a cart (the default,
            used by --book); False once it is confirmed.
        label_fn: How to name a leg in messages. Defaults to outbound/return,
            which is only meaningful inside a booking we created; the
            change-seat modes pass `_train_label` instead.

    Returns:
        (booking, seats_changed) — the updated booking when a PATCH
        succeeded (trusting the API's response over the request), the one
        passed in otherwise, and how many of the requested seats the
        response confirms.

    """
    segments = [
        seg for journey in booking.get("journeys") or [] for seg in journey.get("segments") or []
    ]
    updates = []
    for segment in segments:
        label = label_fn(segment, segments)
        search_id = segment.get("seatMapSearchId")
        if not (segment.get("seatMapAvailable") and search_id):
            logger.info(f"no seat map for {label}")
            continue
        try:
            with spinner(f"reading the seat map for {label}", trail=False):
                seatmap = client.get_seatmap(access_token, booking_id, search_id)
        except Exception as e:
            logger.warning(f"seat map failed for {label}: {e}")
            pwarn(f"could not read the seat map for {label}: {error_text(e)}")
            continue
        if _seats_locked(seatmap):
            logger.info(f"seats cannot be changed for {label}")
            pdim(f"{label}: seat cannot be changed")
            continue

        seat = _choose_seat(seatmap, preference, label)
        if seat is None:
            continue
        if (seat["carriage"], seat["number"]) == current_seat(seatmap):
            pdim(f"{label}: already on the best seat")
            continue
        updates.append(_seat_update(segment, seat))

    if not updates:
        return booking, 0

    try:
        with spinner(f"choosing {len(updates)} seat(s)"):
            resp = client.update_seats(access_token, booking_id, updates, provisional=provisional)
    except Exception as e:
        logger.warning(f"seat update failed: {e}")
        pwarn(f"seats not changed: {error_text(e)}")
        return booking, 0

    _, _, updated = _booking_from_response(resp)
    if not updated:
        # An empty body: the PATCH was accepted and there is nothing to check
        # it against, so the requested seats are the best answer we have.
        return booking, len(updates)

    # Trust the response over the request: the API may hand back a different
    # seat than the one asked for.
    got: set[tuple[Any, Any]] = set()
    for journey in updated.get("journeys") or []:
        for seg in journey.get("segments") or []:
            for product in seg.get("requiredProducts") or []:
                seat_info = product.get("seat") or {}
                got.add((seat_info.get("carriageNumber"), seat_info.get("number")))

    confirmed = 0
    for update in updates:
        if (update["carriageNumber"], update["seatNumber"]) in got:
            confirmed += 1
        else:
            pwarn(
                f"asked for carriage {update['carriageNumber']} seat {update['seatNumber']}, "
                "the booking says otherwise"
            )
    # Confirmed, never requested: "N seat(s) changed" must not contradict the
    # "the booking says otherwise" line printed just above it.
    return updated, confirmed


def handle_booking_process(
    client: SJClient,
    access_token: str,
    cfg: dict,
    passenger_token: str,
    out_search_id: str | None,
    in_search_id: str | None,
    dry_run: bool = False,
) -> dict | None:
    """
    Resolve and book the outbound and/or inbound leg, then check out.

    Pass a search ID for each leg that should be handled; None skips it. With
    both IDs the return leg is added to the outbound booking (one booking
    number); with only in_search_id the return leg is booked on its own (the
    API sees a one-way search as "outbound").

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        cfg: The full config dict.
        passenger_token: Passenger token for booking.
        out_search_id: Outbound search ID, or None to skip the outbound leg.
        in_search_id: Inbound search ID, or None to skip the inbound leg.
        dry_run: If True, collect results without booking.

    Returns:
        Dry-run result dict in dry-run mode. In booking mode, a dict with
        "booking_id", "booking_number", "legs" (["outbound"], ["return"] or
        both), "checked_out" (False if the provisional booking was created
        but checkout failed) and "booking" (the booking object from the API,
        with journeys/segments, for display) when a booking was created;
        None when nothing was booked.

    """
    params = cfg["search_parameters"]
    flexibility = params.get("flexibility", "FULLFLEX")

    booking_id = None
    booking_number = None
    booking: dict = {}
    legs: list[str] = []
    dry_run_result = {}

    # 1. Outbound
    if out_search_id:
        out = _resolve_leg(client, access_token, params, passenger_token, out_search_id, "outbound")
        if dry_run:
            dry_run_result["outbound"] = _dry_run_leg(out, flexibility)
        elif not out["found"]:
            pwarn("no departure found for outbound")
            return None
        elif not out["has_offer"]:
            # Nothing can be booked without an outbound offer; the caller
            # handles the partial (return-leg-only) fallback.
            return None
        else:
            booking_id, booking_number, booking = _create_provisional(
                client, access_token, passenger_token, out, "outbound"
            )
            legs.append("outbound")

    # 2. Inbound
    if in_search_id:
        try:
            inb = _resolve_leg(
                client, access_token, params, passenger_token, in_search_id, "inbound"
            )
            if dry_run:
                row = _dry_run_leg(inb, flexibility)
                # Book mode books nothing from a roundtrip search whose
                # outbound is unavailable (unless book_partial books the
                # return on its own), so the preview must say the same.
                out_row = dry_run_result.get("outbound")
                if (
                    out_row is not None
                    and not out_row["has_offer"]
                    and row["has_offer"]
                    and not params.get("book_partial", False)
                ):
                    row.update(has_offer=False, flexibility=None, blocked="needs book_partial")
                dry_run_result["inbound"] = row
            elif not inb["found"]:
                if not booking_id:
                    pwarn("no departure found for inbound")
                    return None
                pwarn("no departure found for inbound, booking outbound only")
            elif not inb["has_offer"]:
                if not booking_id:
                    return None
                pinfo("no alternative found, booking outbound only")
            elif booking_id:
                alt = "alternative " if inb["alternative"] else ""
                with spinner(f"adding {alt}return leg at {inb['departure']}"):
                    resp = client.add_offer_to_booking(
                        access_token, booking_id, inb["offer_id"], passenger_token
                    )
                _, _, updated = _booking_from_response(resp or {})
                if updated.get("journeys"):
                    booking = updated
                legs.append("return")
            else:
                booking_id, booking_number, booking = _create_provisional(
                    client, access_token, passenger_token, inb, "return"
                )
                legs.append("return")
        except Exception as e:
            if dry_run or not booking_id:
                raise
            # SPEC §8.2: the outbound provisional is already held — keep it
            # and check it out alone rather than leave it to the cleanup.
            logger.error(f"return leg failed: {e}")
            pwarn(f"return leg failed ({error_text(e)}), booking outbound only")

    if dry_run:
        return dry_run_result

    # 3. Checkout
    if not booking_id:
        logger.error("failed to create booking")
        return None

    # Seats before checkout: a seat is chosen on the still-provisional cart,
    # never worth losing the booking over — _apply_seat_preference never
    # raises. Kept out of the checkout spinner below (spinner() isn't
    # reentrant: nesting would stomp its single "is a spinner active" flag).
    preference = params.get("seat_preference")
    if preference:
        booking, _ = _apply_seat_preference(
            client, access_token, booking_id, booking, preference, provisional=True
        )

    checked_out = True
    try:
        email = cfg["auth"]["email"]
        # The API already put the holder's contact details on the provisional;
        # send those back rather than invent a number. None = leave it as is.
        phone = (booking.get("customer") or {}).get("phoneNumber") or next(
            (p.get("phoneNumber") for p in booking.get("passengers") or [] if p.get("phoneNumber")),
            None,
        )
        with spinner(f"checking out booking {booking_number or booking_id}"):
            client.update_booking_customer(access_token, booking_id, email, phone)
            client.checkout_booking(access_token, booking_id)
    except Exception as e:
        # The provisional booking stays; it is cleaned up as stale on the next
        # --book run (SPEC §6.2).
        logger.error(f"checkout failed for booking {booking_id}: {e}")
        pinfo(f"checkout failed: {error_text(e)}")
        checked_out = False

    return {
        "booking_id": booking_id,
        "booking_number": booking_number,
        "legs": legs,
        "checked_out": checked_out,
        "booking": booking,
    }


def _search_and_book_one_way(
    client: SJClient,
    access_token: str,
    cfg: dict,
    from_station: str,
    to_station: str,
    date_str: str,
    tp_product_id: str,
    tp_token_id: str,
    service_types: list | None,
    is_outbound: bool,
    dry_run: bool = False,
) -> dict | None:
    """
    Search and book a single one-way leg independently.

    Used by partial booking as a fallback when the roundtrip's outbound leg
    could not be booked: the return leg is searched and booked as a
    standalone one-way trip (the API needs an outbound offer to create a
    booking, so a roundtrip search's return offer cannot be used alone).

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        cfg: The full config dict.
        from_station: Departure station name.
        to_station: Arrival station name.
        date_str: Date string (YYYY-MM-DD).
        tp_product_id: Travel pass product ID.
        tp_token_id: Travel pass token ID.
        service_types: Service type filter or None.
        is_outbound: True for outbound leg, False for inbound.
        dry_run: If True, collect results without booking.

    Returns:
        Dry-run result dict if in dry-run mode, None otherwise.

    """
    direction = "outbound" if is_outbound else "inbound"
    logger.info(f"searching {direction} one-way: {from_station} → {to_station}")

    search_resp = client.search_journey(
        access_token,
        from_station,
        to_station,
        date_str,
        None,
        tp_product_id,
        service_types,
    )
    passenger_token = search_resp.get("passengerListId") or tp_token_id
    search_id = search_resp.get("departureSearchId")

    if not search_id:
        logger.warning(f"no search ID returned for {direction}")
        return None

    if is_outbound:
        return handle_booking_process(
            client, access_token, cfg, passenger_token, search_id, None, dry_run
        )
    # Inbound as one-way: the API sees it as outbound
    return handle_booking_process(
        client, access_token, cfg, passenger_token, None, search_id, dry_run
    )


def plan_day(
    client: SJClient, params: dict, existing_bookings: list, date_str: str
) -> tuple[bool, bool]:
    """
    Decide which legs still need booking on a date.

    Returns:
        (need_outbound, need_inbound). For a one-way config need_inbound is
        always False. Both False means the day is already fully booked.

    """
    origin_id = client.resolve_station(params["station_from"])
    dest_id = client.resolve_station(params["station_to"])
    do_roundtrip = params.get("roundtrip", False)

    has_outbound = check_existing_booking(existing_bookings, origin_id, dest_id, date_str)
    has_inbound = (
        check_existing_booking(existing_bookings, dest_id, origin_id, date_str)
        if do_roundtrip
        else False
    )
    return not has_outbound, do_roundtrip and not has_inbound


def day_route(params: dict, need_outbound: bool, need_inbound: bool) -> str:
    """Route label for a day card: 'A ⇄ B', 'A → B' or 'B → A'."""
    origin, dest = params["station_from"], params["station_to"]
    if need_outbound and need_inbound:
        return f"{origin} \u21c4 {dest}"
    if need_inbound:
        return f"{dest} \u2192 {origin}"
    return f"{origin} \u2192 {dest}"


def process_booking_flow(
    client: SJClient,
    access_token: str,
    cfg: dict,
    current_date: date,
    tp_product_id: str,
    tp_token_id: str,
    need_outbound: bool,
    need_inbound: bool,
    dry_run: bool = False,
) -> dict | None:
    """
    Search and book the legs a day still needs (see plan_day).

    Returns:
        Dry-run result dict in dry-run mode; in booking mode the result of
        handle_booking_process (None when nothing was booked).

    """
    params = cfg["search_parameters"]
    origin_name = params["station_from"]
    dest_name = params["station_to"]
    date_str = current_date.strftime("%Y-%m-%d")

    logger.info(f"processing date: {date_str}")

    if not (need_outbound or need_inbound):
        pinfo("tickets already booked")
        return None

    service_types = params.get("service_types")
    if service_types and service_types == ["ALL"]:
        service_types = None

    book_partial = params.get("book_partial", False)

    if need_outbound and need_inbound:
        # Roundtrip search → single booking (outbound + return leg), exactly
        # like the SJ app. If the return leg fails the outbound is still kept.
        # If the *outbound* fails nothing can be booked from a roundtrip
        # search (the API needs an outbound offer to create a booking), so
        # with book_partial we fall back to a standalone one-way return leg.
        logger.info("booking roundtrip (both legs missing)")
        search_resp = client.search_journey(
            access_token,
            origin_name,
            dest_name,
            date_str,
            date_str,
            tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        out_id = search_resp.get("departureSearchId")
        in_id = search_resp.get("returnDepartureSearchId")

        if not (out_id and in_id):
            logger.error("failed to get both outbound and inbound search IDs")
            pinfo("search returned no result ids")
            return None

        result = handle_booking_process(
            client, access_token, cfg, passenger_token, out_id, in_id, dry_run
        )
        if dry_run or result or not book_partial:
            return result

        # Nothing booked (outbound unavailable): try the return leg on its own
        pinfo("outbound unavailable, trying return leg as a separate booking")
        return _search_and_book_one_way(
            client,
            access_token,
            cfg,
            dest_name,
            origin_name,
            date_str,
            tp_product_id,
            tp_token_id,
            service_types,
            is_outbound=False,
            dry_run=dry_run,
        )

    if need_outbound:
        if params.get("roundtrip", False):
            pinfo("return already booked, searching outbound only")
        logger.info("booking outbound only")
        return _search_and_book_one_way(
            client,
            access_token,
            cfg,
            origin_name,
            dest_name,
            date_str,
            tp_product_id,
            tp_token_id,
            service_types,
            is_outbound=True,
            dry_run=dry_run,
        )

    # Inbound only: swap origin/dest
    pinfo("outbound already booked, searching return only")
    logger.info("booking inbound only (return leg)")
    return _search_and_book_one_way(
        client,
        access_token,
        cfg,
        dest_name,
        origin_name,
        date_str,
        tp_product_id,
        tp_token_id,
        service_types,
        is_outbound=False,
        dry_run=dry_run,
    )


def _dry_run_note(leg: dict) -> str:
    """Why a dry-run leg could not be booked ("" when it could)."""
    if leg.get("has_offer"):
        return ""
    if leg.get("blocked"):
        return str(leg["blocked"])
    if leg.get("departure", "\u2014") == "\u2014":
        return "no departure found"
    return "no 0-price offer"


def _dry_run_rows(result: dict, date_str: str) -> list[dict]:
    """Leg rows for the dry-run day card, in outbound/return order."""
    rows = []
    for key, direction in (("outbound", "Outbound"), ("inbound", "Return")):
        leg = result.get(key)
        if not leg:
            continue
        rows.append(
            {
                "date": date_str,
                "direction": direction,
                "departure": leg.get("departure", "\u2014"),
                "arrival": leg.get("arrival", "\u2014"),
                "duration": leg.get("duration", ""),
                "train": leg.get("train", ""),
                "route": leg.get("route", ""),
                "comfort_class": leg.get("class", "\u2014"),
                "flexibility": leg.get("flexibility") or "",
                "note": _dry_run_note(leg),
                "has_offer": bool(leg.get("has_offer")),
            }
        )
    return rows


def _booked_rows(booking: dict, booking_number: str | None) -> list[dict]:
    """Leg rows for a freshly booked day, from the booking object the API returned."""
    now = sweden_now()
    rows = []
    for journey in booking.get("journeys") or []:
        for segment in journey.get("segments") or []:
            row = _segment_to_display_row(segment, booking_number or "\u2014", now)
            row["_sort_key"] = segment.get("departureDateTime") or ""
            rows.append(row)
    rows.sort(key=lambda r: r.pop("_sort_key", ""))
    return rows


def _span_label(start: date, end: date) -> str:
    """Span label: "18 – 21 sep 2026", "1 sep – 30 oct 2026", "29 dec 2026 – 9 jan 2027"."""

    def day(d: date, with_year: bool) -> str:
        txt = f"{d.day} {d.strftime('%b').lower()}"
        return f"{txt} {d.year}" if with_year else txt

    if start == end:
        return day(start, True)
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} \u2013 {day(end, True)}"
    return f"{day(start, start.year != end.year)} \u2013 {day(end, True)}"


def describe_run(params: dict, today: date | None = None) -> list[tuple[str, str]]:
    """
    Run-header facts derived from the config, in the shared card grammar.

    Args:
        params: The validated [search_parameters] section.
        today: Swedish date deciding the ISO year of bare week terms (defaults to now).

    Example:
        route     Göteborg Central ⇄ Stockholm Central
        days      1 sep – 30 oct 2026 · weekdays only
        days      W43, W45..46 (19 oct – 15 nov 2026) · weekdays only
        times     out 06:59 · back 17:22
        ticket    2 class calm · FULLFLEX · SJ High-speed train

    """
    roundtrip = params.get("roundtrip", False)
    arrow = "\u21c4" if roundtrip else "\u2192"
    selected = selected_dates(params, today)
    first, last = selected[0], selected[-1]
    contiguous = (last - first).days + 1 == len(selected)
    span = _span_label(first, last)
    days_value = span if contiguous else f"{params['dates']} ({span})"
    skip_w, skip_h = params.get("skip_weekends", True), params.get("skip_holidays", True)
    days = {
        (True, True): "weekdays only",
        (True, False): "weekdays incl. red days",
        (False, True): "every day except red days",
        (False, False): "every day",
    }[(skip_w, skip_h)]
    times = [f"out {params.get('time_leave')}"]
    if roundtrip:
        times.append(f"back {params.get('time_return')}")

    parts = [params.get("comfort_class", ""), params.get("flexibility", "")]
    service_types = params.get("service_types") or []
    if service_types and service_types != ["ALL"]:
        parts.append(", ".join(SERVICE_TYPE_NAMES.get(t, t) for t in service_types))
    if not params.get("select_closest_ticket_available", False):
        parts.append("exact time only")
    if not params.get("allow_class_fallback", True):
        parts.append("no class fallback")
    if params.get("book_partial", False):
        parts.append("partial ok")
    return [
        ("route", f"{params['station_from']} {arrow} {params['station_to']}"),
        ("days", f"{days_value} \u00b7 {days}"),
        ("times", " \u00b7 ".join(times)),
        ("ticket", " \u00b7 ".join(p for p in parts if p)),
    ]


def _run_summary(counts: dict[str, int], dry_run: bool) -> str:
    """Footer line: '12 day(s) · 9 booked · 1 already booked · 2 skipped'."""
    parts = [f"{counts['days']} day(s)"]
    labels = (
        ("booked", "bookable" if dry_run else "booked"),
        ("partial", "partly bookable" if dry_run else "partly booked"),
        ("unavailable", "unavailable" if dry_run else "not booked"),
        ("failed", "checkout failed"),
        ("error", "error(s)"),
        ("already", "already booked"),
        ("skipped", "skipped"),
    )
    parts += [f"{counts[k]} {label}" for k, label in labels if counts.get(k)]
    if dry_run:
        parts.insert(0, "dry run")
    return " \u00b7 ".join(parts)


def _run_outcome(counts: dict[str, int], dry_run: bool) -> bool | None:
    """
    Colour of the closing ●: green only when the run really booked something.

    A dry run changes nothing, and neither does a run where every day was
    already booked or skipped — those close dim, not green.
    """
    if counts.get("failed") or counts.get("error"):
        return False
    if dry_run:
        return None
    return True if (counts.get("booked") or counts.get("partial")) else None


def process_date_range(
    client: SJClient,
    access_token: str,
    token_manager: TokenManager,
    cfg: dict,
    tp_product_id: str,
    tp_token_id: str,
    existing_bookings: list,
    dry_run: bool = False,
    today: date | None = None,
) -> dict[str, int]:
    """
    Process every selected date, printing one card per day.

    Each day is a card: bold date + route header, the progress trail and
    messages indented beneath it, then the booked (or bookable) legs in the
    same shape as --list-bookings. Skipped and already-booked days are a
    single dim line. A summary footer closes the run.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        token_manager: Token manager for mid-run refresh.
        cfg: The full config dict.
        tp_product_id: Travel pass product ID.
        tp_token_id: Travel pass token ID.
        existing_bookings: List of existing bookings.
        dry_run: If True, collect results without booking.
        today: Today's Swedish date (defaults to now); selected dates before
            it are dropped with a note, so a standing selection keeps working
            on later runs.

    Returns:
        The day counts behind the summary line: "days" plus any of "booked",
        "partial", "unavailable", "failed" (checkout failed), "error" (an
        exception while processing the day), "already", "skipped". The
        caller maps "failed"/"error" to exit code 1 (SPEC §5.7).

    """
    _reset_seat_prompts()  # a fresh run always starts willing to ask
    params = cfg["search_parameters"]
    skip_weekends = params.get("skip_weekends", True)
    skip_holidays = params.get("skip_holidays", True)

    counts: dict[str, int] = {"days": 0}

    def count(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    # No leading blank: the caller prints one after the run-header facts,
    # before the bookings fetch, so the live spinner is already separated.
    today = today or sweden_now().date()
    dates = booking_dates(params, today)
    dropped = len(selected_dates(params, today)) - len(dates)
    if not dates:  # validated at startup; only a run straddling midnight gets here
        pstatus(False, "all selected dates have passed")
        count("error")
        return counts
    if dropped:
        pwarn(f"{dropped} selected day(s) have passed, starting from {dates[0].isoformat()}")
        blank()

    for i, day in enumerate(dates):
        date_str = day.isoformat()
        counts["days"] += 1

        reason = skip_reason(day, skip_weekends, skip_holidays)
        if reason:
            print_day_note(date_str, reason)
            blank()
            count("skipped")
            continue

        # Mid-run token refresh. Without a session nothing more can be
        # booked: report it on this day's card and stop, so the summary line
        # and the exit code still tell what happened.
        try:
            access_token = ensure_valid_token(client, token_manager, access_token)
        except SJAuthError as e:
            print_day_header(date_str, "")
            with indented():
                pinfo(f"error: {e}")
                pwarn("stopping: no valid session for the remaining dates")
            count("error")
            blank()
            break

        try:
            need_outbound, need_inbound = plan_day(client, params, existing_bookings, date_str)
        except Exception as e:
            logger.error(f"error planning {date_str}: {e}")
            print_day_header(date_str, "")
            with indented():
                pinfo(f"error: {error_text(e)}")
            count("error")
            blank()
            continue
        if not (need_outbound or need_inbound):
            print_day_note(date_str, "tickets already booked")
            blank()
            count("already")
            continue

        print_day_header(date_str, day_route(params, need_outbound, need_inbound))
        with indented():
            try:
                result = process_booking_flow(
                    client,
                    access_token,
                    cfg,
                    day,
                    tp_product_id,
                    tp_token_id,
                    need_outbound,
                    need_inbound,
                    dry_run,
                )
            except Exception as e:
                logger.error(f"error processing {date_str}: {e}")
                pinfo(f"error: {error_text(e)}")
                result = None
                errored = True
            else:
                errored = False

            if errored:
                count("error")
            elif dry_run:
                rows = _dry_run_rows(result or {}, date_str)
                print_leg_lines(rows)
                offers = sum(r["has_offer"] for r in rows)
                if rows and offers == len(rows):
                    count("booked")
                else:
                    count("partial" if offers else "unavailable")
            elif result:
                # The booking exists whatever the rendering does: an odd
                # shape in the booking object must not turn a booked day
                # into a crashed run.
                number = result.get("booking_number") or result.get("booking_id")
                try:
                    booked = _booked_rows(result.get("booking") or {}, result["booking_number"])
                    print_leg_lines(booked)
                except Exception as e:
                    logger.error(f"could not render the legs of booking {number}: {e}")
                    pwarn(f"booked as {number}, but the legs could not be shown ({error_text(e)})")
                if not result.get("checked_out"):
                    pwarn("checkout failed, provisional left (cleaned up on next --book run)")
                    count("failed")
                else:
                    wanted = int(need_outbound) + int(need_inbound)
                    count("booked" if len(result.get("legs") or []) >= wanted else "partial")
            else:
                pdim("nothing booked")
                count("unavailable")
        blank()

        if i + 1 < len(dates):
            with spinner("waiting before next date", trail=False):
                time.sleep(2)

    pstatus(_run_outcome(counts, dry_run), _run_summary(counts, dry_run))
    return counts


def _confirm(question: str) -> bool:
    """Ask a yes/no question on stdin; 'y' and 'yes' (any case) mean yes."""
    return ask(question).strip().lower() in ("y", "yes")


def handle_cancel_mode(
    client: SJClient,
    access_token: str,
    cfg: dict,
    cancel_date: str,
    dry_run: bool = False,
) -> bool:
    """
    Interactive cancellation for a specific date.

    Finds all bookings matching the configured route on the given date,
    then delegates to handle_cancel_booking for each matching booking.

    Returns:
        True when every matching booking was handled successfully, nothing
        matched (a date with no bookings is nothing to do, not a failure),
        or this is a dry run; False when any cancellation failed.

    """
    params = cfg["search_parameters"]
    origin_name = params["station_from"]
    dest_name = params["station_to"]
    origin_id = client.resolve_station(origin_name)
    dest_id = client.resolve_station(dest_name)

    with spinner(f"fetching bookings for {cancel_date}"):
        bookings = fetch_all_bookings(client, access_token, cancel_date, cancel_date)

    # Find booking numbers with a journey on the route (either direction)
    # that day — whole journeys, so a connection still matches.
    matched_numbers = set()
    for item in bookings:
        booking = item.get("booking") or {}
        if not is_active_booking(booking):
            continue
        for journey in booking.get("journeys") or []:
            j_origin, j_dest, j_date = _journey_endpoints(journey)
            if j_date != cancel_date:
                continue
            if (j_origin, j_dest) in {(origin_id, dest_id), (dest_id, origin_id)}:
                b_num = booking.get("bookingNumber")
                if b_num:
                    matched_numbers.add(b_num)

    if not matched_numbers:
        blank()
        pstatus(False, f"no bookings found for {cancel_date} on route {origin_name} → {dest_name}")
        return True

    # The bookings are already fetched, so no travel pass (date range) is
    # needed. Only that day's journeys are up for cancellation: a booking
    # made on sj.se may hold journeys on other days too.
    ok = True
    for b_num in sorted(matched_numbers):
        ok &= handle_cancel_booking(
            client,
            access_token,
            None,
            b_num,
            prefetched_bookings=bookings,
            dry_run=dry_run,
            only_date=cancel_date,
        )
    return ok


def handle_cancel_booking(
    client: SJClient,
    access_token: str,
    travel_pass: dict | None,
    booking_number: str,
    prefetched_bookings: list | None = None,
    dry_run: bool = False,
    only_date: str | None = None,
) -> bool:
    """
    Cancel a booking by its booking number.

    Fetches all bookings, finds the one matching the given booking number,
    displays its details, and asks for confirmation before cancelling.
    With dry_run, stops after the display: no prompts, no cancellation —
    the status line says what would be cancelled.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        travel_pass: Travel pass dict (used for date range), or None.
        booking_number: The booking number to cancel.
        prefetched_bookings: If provided, skip fetching and use these instead.
        dry_run: Preview only — never prompt, never call a cancel API.
        only_date: Swedish date (YYYY-MM-DD); when given, only the journeys
            departing that day are shown and offered for cancellation
            (--cancel-date), the booking's other days are left untouched.

    Returns:
        True when the requested cancellation happened, nothing needed
        cancelling, or this is a dry run; False when the booking was not
        found, the user declined, or the API refused (exit code 1).

    """
    if prefetched_bookings is not None:
        all_bookings = prefetched_bookings
    else:
        b_start, b_end = booking_date_range(travel_pass)
        with spinner(f"searching for booking {booking_number}"):
            all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)

    # Find the booking matching the booking number
    matched_item = None
    for item in all_bookings:
        booking = item.get("booking") or {}
        if not is_active_booking(booking):
            continue
        if booking.get("bookingNumber") == booking_number:
            matched_item = item
            break

    if not matched_item:
        blank()
        pstatus(False, f"no active booking found with number {booking_number}")
        return False

    booking = matched_item.get("booking") or {}
    b_id = _booking_id(matched_item, booking)

    # Check if there's a pending cancellation that needs to be resolved
    booking_status = booking.get("bookingStatus") or ""
    possible_actions = (booking.get("possibleActions") or []) + (
        booking.get("bookingPossibleActions") or []
    )
    if booking_status == "CHANGED" and "REVERT" in possible_actions:
        if dry_run:
            blank()
            pstatus(
                None,
                f"dry run · booking {booking_number} has a pending cancellation, nothing done",
            )
            return True
        pinfo(f"booking {booking_number} has a pending cancellation in progress")
        pinfo("  1. confirm the pending cancellation")
        pinfo("  2. revert (undo) the pending cancellation")
        pinfo("  3. do nothing")
        blank()
        choice = ask("select action [1/2/3]: ").strip()

        if choice == "1":
            try:
                client.finalize_cancellation(access_token, b_id)
            except Exception as e:
                pinfo(f"failed to confirm cancellation for {booking_number}: {error_text(e)}")
                return False
            pinfo(f"booking {booking_number} cancellation confirmed")
            return True
        if choice == "2":
            try:
                client.revert_booking(access_token, b_id)
            except Exception as e:
                pinfo(f"failed to revert booking {booking_number}: {error_text(e)}")
                return False
            pinfo(f"booking {booking_number} reverted to original state")
            return True
        pinfo("no action taken")
        return False

    now = sweden_now()

    # Collect segments for display and cancellation
    display_rows: list[dict[str, Any]] = []
    segments_to_cancel: list[dict[str, Any]] = []
    has_past_segment = False
    other_days = 0  # cancellable journeys on days other than only_date

    for journey in booking.get("journeys") or []:
        for seg in journey.get("segments") or []:
            row = _segment_to_display_row(seg, booking_number, now)
            service_id = seg.get("serviceIdentifier") or ""
            cancellable = row["past"] == "N" and bool(service_id)
            if only_date and row["date"] != only_date:
                other_days += cancellable
                continue
            display_rows.append(row)

            if row["past"] == "Y":
                has_past_segment = True

            # Collect cancellation metadata for future segments
            if cancellable:
                passengers = seg.get("passengers") or journey.get("passengers") or []
                passenger_ids = [
                    p.get("id") or p.get("passengerId")
                    for p in passengers
                    if p.get("id") or p.get("passengerId")
                ]
                if not passenger_ids:
                    passenger_ids = ["passenger_1"]

                segments_to_cancel.append(
                    {
                        "serviceIdentifier": service_id,
                        "passengerIds": passenger_ids,
                        "_row": row,
                    }
                )

    blank()
    print_bookings_table(display_rows, summary=False)
    if other_days:
        pdim(
            f"{other_days} other journey{'s' if other_days != 1 else ''} in booking "
            f"{booking_number} on other dates {'are' if other_days != 1 else 'is'} kept"
        )
    blank()

    if not segments_to_cancel:
        if has_past_segment:
            pstatus(False, "all segments are in the past, nothing to cancel")
        else:
            pinfo("no cancellable segments found")
        return True

    if dry_run:
        pstatus(
            None,
            f"dry run · {len(segments_to_cancel)} journey(s) "
            f"would be cancelled from booking {booking_number}",
        )
        return True

    # Select which segments to cancel
    if len(segments_to_cancel) == 1:
        question = (
            f"cancel this journey from booking {booking_number}? [y/n]: "
            if other_days
            else f"cancel booking {booking_number}? [y/n]: "
        )
        if not _confirm(question):
            blank()
            pstatus(False, "cancellation aborted")
            return False
        selected = segments_to_cancel
    else:
        pinfo("cancel which journey?")
        # same booking on every line, so drop the number from the choices
        choices = [{**seg["_row"], "booking_number": ""} for seg in segments_to_cancel]
        lines = leg_lines(choices)
        for i, line in enumerate(lines, 1):
            pinfo(f"  {i}. {line}")
        pinfo("  a. all")

        valid_nums = [str(i) for i in range(1, len(segments_to_cancel) + 1)]
        blank()
        choice = ask(f"select [{'/'.join(valid_nums)}/a]: ").strip()

        if choice.upper() == "A":
            selected = segments_to_cancel
        else:
            # Validate-first (like --cancel-date): every token must be a
            # listed number; duplicates collapse so the API gets each
            # journey once.
            parts = [p.strip() for p in choice.split(",") if p.strip()]
            if not parts or any(p not in valid_nums for p in parts):
                pinfo("invalid selection, aborting")
                return False
            indices = sorted({int(p) - 1 for p in parts})
            selected = [segments_to_cancel[i] for i in indices]

        # Confirm: show exactly what will be cancelled
        blank()
        pinfo("selected for cancellation:")
        with indented():
            print_leg_lines([{**seg["_row"], "booking_number": ""} for seg in selected])
        blank()
        if not _confirm("cancel selected journey(s)? [y/n]: "):
            blank()
            pstatus(False, "cancellation aborted")
            return False

    # Clean up internal labels before sending to API
    payload = [
        {"serviceIdentifier": s["serviceIdentifier"], "passengerIds": s["passengerIds"]}
        for s in selected
    ]

    # Step 1: cancel the journeys (PATCH); step 2: confirm the cancellation
    # (checkout). Either failure shows ✗ on the trail and names the cause;
    # after a failed step 2 the booking is left in pending cancellation,
    # which the next --cancel-booking offers to confirm or revert.
    initiated = False
    try:
        with spinner(f"cancelling booking {booking_number}"):
            client.cancel_booking_with_patch(access_token, b_id, payload)
            initiated = True
            client.finalize_cancellation(access_token, b_id)
    except Exception as e:
        blank()
        if initiated:
            pstatus(
                False,
                f"cancellation initiated but confirmation failed for {booking_number}: "
                f"{error_text(e)}",
            )
        else:
            pstatus(False, f"failed to cancel booking {booking_number}: {error_text(e)}")
        return False

    n_selected = len(selected)
    n_total = len(segments_to_cancel) + other_days
    blank()
    if n_selected == n_total:
        pstatus(True, f"booking {booking_number} cancelled")
    else:
        pstatus(
            True, f"{n_selected} of {n_total} journey(s) cancelled from booking {booking_number}"
        )
    return True


def _route_label(segments: list[dict]) -> str:
    """'A → B' for one direction, 'A ⇄ B' when the opposite direction is also present."""
    pairs = [
        (
            (seg.get("departureStation") or {}).get("name") or "—",
            (seg.get("arrivalStation") or {}).get("name") or "—",
        )
        for seg in segments
    ]
    if not pairs:
        return "—"
    origin, dest = pairs[0]
    if any(pair == (dest, origin) for pair in pairs):
        return f"{origin} ⇄ {dest}"
    return f"{origin} → {dest}"


def _scoped_to(booking: dict, segments: list[dict]) -> dict:
    """
    `booking`, cut down to the legs `segments` covers.

    The seat PATCH answers with the whole booking, while a --change-seat-date
    target is one day of it: without this the card would print the days it
    never touched, under the target day's header. Segments are matched on
    (service, departure), and a response that carries none of them falls back
    to the segments asked about, so the card is never empty.
    """
    keys = {(seg.get("serviceIdentifier"), seg.get("departureDateTime")) for seg in segments}
    kept = [
        seg
        for journey in booking.get("journeys") or []
        for seg in journey.get("segments") or []
        if (seg.get("serviceIdentifier"), seg.get("departureDateTime")) in keys
    ]
    return {"journeys": [{"segments": kept or segments}]}


def _preview_seats(
    client: SJClient,
    access_token: str,
    booking_id: str,
    booking: dict,
    preference: list[str] | str,
    label_fn: Callable[[dict, list[dict]], str] = _train_label,
) -> None:
    """
    Dry-run seat preview: report what --change-seat would do, without writing.

    Mirrors _apply_seat_preference's segment walk (same seat-map fetch, same
    hasDeparted/canChangeSeat skip) but only reads: it never calls
    update_seats, and in "ask" mode it never prompts — it reports the
    current seat and how many are free instead.
    """
    segments = [
        seg for journey in booking.get("journeys") or [] for seg in journey.get("segments") or []
    ]
    for segment in segments:
        label = label_fn(segment, segments)
        search_id = segment.get("seatMapSearchId")
        if not (segment.get("seatMapAvailable") and search_id):
            logger.info(f"no seat map for {label}")
            continue
        try:
            with spinner(f"reading the seat map for {label}", trail=False):
                seatmap = client.get_seatmap(access_token, booking_id, search_id)
        except Exception as e:
            logger.warning(f"seat map failed for {label}: {e}")
            pwarn(f"could not read the seat map for {label}: {error_text(e)}")
            continue
        if _seats_locked(seatmap):
            logger.info(f"seats cannot be changed for {label}")
            pdim(f"{label}: seat cannot be changed")
            continue

        if isinstance(preference, str):  # "ask": read-only, never prompt in a dry run
            carriage, number = current_seat(seatmap)
            current = f"carriage {carriage} seat {number}" if carriage else "no assigned seat"
            pdim(f"{label}: currently {current} · {len(free_seats(seatmap))} free seat(s)")
            continue

        seat = best_seat(seatmap, list(preference))
        if seat is None:
            pwarn(f"no free seat to choose from for {label}")
            continue
        if (seat["carriage"], seat["number"]) == current_seat(seatmap):
            pdim(f"{label}: already on the best seat")
            continue
        pinfo(f"{label}: would take {describe_seat(seat)}")


def handle_change_seat(
    client: SJClient,
    access_token: str,
    cfg: dict,
    dates: list[str] | None = None,
    booking_numbers: list[str] | None = None,
    travel_pass: dict | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Re-seat existing bookings, by date (configured route) or by booking number.

    Mirrors the cancel pair's scoping: dates take that day's journeys on the
    configured route (handle_cancel_mode's matching), an explicit booking
    number takes every leg of that booking whatever route it runs
    (handle_cancel_booking's not-found reporting). Seats are chosen exactly
    as in --book (_apply_seat_preference) and written through the
    confirmed-booking endpoint (provisional=False).

    A segment that has already departed, or whose seat map reports
    canChangeSeat: false, is left alone and noted rather than attempted.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        cfg: The full config dict.
        dates: Swedish dates (YYYY-MM-DD); each day's journeys on the
            configured route (either direction) are re-seated.
        booking_numbers: Explicit booking numbers; every leg of each is
            re-seated, whatever route it runs — a number is already an
            explicit target, so no route filter applies.
        travel_pass: Travel pass dict, for the date range a booking-number
            search covers (see booking_date_range); None uses the fallback
            window.
        dry_run: Read seat maps and report the choice, but never PATCH.

    Returns:
        True when everything matched was handled (or nothing matched at
        all); False when seat_preference is missing, a named booking was not
        found, or a seat change raised while being applied.

    """
    _reset_seat_prompts()  # a fresh run always starts willing to ask
    params = cfg["search_parameters"]
    # The CLI validates the key before we get here (require_seat_preference),
    # but do not let a missing one quietly turn into "ask": an unattended run
    # would sit on a prompt it can never answer.
    preference = params.get("seat_preference")
    if not preference:
        pstatus(False, 'no seat_preference in [search_parameters] (set it to "ask" or a word list)')
        return False

    # (booking item, booking, matched date or None) — None means "every leg",
    # used for the booking-number path where no date scoping applies.
    targets: list[tuple[dict, dict, str | None]] = []
    ok = True
    reported = False  # a per-target ● already said how this run ended

    if dates:
        origin_id = client.resolve_station(params["station_from"])
        dest_id = client.resolve_station(params["station_to"])
        for day in dates:
            with spinner(f"fetching bookings for {day}", trail=False):
                day_bookings = fetch_all_bookings(client, access_token, day, day)
            # Same matching as handle_cancel_mode: whole journeys (a change
            # still matches), on the route in either direction, that date.
            matched_numbers = set()
            for item in day_bookings:
                booking = item.get("booking") or {}
                if not is_active_booking(booking):
                    continue
                for journey in booking.get("journeys") or []:
                    j_origin, j_dest, j_date = _journey_endpoints(journey)
                    if j_date != day:
                        continue
                    if (j_origin, j_dest) in {(origin_id, dest_id), (dest_id, origin_id)}:
                        b_num = booking.get("bookingNumber")
                        if b_num:
                            matched_numbers.add(b_num)
            for item in day_bookings:
                booking = item.get("booking") or {}
                if booking.get("bookingNumber") in matched_numbers:
                    targets.append((item, booking, day))

    if booking_numbers:
        b_start, b_end = booking_date_range(travel_pass)
        with spinner("fetching bookings", trail=False):
            all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)
        by_number: dict[str, tuple[dict, dict]] = {}
        for item in all_bookings:
            booking = item.get("booking") or {}
            if not is_active_booking(booking):
                continue
            num = booking.get("bookingNumber")
            if num:
                by_number.setdefault(num, (item, booking))
        for number in booking_numbers:
            found = by_number.get(number)
            if not found:
                blank()
                pstatus(False, f"no active booking found with number {number}")
                reported = True
                ok = False
                continue
            targets.append((*found, None))

    any_target = bool(targets)
    seats_changed_total = 0
    now = sweden_now()

    for item, booking, matched_date in targets:
        booking_number = booking.get("bookingNumber") or "—"
        booking_id = _booking_id(item, booking)
        all_segs = [
            seg
            for journey in booking.get("journeys") or []
            for seg in journey.get("segments") or []
        ]
        # A date target scopes to that day's segments only — the booking's
        # other days are kept, exactly like --cancel-date's only_date.
        scoped = [
            seg
            for seg in all_segs
            if not matched_date or _segment_date(seg.get("departureDateTime", "")) == matched_date
        ]
        if not scoped:
            continue  # the journey that matched is gone by the time we re-read it

        dated = sorted(scoped, key=lambda s: s.get("departureDateTime") or "")
        header_date = matched_date or _segment_date(dated[0].get("departureDateTime", ""))
        print_day_header(header_date, _route_label(scoped))
        with indented():
            workable = []
            for seg in scoped:
                row = _segment_to_display_row(seg, booking_number, now)
                if row["past"] == "Y":
                    pdim(f"{_train_label(seg, all_segs)}: already departed, skipped")
                    continue
                workable.append(seg)

            if not workable:
                pdim("nothing to change")
                blank()
                continue

            target_booking = {"journeys": [{"segments": workable}]}
            try:
                if dry_run:
                    _preview_seats(client, access_token, booking_id, target_booking, preference)
                else:
                    updated, changed = _apply_seat_preference(
                        client,
                        access_token,
                        booking_id,
                        target_booking,
                        preference,
                        provisional=False,
                        label_fn=_train_label,
                    )
                    if changed:
                        seats_changed_total += changed
                        print_leg_lines(_booked_rows(_scoped_to(updated, workable), booking_number))
                    else:
                        pdim("no seats changed")
            except Exception as e:
                logger.error(f"seat change failed for booking {booking_number}: {e}")
                pwarn(f"seat change failed for booking {booking_number}: {error_text(e)}")
                ok = False
        blank()

    if seats_changed_total:
        pstatus(True, f"{seats_changed_total} seat(s) changed")
    elif any_target:
        pstatus(None, "dry run · nothing to change" if dry_run else "nothing changed")
    elif not reported:
        # One closing ● per operation: an unresolved booking number already
        # printed its own, so do not follow it with "no bookings matched".
        pstatus(False, "no bookings matched")

    return ok


def _assigned_seat_details(seatmap: dict) -> list[str]:
    """
    Display words for the seat assigned in one seat map.

    Looks the assigned seat up in the carriage layout (`seats.assigned_seat`)
    so it can report computed properties — currently just `single` — the
    same way a free seat's card does. Falls back to
    `passengerSeats[0].carriageSeatProperties` — the map's one entry scoped
    to the assigned seat, read via property code only — when the lookup
    fails (an unfamiliar map shape must degrade, not crash). Empty when
    there is no assigned seat or none of its codes are recognised (both
    count as "unavailable" to the caller).
    """
    seat = assigned_seat(seatmap)
    if seat is not None:
        return seat_words(seat)
    assigned = next((s for s in seatmap.get("passengerSeats") or [] if isinstance(s, dict)), None)
    if assigned is None:
        return []
    codes = [
        code
        for p in assigned.get("carriageSeatProperties") or []
        if isinstance(p, dict) and (code := p.get("code"))
    ]
    return assigned_seat_words(codes)


def _add_seat_details(
    client: SJClient,
    access_token: str,
    tasks: list[tuple[dict, str, str]],
) -> None:
    """
    Fetch each eligible segment's seat map once and append its characteristics.

    Appends " · <words>" to that row's seat cell, for --seat-details.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        tasks: (row, booking_id, seatMapSearchId) for every segment eligible
            for a seat-map fetch (see handle_list_bookings). Fetches are
            cached by (booking_id, seatMapSearchId) so a map shared by two
            legs is only fetched once.

    A map that will not load, an empty passengerSeats, or no recognisable
    property code all leave the row's plain seat cell untouched — a seat
    detail is never worth breaking the listing over. Individual causes are
    logged; the failures are reported as one aggregated `pwarn` afterwards,
    not one per leg.

    """
    cache: dict[tuple[str, str], dict | None] = {}
    failures = 0
    with spinner("fetching seat details", trail=False):
        for row, booking_id, search_id in tasks:
            key = (booking_id, search_id)
            if key not in cache:
                try:
                    cache[key] = client.get_seatmap(access_token, booking_id, search_id)
                except Exception as e:
                    logger.warning(f"seat map failed for booking {booking_id}: {e}")
                    cache[key] = None
            seatmap = cache[key]
            words = _assigned_seat_details(seatmap) if seatmap is not None else []
            if not words:
                failures += 1
                continue
            row["seat"] = f"{row['seat']} · {', '.join(words)}"

    if failures:
        pwarn(f"seat details unavailable for {failures} leg(s)")


def handle_list_bookings(
    client: SJClient,
    access_token: str,
    travel_pass: dict,
    seat_details: bool = False,
) -> None:
    """
    Fetch and display all active bookings per SPEC §5.4 (the caller prints the title).

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        travel_pass: The active travel pass, for the booking date range.
        seat_details: With True, fetch the seat map for every not-yet-departed
            segment that has one and append its seat's characteristics
            (window, aisle, table, forward/backward) to the seat cell — one
            extra request per eligible leg (see _add_seat_details).

    """
    b_start, b_end = booking_date_range(travel_pass)

    with spinner("fetching bookings", trail=False):
        all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)

    # Transform raw API items into display rows
    now = sweden_now()
    display_rows = []
    seat_tasks: list[tuple[dict, str, str]] = []
    for item in all_bookings:
        booking = item.get("booking") or {}
        if not is_active_booking(booking):
            continue  # cancelled, or a stale provisional --book will clean up

        booking_number = booking.get("bookingNumber") or "—"
        booking_id = _booking_id(item, booking)
        for journey in booking.get("journeys") or []:
            for segment in journey.get("segments") or []:
                row = _segment_to_display_row(segment, booking_number, now)
                row["_sort_key"] = segment.get("departureDateTime") or ""
                display_rows.append(row)
                search_id = segment.get("seatMapSearchId")
                if (
                    seat_details
                    and segment.get("seatMapAvailable")
                    and search_id
                    and row["past"] == "N"
                ):
                    seat_tasks.append((row, booking_id, search_id))

    if not display_rows:
        pstatus(False, f"no bookings found between {b_start} and {b_end}")
        return

    if seat_tasks:
        _add_seat_details(client, access_token, seat_tasks)

    # Sort by date, then departure time
    display_rows.sort(key=lambda r: r.pop("_sort_key", ""))

    print_bookings_table(display_rows)


def _get_arrival_time(departure: dict) -> str:
    """Extract arrival time string from a departure dict."""
    try:
        arr_dt = departure.get("arrivalDateTime", "")
        return arr_dt.split("T")[1][:5]
    except (IndexError, AttributeError):
        return "—"
