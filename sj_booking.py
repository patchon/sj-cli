"""Booking business logic for the SJ API client."""

import logging
import time
from datetime import datetime, timedelta

from sj_auth import ensure_valid_token
from sj_calendar import skip_reason
from sj_client import SJClient
from sj_errors import SJAPIError
from sj_output import (
    format_class_name,
    format_duration,
    format_table,
    pinfo,
    print_bookings_table,
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
    now_date = datetime.now()
    b_start = (now_date + timedelta(days=start_offset_days)).strftime("%Y-%m-%d")

    valid_end = travel_pass.get("endTravelValidityDateTime") if travel_pass else None
    if valid_end:
        vp_end = datetime.fromisoformat(valid_end.replace("Z", "+00:00")).replace(tzinfo=None)
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
        now: Current datetime for past-detection.

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
        dep_parsed = datetime.fromisoformat(dep_dt.replace("Z", "+00:00")).replace(tzinfo=None)
        if dep_parsed < now:
            in_past = "Y"
    except (ValueError, AttributeError):
        pass

    try:
        date_str = dep_dt.split("T")[0]
        dep_time = dep_dt.split("T")[1][:5]
        arr_time = arr_dt.split("T")[1][:5]
    except (IndexError, AttributeError):
        date_str = dep_dt
        dep_time = dep_dt
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
        pinfo(f"departure at {time_str}: no matching class available")
        return None

    if valid_class != requested_class:
        pinfo(f"departure at {time_str}: {requested_class} unavailable, using {valid_class}")

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
    offer_response: dict, requested_class: str, requested_flexibility: str
) -> tuple[str, str] | None:
    """
    Parse the get_offers response and find the offerId for a 0-price offer.

    Args:
        offer_response: The offers API response dict.
        requested_class: Requested comfort class.
        requested_flexibility: Requested flexibility (FULLFLEX, SEMIFLEX, NOFLEX).

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
        "2 class calm": ["SECOND_CALM", "SECOND"],
        "1 class": ["FIRST"],
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
    result = find_offer_id(offers, valid_class, flexibility)
    if result:
        offer_id, matched_class = result
        pinfo(f"found offer at alternative departure {time_str}")
        if matched_class != valid_class:
            pinfo(f"  class fallback: {valid_class} → {matched_class}")
        return {
            "offer_id": offer_id,
            "time_str": time_str,
            "arrival": _get_arrival_time(dep),
            "class": matched_class,
            "departure": dep,
        }

    pinfo(f"alternative departure {time_str} also unavailable, skipping")
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

    Polls up to 5 times with 1-second intervals until departures appear.
    """
    departures = []
    for _ in range(5):
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
    for item in bookings:
        booking = item.get("booking", {})
        b_id = booking.get("bookingId") or booking.get("id") or item.get("bookingId")
        b_num = booking.get("bookingNumber") or b_id

        if is_stale_provisional(booking):
            # cancel_provisional_booking() swallows errors and returns False;
            # raise inside the spinner so the trail shows ✗ and we can report it.
            try:
                with spinner(f"cancelling stale provisional booking {b_num}"):
                    if not client.cancel_provisional_booking(access_token, b_id):
                        raise SJAPIError(f"cancel request for {b_num} was not accepted")
            except SJAPIError as e:
                logger.warning(f"failed to cancel provisional booking {b_id}: {e}")
                pinfo(f"could not cancel stale provisional booking {b_num}, continuing")
            continue

        if booking.get("bookingStatus") == "CANCELLED":
            continue

        valid_bookings.append(item)

    logger.info(f"found {len(valid_bookings)} active bookings after cleanup")
    return valid_bookings


