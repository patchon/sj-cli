"""Booking business logic for the SJ API client."""

import logging
import time
from datetime import datetime, timedelta

from sj_auth import ensure_valid_token
from sj_calendar import skip_reason, sweden_now, to_sweden
from sj_client import SJClient
from sj_config import SERVICE_TYPE_NAMES
from sj_errors import SJAPIError
from sj_output import (
    ask,
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
    pstatus,
    pwarn,
    spinner,
)
from sj_token import TokenManager

logger = logging.getLogger(__name__)


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
    prod_name = segment.get("productFamily", {}).get("name", "—")
    dep_station = segment.get("departureStation", {}).get("name", "—")
    arr_station = segment.get("arrivalStation", {}).get("name", "—")

    # Train: brand + public service number, e.g. "X 2000 537"
    brand = segment.get("serviceBrandNameDescription") or segment.get("serviceType", {}).get(
        "name", ""
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
        p.get("code") for leg in departure.get("legs", []) for p in leg.get("serviceProperties", [])
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
    dep = _find_departure_by_time(departures, target_time_str, select_closest)
    if not dep:
        return None

    time_str = dep.get("departureDateTime", "").split("T")[1][:5]

    valid_class = _resolve_class_for_departure(dep, requested_class, allow_fallback)
    if not valid_class:
        pwarn(f"departure at {time_str}: no matching class available")
        return None

    if valid_class != requested_class:
        pwarn(f"departure at {time_str}: {requested_class} unavailable, using {valid_class}")

    target_minutes = time_str_to_minutes(target_time_str)
    dep_minutes = get_departure_time_minutes(dep)
    diff = dep_minutes - target_minutes

    return {
        "departure": dep,
        "class": valid_class,
        "diff": diff,
        "time_str": time_str,
        "id": dep.get("departureId"),
    }


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
    seat_offers = offer_response.get("seatOffers", {})
    offers = seat_offers.get("offers", {})
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

    api_to_display = {
        "SECOND": "2 class",
        "SECOND_CALM": "2 class calm",
        "FIRST": "1 class",
    }

    # Iterate in preference order, not API dict order — the API lists SECOND
    # before SECOND_CALM, so walking offers.items() would always pick plain
    # 2 class even when calm is available.
    target_classes = class_map.get(requested_class, list(offers.keys()))

    for comf_key in target_classes:
        offer_data = offers.get(comf_key)
        if offer_data:
            flexibilities = offer_data.get("flexibilities", {})

            for flex_type, flex_data in flexibilities.items():
                if flex_type != requested_flexibility:
                    continue

                if not flex_data.get("available"):
                    logger.warning(f"found {comf_key}/{flex_type} but it is not available")
                    continue

                prices = flex_data.get("journeyPrices", {})
                price_obj = prices.get("price", {})
                amount = price_obj.get("amount")

                try:
                    amount_val = float(amount)
                except (TypeError, ValueError):
                    amount_val = -1

                if amount_val == 0:
                    matched_class = api_to_display.get(comf_key, requested_class)
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
    travels = results.get("travels", [])
    if not travels:
        return None
    departures = travels[0].get("departures", [])

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
        # Filter: only earlier (diff < 0) or only later (diff > 0)
        if prefer_earlier and diff >= 0:
            continue
        if not prefer_earlier and diff <= 0:
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
    departures = []
    for attempt in range(5):
        if attempt:
            time.sleep(1.0)
        results = client.get_search_results(access_token, search_id)
        travels = results.get("travels", [])
        if travels:
            departures = travels[0].get("departures", [])
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
    return status == "NEW" and "CANCEL_JOURNEY" in booking.get("possibleActions", [])


def check_existing_booking(bookings: list, origin_id: str, dest_id: str, date_str: str) -> bool:
    """Check if a (non-cancelled, non-provisional) booking exists for the route and date."""
    for item in bookings:
        booking_details = item.get("booking", {})

        if booking_details.get("bookingStatus") == "CANCELLED" or is_stale_provisional(
            booking_details
        ):
            continue

        for journey in booking_details.get("journeys", []):
            for leg in journey.get("segments", []):
                l_origin = leg.get("departureStation", {}).get("uicStationCode")
                l_dest = leg.get("arrivalStation", {}).get("uicStationCode")

                dt_str = leg.get("departureDateTime", "")
                if not dt_str:
                    continue
                l_date = dt_str.split("T")[0]

                if l_origin == origin_id and l_dest == dest_id and l_date == date_str:
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
    bookings_list = []
    page = 0
    while True:
        bookings_resp = client.get_bookings(access_token, start_date, end_date, page)
        page_bookings = bookings_resp.get("bookings", [])
        bookings_list.extend(page_bookings)

        next_page = bookings_resp.get("nextPage")
        if next_page is None or next_page == page:
            break

        page = next_page
        time.sleep(0.5)

    return bookings_list


def cleanup_stale_provisionals(client: SJClient, access_token: str, bookings: list) -> list:
    """
    Cancel stale provisional bookings and return the filtered list.

    Any booking with status "NEW" and "CANCEL_JOURNEY" in possibleActions
    is a stale provisional from a previous interrupted run.

    Returns:
        Filtered list with stale provisionals removed.

    """
    valid_bookings = []
    printed_trail = False
    for item in bookings:
        booking = item.get("booking", {})
        b_id = booking.get("bookingId") or booking.get("id") or item.get("bookingId")
        b_num = booking.get("bookingNumber") or b_id

        if is_stale_provisional(booking):
            # cancel_provisional_booking() swallows errors and returns False;
            # raise inside the spinner so the trail shows ✗ and we can report it.
            printed_trail = True
            try:
                with spinner(f"cancelling stale provisional booking {b_num}"):
                    if not client.cancel_provisional_booking(access_token, b_id):
                        raise SJAPIError(f"cancel request for {b_num} was not accepted")
            except SJAPIError as e:
                logger.warning(f"failed to cancel provisional booking {b_id}: {e}")
                pwarn(f"could not cancel stale provisional booking {b_num}, continuing")
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
    target_time = params.get("time_leave") if outbound else params.get("time_return", "17:00")
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
            "departure": "—", "arrival": "—", "class": "—", "flexibility": "—", "has_offer": False
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
    booking = resp.get("booking") if isinstance(resp.get("booking"), dict) else resp
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
        inb = _resolve_leg(client, access_token, params, passenger_token, in_search_id, "inbound")
        if dry_run:
            dry_run_result["inbound"] = _dry_run_leg(inb, flexibility)
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

    if dry_run:
        return dry_run_result

    # 3. Checkout
    if not booking_id:
        logger.error("failed to create booking")
        return None

    checked_out = True
    try:
        email = cfg["auth"]["email"]
        phone = cfg["auth"].get("phone", "+46700000000")
        with spinner(f"checking out booking {booking_number or booking_id}"):
            client.update_booking_customer(access_token, booking_id, email, phone)
            client.checkout_booking(access_token, booking_id)
    except Exception as e:
        # The provisional booking stays; it is cleaned up as stale on the next
        # --book run (SPEC §6.2).
        logger.error(f"checkout failed for booking {booking_id}: {e}")
        pinfo(f"checkout failed: {e}")
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
    current_date: datetime,
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
    for journey in booking.get("journeys", []):
        for segment in journey.get("segments", []):
            row = _segment_to_display_row(segment, booking_number or "\u2014", now)
            row["_sort_key"] = segment.get("departureDateTime", "")
            rows.append(row)
    rows.sort(key=lambda r: r.pop("_sort_key", ""))
    return rows


def _span_label(start: datetime, end: datetime) -> str:
    """Span label: "18 - 21 sep 2026", "1 sep - 30 oct 2026", "29 dec 2026 - 9 jan 2027"."""
    def day(d: datetime, with_year: bool) -> str:
        txt = f"{d.day} {d.strftime('%b').lower()}"
        return f"{txt} {d.year}" if with_year else txt

    if start.date() == end.date():
        return day(start, True)
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day} \u2013 {day(end, True)}"
    return f"{day(start, start.year != end.year)} \u2013 {day(end, True)}"