def handle_booking_process(
    client: SJClient,
    access_token: str,
    cfg: dict,
    passenger_token: str,
    out_search_id: str | None,
    in_search_id: str | None,
    do_out: bool,
    do_in: bool,
    dry_run: bool = False,
    date_str: str = "",
) -> dict | None:
    """
    Handle the booking creation for outbound and/or inbound legs.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token.
        cfg: The full config dict.
        passenger_token: Passenger token for booking.
        out_search_id: Outbound search ID (None if not needed).
        in_search_id: Inbound search ID (None if not needed).
        do_out: Whether to book outbound.
        do_in: Whether to book inbound.
        dry_run: If True, collect results without booking.
        date_str: Date string for display purposes.

    Returns:
        Dry-run result dict in dry-run mode. In booking mode, a dict with
        "booking_id", "booking_number", "legs" (["outbound"], ["return"] or
        both) and "checked_out" (False if the provisional booking was created
        but checkout failed) when a booking was created; None when nothing
        was booked.

    """
    params = cfg["search_parameters"]
    flexibility = params.get("flexibility", "FULLFLEX")
    allow_fallback = params.get("allow_class_fallback", True)

    origin = params["station_from"]
    dest = params["station_to"]

    booking_id = None
    booking_number = None
    legs: list[str] = []
    dry_run_result = {}

    # 1. Outbound
    if do_out and out_search_id:
        target_time = params.get("time_leave")
        with spinner(f"searching {date_str}: {origin} → {dest} at {target_time}"):
            best_out = poll_and_select(
                client,
                access_token,
                out_search_id,
                target_time,
                params["comfort_class"],
                params.get("select_closest_ticket_available", False),
                allow_fallback,
            )
        if best_out and best_out["diff"] != 0:
            pinfo(
                f"no exact match for {target_time}, "
                f"closest is {best_out['time_str']} ({best_out['diff']:+d}m)"
            )
        if best_out:
            if dry_run:
                # Collect dry-run info: check offer availability
                with spinner(f"checking offers for outbound at {best_out['time_str']}"):
                    offers = client.get_offers(access_token, best_out["id"], passenger_token)
                offer_result = find_offer_id(offers, best_out["class"], flexibility)
                if not offer_result:
                    # Try alternative (earlier departure for outbound)
                    pinfo(
                        f"no valid offer found for outbound at {best_out['time_str']},"
                        f"looking for closest alternative"
                    )
                    alt = _try_alternative_departure(
                        client,
                        access_token,
                        out_search_id,
                        target_time,
                        params["comfort_class"],
                        flexibility,
                        allow_fallback,
                        passenger_token,
                        best_out["id"],
                        prefer_earlier=True,
                    )
                    if alt:
                        dry_run_result["outbound"] = {
                            "departure": alt["time_str"],
                            "arrival": alt["arrival"],
                            "class": alt["class"],
                            "flexibility": flexibility,
                            "has_offer": True,
                        }
                    else:
                        dry_run_result["outbound"] = {
                            "departure": best_out["time_str"],
                            "arrival": _get_arrival_time(best_out["departure"]),
                            "class": best_out["class"],
                            "flexibility": None,
                            "has_offer": False,
                        }
                else:
                    _, matched_class = offer_result
                    if matched_class != best_out["class"]:
                        pinfo(f"outbound class fallback: {best_out['class']} → {matched_class}")
                    dry_run_result["outbound"] = {
                        "departure": best_out["time_str"],
                        "arrival": _get_arrival_time(best_out["departure"]),
                        "class": matched_class,
                        "flexibility": flexibility,
                        "has_offer": True,
                    }
            else:
                logger.info(f"selected outbound: {best_out['time_str']}")
                with spinner(f"checking offers for outbound at {best_out['time_str']}"):
                    offers = client.get_offers(access_token, best_out["id"], passenger_token)
                offer_result = find_offer_id(offers, best_out["class"], flexibility)
                if offer_result:
                    offer_id, matched_class = offer_result
                    if matched_class != best_out["class"]:
                        pinfo(f"outbound class fallback: {best_out['class']} → {matched_class}")
                    with spinner(f"creating booking with outbound at {best_out['time_str']}"):
                        b_resp = client.create_provisional_booking(
                            access_token, offer_id, passenger_token
                        )
                    booking_id = b_resp.get("bookingId") or b_resp.get("id")
                    booking_number = b_resp.get("bookingNumber")
                    legs.append("outbound")
                else:
                    pinfo(
                        f"no valid offer found for outbound {origin} → {dest}"
                        f"at {best_out['time_str']}, looking for closest alternative"
                    )
                    alt = _try_alternative_departure(
                        client,
                        access_token,
                        out_search_id,
                        target_time,
                        params["comfort_class"],
                        flexibility,
                        allow_fallback,
                        passenger_token,
                        best_out["id"],
                        prefer_earlier=True,
                    )
                    if alt:
                        alt_time = alt["time_str"]
                        with spinner(f"creating booking with alternative outbound at {alt_time}"):
                            b_resp = client.create_provisional_booking(
                                access_token, alt["offer_id"], passenger_token
                            )
                        booking_id = b_resp.get("bookingId") or b_resp.get("id")
                        booking_number = b_resp.get("bookingNumber")
                        legs.append("outbound")
                    else:
                        # Nothing can be booked without an outbound offer; the
                        # caller handles the partial (return-leg-only) fallback.
                        return None
        elif dry_run:
            dry_run_result["outbound"] = {
                "departure": "—",
                "arrival": "—",
                "class": "—",
                "flexibility": "—",
                "has_offer": False,
            }
        else:
            pinfo("no departure found for outbound")
            return None

    # 2. Inbound
    if do_in and in_search_id:
        target_time = params.get("time_return", "17:00")
        with spinner(f"searching {date_str}: {dest} → {origin} at {target_time}"):
            best_in = poll_and_select(
                client,
                access_token,
                in_search_id,
                target_time,
                params["comfort_class"],
                params.get("select_closest_ticket_available", False),
                allow_fallback,
            )
        if best_in and best_in["diff"] != 0:
            pinfo(
                f"no exact match for {target_time}, "
                f"closest is {best_in['time_str']} ({best_in['diff']:+d}m)"
            )
        if best_in:
            if dry_run:
                with spinner(f"checking offers for inbound at {best_in['time_str']}"):
                    offers = client.get_offers(access_token, best_in["id"], passenger_token)
                offer_result = find_offer_id(offers, best_in["class"], flexibility)
                if not offer_result:
                    pinfo(
                        f"no valid offer found for inbound at {best_in['time_str']},"
                        f"looking for closest alternative"
                    )
                    alt = _try_alternative_departure(
                        client,
                        access_token,
                        in_search_id,
                        target_time,
                        params["comfort_class"],
                        flexibility,
                        allow_fallback,
                        passenger_token,
                        best_in["id"],
                        prefer_earlier=False,
                    )
                    if alt:
                        dry_run_result["inbound"] = {
                            "departure": alt["time_str"],
                            "arrival": alt["arrival"],
                            "class": alt["class"],
                            "flexibility": flexibility,
                            "has_offer": True,
                        }
                    else:
                        dry_run_result["inbound"] = {
                            "departure": best_in["time_str"],
                            "arrival": _get_arrival_time(best_in["departure"]),
                            "class": best_in["class"],
                            "flexibility": None,
                            "has_offer": False,
                        }
                else:
                    _, matched_class = offer_result
                    if matched_class != best_in["class"]:
                        pinfo(f"inbound class fallback: {best_in['class']} → {matched_class}")
                    dry_run_result["inbound"] = {
                        "departure": best_in["time_str"],
                        "arrival": _get_arrival_time(best_in["departure"]),
                        "class": matched_class,
                        "flexibility": flexibility,
                        "has_offer": True,
                    }
            else:
                logger.info(f"selected inbound: {best_in['time_str']}")
                with spinner(f"checking offers for inbound at {best_in['time_str']}"):
                    offers = client.get_offers(access_token, best_in["id"], passenger_token)
                offer_result = find_offer_id(offers, best_in["class"], flexibility)

                if offer_result:
                    offer_id, matched_class = offer_result
                    if matched_class != best_in["class"]:
                        pinfo(f"inbound class fallback: {best_in['class']} → {matched_class}")
                    if booking_id:
                        # Add return leg to existing booking
                        with spinner(f"adding return leg at {best_in['time_str']}"):
                            client.add_offer_to_booking(
                                access_token, booking_id, offer_id, passenger_token
                            )
                        legs.append("return")
                    elif not do_out:
                        # Inbound-only search (one-way): API treats as outbound
                        with spinner(f"creating booking with inbound at {best_in['time_str']}"):
                            b_resp = client.create_provisional_booking(
                                access_token,
                                offer_id,
                                passenger_token,
                            )
                        booking_id = b_resp.get("bookingId") or b_resp.get("id")
                        booking_number = b_resp.get("bookingNumber")
                        legs.append("return")
                else:
                    pinfo(
                        f"no valid offer found for inbound {dest} → {origin}"
                        f"at {best_in['time_str']}, looking for closest alternative"
                    )
                    alt = _try_alternative_departure(
                        client,
                        access_token,
                        in_search_id,
                        target_time,
                        params["comfort_class"],
                        flexibility,
                        allow_fallback,
                        passenger_token,
                        best_in["id"],
                        prefer_earlier=False,
                    )
                    if alt:
                        if booking_id:
                            with spinner(f"adding alternative return leg at {alt['time_str']}"):
                                client.add_offer_to_booking(
                                    access_token, booking_id, alt["offer_id"], passenger_token
                                )
                            legs.append("return")
                        elif not do_out:
                            alt_time = alt["time_str"]
                            with spinner(f"creating alternative inbound booking at {alt_time}"):
                                b_resp = client.create_provisional_booking(
                                    access_token,
                                    alt["offer_id"],
                                    passenger_token,
                                )
                            booking_id = b_resp.get("bookingId") or b_resp.get("id")
                            booking_number = b_resp.get("bookingNumber")
                            legs.append("return")
                    elif booking_id:
                        pinfo("no alternative found, booking outbound only")
                    else:
                        return None
        elif dry_run:
            dry_run_result["inbound"] = {
                "departure": "—",
                "arrival": "—",
                "class": "—",
                "flexibility": "—",
                "has_offer": False,
            }
        elif booking_id:
            pinfo("no departure found for inbound, booking outbound only")
        else:
            pinfo("no departure found for inbound")
            return None

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
            client,
            access_token,
            cfg,
            passenger_token,
            search_id,
            None,
            True,
            False,
            dry_run,
            date_str,
        )
    # Inbound as one-way: API sees it as outbound (do_out=False, do_in=True)
    return handle_booking_process(
        client,
        access_token,
        cfg,
        passenger_token,
        None,
        search_id,
        False,
        True,
        dry_run,
        date_str,
    )


def process_booking_flow(
    client: SJClient,
    access_token: str,
    cfg: dict,
    current_date: datetime,
    tp_product_id: str,
    tp_token_id: str,
    existing_bookings: list,
    dry_run: bool = False,
) -> dict | None:
    """
    Process booking for a single date.

    Returns:
        Dry-run result dict in dry-run mode; in booking mode the result of
        handle_booking_process (None when nothing was booked).

    """
    params = cfg["search_parameters"]

    origin_name = params["station_from"]
    dest_name = params["station_to"]

    origin_id = client.resolve_station(origin_name)
    dest_id = client.resolve_station(dest_name)

    date_str = current_date.strftime("%Y-%m-%d")
    do_roundtrip = params.get("roundtrip", False)

    logger.info(f"processing date: {date_str}")

    # Check duplicates
    has_outbound = check_existing_booking(existing_bookings, origin_id, dest_id, date_str)
    has_inbound = False
    if do_roundtrip:
        has_inbound = check_existing_booking(existing_bookings, dest_id, origin_id, date_str)

    if has_outbound and (not do_roundtrip or has_inbound):
        pinfo(f"{date_str}: already fully booked, skipping")
        return None

    req_outbound = not has_outbound
    req_inbound = do_roundtrip and not has_inbound

    if do_roundtrip and req_outbound and not req_inbound:
        pinfo(f"{date_str}: inbound already booked, searching outbound only")
    elif do_roundtrip and not req_outbound and req_inbound:
        pinfo(f"{date_str}: outbound already booked, searching inbound only")

    service_types = params.get("service_types")
    if service_types and service_types == ["ALL"]:
        service_types = None

    book_partial = params.get("book_partial", False)

    # Search
    if req_outbound and req_inbound:
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
            pinfo(f"{date_str}: search returned no result ids, skipping")
            return None

        result = handle_booking_process(
            client,
            access_token,
            cfg,
            passenger_token,
            out_id,
            in_id,
            True,
            True,
            dry_run,
            date_str,
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

    if req_outbound:
        logger.info("booking outbound only")
        search_resp = client.search_journey(
            access_token,
            origin_name,
            dest_name,
            date_str,
            None,
            tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        out_id = search_resp.get("departureSearchId")
        if out_id:
            return handle_booking_process(
                client,
                access_token,
                cfg,
                passenger_token,
                out_id,
                None,
                True,
                False,
                dry_run,
                date_str,
            )
        return None

    if req_inbound:
        # Inbound-only: swap origin/dest
        logger.info("booking inbound only (return leg)")
        search_resp = client.search_journey(
            access_token,
            dest_name,
            origin_name,
            date_str,
            None,
            tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        in_id = search_resp.get("departureSearchId")
        if in_id:
            return handle_booking_process(
                client,
                access_token,
                cfg,
                passenger_token,
                None,
                in_id,
                False,
                True,
                dry_run,
                date_str,
            )
        return None

    return None


def _dry_run_note(leg: dict) -> str:
    """Why a dry-run leg could not be booked ("" when it could)."""
    if leg.get("has_offer"):
        return ""
    if leg.get("departure", "\u2014") == "\u2014":
        return "no departure found"
    return "no 0-price offer"


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
    Process all dates in the configured range.

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
        List of dry-run result dicts (empty if not dry-run).

    """
    params = cfg["search_parameters"]
    start_date = datetime.strptime(params["date_start"], "%Y-%m-%d")
    end_date = datetime.strptime(params["date_end"], "%Y-%m-%d")
    skip_weekends = params.get("skip_weekends", True)
    skip_holidays = params.get("skip_holidays", True)

    results = []
    curr = start_date

    while curr <= end_date:
        date_str = curr.strftime("%Y-%m-%d")

        reason = skip_reason(curr.date(), skip_weekends, skip_holidays)
        if reason:
            pinfo(f"skipping {date_str} ({reason})")
            curr += timedelta(days=1)
            continue

        # Mid-run token refresh
        access_token = ensure_valid_token(client, token_manager, access_token)

        try:
            result = process_booking_flow(
                client,
                access_token,
                cfg,
                curr,
                tp_product_id,
                tp_token_id,
                existing_bookings,
                dry_run,
            )
            if dry_run and result:
                # Flatten outbound/inbound into separate rows
                if "outbound" in result:
                    out = result["outbound"]
                    results.append(
                        {
                            "date": date_str,
                            "direction": "Outbound",
                            "departure": out.get("departure", "—"),
                            "arrival": out.get("arrival", "—"),
                            "comfort_class": out.get("class", "—"),
                            "flexibility": out.get("flexibility") or "—",
                            "note": _dry_run_note(out),
                        }
                    )
                if "inbound" in result:
                    inb = result["inbound"]
                    results.append(
                        {
                            "date": date_str,
                            "direction": "Return",
                            "departure": inb.get("departure", "—"),
                            "arrival": inb.get("arrival", "—"),
                            "comfort_class": inb.get("class", "—"),
                            "flexibility": inb.get("flexibility") or "—",
                            "note": _dry_run_note(inb),
                        }
                    )
            elif result:
                legs = " + ".join(result.get("legs") or []) or "booking"
                ref = result.get("booking_number") or result.get("booking_id")
                if result.get("checked_out"):
                    pinfo(f"{date_str}: booked {legs} \u00b7 {ref}")
                else:
                    pinfo(f"{date_str}: {legs} {ref} created but checkout failed")
        except Exception as e:
            logger.error(f"error processing {date_str}: {e}")
            pinfo(f"error processing {date_str}: {e}")

        curr += timedelta(days=1)
        if curr <= end_date:
            next_date = curr.strftime("%Y-%m-%d")
            with spinner(f"waiting before processing {next_date}"):
                time.sleep(2)

    return results


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
        pinfo(f"no bookings found for {cancel_date} on route {origin_name} → {dest_name}")
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
        pinfo(f"no active booking found with number {booking_number}")
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
        choice = input("select action [1/2/3]: ").strip()

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

    now = datetime.now()

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

                dep_station = seg.get("departureStation", {}).get("name", "—")
                arr_station = seg.get("arrivalStation", {}).get("name", "—")
                segments_to_cancel.append(
                    {
                        "serviceIdentifier": service_id,
                        "passengerIds": passenger_ids,
                        "_label": (
                            f"{row['direction']}  {row['date']}  "
                            f"{row['departure']} → {row['arrival']}  "
                            f"{dep_station} → {arr_station}"
                        ),
                        "_date": row["date"],
                        "_direction": row["direction"],
                        "_dep_time": row["departure"],
                        "_arr_time": row["arrival"],
                        "_route": row["route"],
                    }
                )

    print_bookings_table(display_rows, f"Booking {booking_number}", summary=False)
    print()

    if not segments_to_cancel:
        if has_past_segment:
            pinfo("all segments are in the past, nothing to cancel")
        else:
            pinfo("no cancellable segments found")
        return

    # Select which segments to cancel
    if len(segments_to_cancel) == 1:
        confirm = input(f"cancel booking {booking_number}? [y/n]: ").strip().lower()
        if confirm != "y":
            pinfo("cancellation aborted")
            return
        selected = segments_to_cancel
    else:
        pinfo("cancel which journey?")
        for i, seg in enumerate(segments_to_cancel, 1):
            pinfo(f"  {i}. {seg['_label']}")
        pinfo("  a. all")

        valid_nums = [str(i) for i in range(1, len(segments_to_cancel) + 1)]
        choice = input(f"select [{'/'.join(valid_nums)}/a]: ").strip()

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

        # Confirm with table
        confirm_rows = [
            {
                "date": s.get("_date", ""),
                "direction": s.get("_direction", ""),
                "departure": s.get("_dep_time", ""),
                "arrival": s.get("_arr_time", ""),
                "route": s.get("_route", ""),
            }
            for s in selected
        ]
        confirm_headers = ["Date", "Direction", "Departure", "Arrival", "Route"]
        confirm_table_rows = [
            [r["date"], r["direction"], r["departure"], r["arrival"], r["route"]]
            for r in confirm_rows
        ]
        table = format_table(confirm_headers, confirm_table_rows, title="Selected for cancellation")
        print(f"\n{table}")
        confirm = input("cancel selected journey(s)? [y/n]: ").strip().lower()
        if confirm != "y":
            pinfo("cancellation aborted")
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
            pinfo(f"booking {booking_number} cancelled")
        else:
            pinfo(f"{n_selected} of {n_total} journey(s) cancelled from booking {booking_number}")
    else:
        pinfo(f"cancellation initiated but confirmation failed for {booking_number}")


def handle_list_bookings(
    client: SJClient,
    access_token: str,
    travel_pass: dict,
) -> None:
    """Fetch and display all active bookings per SPEC §5.4."""
    pass_name = travel_pass.get("name", "Travel Pass")

    b_start, b_end = booking_date_range(travel_pass)

    with spinner("fetching bookings"):
        all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)

    # Transform raw API items into display rows
    now = datetime.now()
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
        pinfo(f"no bookings found between {b_start} and {b_end}")
        return

    # Sort by date, then departure time
    display_rows.sort(key=lambda r: r.pop("_sort_key", ""))

    print_bookings_table(display_rows, pass_name)


def _get_arrival_time(departure: dict) -> str:
    """Extract arrival time string from a departure dict."""
    try:
        arr_dt = departure.get("arrivalDateTime", "")
        return arr_dt.split("T")[1][:5]
    except (IndexError, AttributeError):
        return "—"