def describe_run(params: dict) -> list[tuple[str, str]]:
    """
    Run-header facts derived from the config, in the shared card grammar.

    Example:
        route     Linköping Central ⇄ Stockholm Central
        days      1 sep - 30 oct 2026 · weekdays only
        times     out 06:59 · back 17:22
        ticket    2 class calm · FULLFLEX · SJ High-speed train

    """
    roundtrip = params.get("roundtrip", False)
    arrow = "\u21c4" if roundtrip else "\u2192"
    start = datetime.strptime(params["date_start"], "%Y-%m-%d")
    end = datetime.strptime(params["date_end"], "%Y-%m-%d")
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
        ("days", f"{_span_label(start, end)} \u00b7 {days}"),
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
        ("already", "already booked"),
        ("skipped", "skipped"),
    )
    parts += [f"{counts[k]} {label}" for k, label in labels if counts.get(k)]
    if dry_run:
        parts.insert(0, "dry run")
    return " \u00b7 ".join(parts)


def process_date_range(
    client: SJClient,
    access_token: str,
    token_manager: TokenManager,
    cfg: dict,
    tp_product_id: str,
    tp_token_id: str,
    existing_bookings: list,
    dry_run: bool = False,
) -> list[dict]:
    """
    Process all dates in the configured range, printing one card per day.

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

    Returns:
        List of dry-run leg rows (empty if not dry-run).

    """
    params = cfg["search_parameters"]
    start_date = datetime.strptime(params["date_start"], "%Y-%m-%d")
    end_date = datetime.strptime(params["date_end"], "%Y-%m-%d")
    skip_weekends = params.get("skip_weekends", True)
    skip_holidays = params.get("skip_holidays", True)

    results: list[dict] = []
    counts: dict[str, int] = {"days": 0}
    curr = start_date

    def count(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    # No leading blank: the caller prints one after the run-header facts,
    # before the bookings fetch, so the live spinner is already separated.
    while curr <= end_date:
        date_str = curr.strftime("%Y-%m-%d")
        counts["days"] += 1

        reason = skip_reason(curr.date(), skip_weekends, skip_holidays)
        if reason:
            print_day_note(date_str, reason)
            blank()
            count("skipped")
            curr += timedelta(days=1)
            continue

        # Mid-run token refresh
        access_token = ensure_valid_token(client, token_manager, access_token)

        need_outbound, need_inbound = plan_day(client, params, existing_bookings, date_str)
        if not (need_outbound or need_inbound):
            print_day_note(date_str, "tickets already booked")
            blank()
            count("already")
            curr += timedelta(days=1)
            continue

        print_day_header(date_str, day_route(params, need_outbound, need_inbound))
        with indented():
            try:
                result = process_booking_flow(
                    client,
                    access_token,
                    cfg,
                    curr,
                    tp_product_id,
                    tp_token_id,
                    need_outbound,
                    need_inbound,
                    dry_run,
                )
            except Exception as e:
                logger.error(f"error processing {date_str}: {e}")
                pinfo(f"error: {e}")
                result = None

            if dry_run:
                rows = _dry_run_rows(result or {}, date_str)
                results.extend(rows)
                print_leg_lines(rows)
                offers = sum(r["has_offer"] for r in rows)
                if rows and offers == len(rows):
                    count("booked")
                else:
                    count("partial" if offers else "unavailable")
            elif result:
                booked = _booked_rows(result.get("booking") or {}, result["booking_number"])
                print_leg_lines(booked)
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

        curr += timedelta(days=1)
        if curr <= end_date:
            with spinner("waiting before next date", trail=False):
                time.sleep(2)

    pstatus(True, _run_summary(counts, dry_run))
    return results


def _confirm(question: str) -> bool:
    """Ask a yes/no question on stdin; 'y' and 'yes' (any case) mean yes."""
    return ask(question).strip().lower() in ("y", "yes")


def handle_cancel_mode(
    client: SJClient,
    access_token: str,
    cfg: dict,
    cancel_date: str,
) -> None:
    """
    Interactive cancellation for a specific date.

    Finds all bookings matching the configured route on the given date,
    then delegates to handle_cancel_booking for each matching booking.
    """
    params = cfg["search_parameters"]
    origin_name = params["station_from"]
    dest_name = params["station_to"]
    origin_id = client.resolve_station(origin_name)
    dest_id = client.resolve_station(dest_name)

    with spinner(f"fetching bookings for {cancel_date}"):
        bookings = fetch_all_bookings(client, access_token, cancel_date, cancel_date)

    # Find booking numbers that match the route and date
    matched_numbers = set()
    for item in bookings:
        booking = item.get("booking", {})
        if booking.get("bookingStatus") == "CANCELLED":
            continue

        for journey in booking.get("journeys", []):
            for seg in journey.get("segments", []):
                l_origin = seg.get("departureStation", {}).get("uicStationCode")
                l_dest = seg.get("arrivalStation", {}).get("uicStationCode")
                dt_str = seg.get("departureDateTime", "")
                if not dt_str:
                    continue
                l_date = dt_str.split("T")[0]

                if l_date != cancel_date:
                    continue

                if (l_origin == origin_id and l_dest == dest_id) or (
                    l_origin == dest_id and l_dest == origin_id
                ):
                    b_num = booking.get("bookingNumber")
                    if b_num:
                        matched_numbers.add(b_num)

    if not matched_numbers:
        blank()
        pstatus(False, f"no bookings found for {cancel_date} on route {origin_name} → {dest_name}")
        return

    # Build a minimal travel_pass dict for handle_cancel_booking
    # (it only needs endTravelValidityDateTime for date range, but we already
    # fetched bookings so we pass dates that cover our cancel_date)
    for b_num in sorted(matched_numbers):
        handle_cancel_booking(client, access_token, None, b_num, prefetched_bookings=bookings)


def handle_cancel_booking(
    client: SJClient,
    access_token: str,
    travel_pass: dict | None,
    booking_number: str,
    prefetched_bookings: list | None = None,
) -> None:
    """
    Cancel a booking by its booking number.

    Fetches all bookings, finds the one matching the given booking number,
    displays its details, and asks for confirmation before cancelling.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        travel_pass: Travel pass dict (used for date range), or None.
        booking_number: The booking number to cancel.
        prefetched_bookings: If provided, skip fetching and use these instead.

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
        booking = item.get("booking", {})
        if booking.get("bookingStatus") == "CANCELLED":
            continue
        if booking.get("bookingNumber") == booking_number:
            matched_item = item
            break

    if not matched_item:
        blank()
        pstatus(False, f"no active booking found with number {booking_number}")
        return

    booking = matched_item.get("booking", {})
    b_id = booking.get("bookingId") or booking.get("id") or matched_item.get("bookingId")

    # Check if there's a pending cancellation that needs to be resolved
    booking_status = booking.get("bookingStatus", "")
    possible_actions = booking.get("possibleActions", []) + booking.get(
        "bookingPossibleActions", []
    )
    if booking_status == "CHANGED" and "REVERT" in possible_actions:
        pinfo(f"booking {booking_number} has a pending cancellation in progress")
        pinfo("  1. confirm the pending cancellation")
        pinfo("  2. revert (undo) the pending cancellation")
        pinfo("  3. do nothing")
        blank()
        choice = ask("select action [1/2/3]: ").strip()

        if choice == "1":
            confirmed = client.finalize_cancellation(access_token, b_id)
            if confirmed:
                pinfo(f"booking {booking_number} cancellation confirmed")
            else:
                pinfo(f"failed to confirm cancellation for {booking_number}")
        elif choice == "2":
            reverted = client.revert_booking(access_token, b_id)
            if reverted:
                pinfo(f"booking {booking_number} reverted to original state")
            else:
                pinfo(f"failed to revert booking {booking_number}")
        else:
            pinfo("no action taken")
        return

    now = sweden_now()

    # Collect segments for display and cancellation
    display_rows = []
    segments_to_cancel = []
    has_past_segment = False

    for journey in booking.get("journeys", []):
        for seg in journey.get("segments", []):
            row = _segment_to_display_row(seg, booking_number, now)
            display_rows.append(row)

            if row["past"] == "Y":
                has_past_segment = True

            # Collect cancellation metadata for future segments
            service_id = seg.get("serviceIdentifier", "")
            if row["past"] == "N" and service_id:
                passenger_ids = [
                    p.get("id", p.get("passengerId", ""))
                    for p in seg.get("passengers", journey.get("passengers", []))
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
    blank()

    if not segments_to_cancel:
        if has_past_segment:
            pstatus(False, "all segments are in the past, nothing to cancel")
        else:
            pinfo("no cancellable segments found")
        return

    # Select which segments to cancel
    if len(segments_to_cancel) == 1:
        if not _confirm(f"cancel booking {booking_number}? [y/n]: "):
            blank()
            pstatus(False, "cancellation aborted")
            return
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
            # Parse comma-separated selection
            indices = []
            for raw_part in choice.split(","):
                stripped = raw_part.strip()
                if stripped in valid_nums:
                    indices.append(int(stripped) - 1)
            if not indices:
                pinfo("invalid selection, aborting")
                return
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
            return

    # Clean up internal labels before sending to API
    payload = [
        {"serviceIdentifier": s["serviceIdentifier"], "passengerIds": s["passengerIds"]}
        for s in selected
    ]

    with spinner(f"cancelling booking {booking_number}"):
        # Step 1: Provisional cancel; Step 2: confirm the cancellation (checkout)
        success = client.cancel_booking_with_patch(access_token, b_id, payload)
        confirmed = client.finalize_cancellation(access_token, b_id) if success else False

    if not success:
        pinfo(f"failed to cancel booking {booking_number}")
        return

    n_selected = len(selected)
    n_total = len(segments_to_cancel)
    if confirmed:
        if n_selected == n_total:
            blank()
            pstatus(True, f"booking {booking_number} cancelled")
        else:
            blank()
            pstatus(
                True,
                f"{n_selected} of {n_total} journey(s) cancelled from booking {booking_number}",
            )
    else:
        blank()
        pstatus(False, f"cancellation initiated but confirmation failed for {booking_number}")


def handle_list_bookings(
    client: SJClient,
    access_token: str,
    travel_pass: dict,
) -> None:
    """Fetch and display all active bookings per SPEC §5.4 (the caller prints the title)."""
    b_start, b_end = booking_date_range(travel_pass)

    with spinner("fetching bookings", trail=False):
        all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)

    # Transform raw API items into display rows
    now = sweden_now()
    display_rows = []
    for item in all_bookings:
        booking = item.get("booking", {})
        if booking.get("bookingStatus") == "CANCELLED":
            continue

        booking_number = booking.get("bookingNumber", "—")
        for journey in booking.get("journeys", []):
            for segment in journey.get("segments", []):
                row = _segment_to_display_row(segment, booking_number, now)
                row["_sort_key"] = segment.get("departureDateTime", "")
                display_rows.append(row)

    if not display_rows:
        pstatus(False, f"no bookings found between {b_start} and {b_end}")
        return

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
