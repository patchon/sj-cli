"""Booking business logic for the SJ API client."""

import logging
import time
from datetime import datetime, timedelta

from sj_auth import ensure_valid_token
from sj_client import SJClient
from sj_output import format_table, pinfo, print_bookings_table
from sj_token import TokenManager

logger = logging.getLogger(__name__)


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
        for leg in departure.get("legs", [])
        for p in leg.get("serviceProperties", [])
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
        best_dep, diff = timed[0]
        time_str = best_dep.get("departureDateTime", "").split("T")[1][:5]
        if diff != 0:
            pinfo(f"no exact match for {target_time_str}, closest is {time_str} ({diff:+d}m)")
        return best_dep

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
        "abs_diff": abs(diff),
        "time_str": time_str,
        "id": dep.get("departureId"),
    }


def find_offer_id(
    offer_response: dict, requested_class: str, requested_flexibility: str
) -> str | None:
    """
    Parse the get_offers response and find the offerId for a 0-price offer.

    Args:
        offer_response: The offers API response dict.
        requested_class: Requested comfort class.
        requested_flexibility: Requested flexibility (FULLFLEX, SEMIFLEX, NOFLEX).

    Returns:
        Offer ID string if a matching 0-price offer is found, None otherwise.

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

    target_classes = class_map.get(requested_class, [])

    for comf_key, offer_data in offers.items():
        if comf_key in target_classes or not target_classes:
            flexibilities = offer_data.get("flexibilities", {})

            for flex_type, flex_data in flexibilities.items():
                if flex_type != requested_flexibility:
                    continue

                if not flex_data.get("available"):
                    logger.warning(
                        f"found {comf_key}/{flex_type} but it is not available"
                    )
                    continue

                prices = flex_data.get("journeyPrices", {})
                price_obj = prices.get("price", {})
                amount = price_obj.get("amount")

                try:
                    amount_val = float(amount)
                except (TypeError, ValueError):
                    amount_val = -1

                if amount_val == 0:
                    logger.info(f"found 0-price offer: {comf_key} - {flex_type}")
                    specific_offer_id = flex_data.get("offerId")
                    if specific_offer_id:
                        return specific_offer_id
                    logger.warning(
                        "found 0-price match but no offerId in flex object"
                    )
                    return None
                logger.warning(
                    f"offer found {comf_key}/{flex_type} but price is {amount} "
                    f"(expected 0), skipping"
                )

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


def check_existing_booking(
    bookings: list, origin_id: str, dest_id: str, date_str: str
) -> bool:
    """Check if a booking already exists for the given route and date."""
    for item in bookings:
        booking_details = item.get("booking", {})

        if booking_details.get("bookingStatus") == "CANCELLED":
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


def fetch_all_bookings(
    client: SJClient, access_token: str, start_date: str, end_date: str
) -> list:
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


def cleanup_stale_provisionals(
    client: SJClient, access_token: str, bookings: list
) -> list:
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
        status = booking.get("bookingStatus") or booking.get("status")
        possible_actions = booking.get("possibleActions", [])

        if status == "NEW" and "CANCEL_JOURNEY" in possible_actions:
            pinfo(f"cancelling stale provisional booking {b_id} ...")
            try:
                client.cancel_provisional_booking(access_token, b_id)
                logger.info(f"cancelled stale provisional booking {b_id}")
            except Exception as e:
                logger.warning(f"failed to cancel provisional booking {b_id}: {e}")
            continue

        if status == "CANCELLED":
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
        Dry-run result dict if in dry-run mode, None otherwise.

    """
    params = cfg["search_parameters"]
    flexibility = params.get("flexibility", "FULLFLEX")
    allow_fallback = params.get("allow_class_fallback", True)
    book_partial = params.get("book_partial", False)

    origin = params["station_from"]
    dest = params["station_to"]

    service_type_names = {
        "SJ_HIGH": "SJ High-speed train",
        "SJ_IC": "SJ InterCity",
        "SJ_REG": "SJ Regional",
        "SJ_NT": "SJ Night train",
        "X_TRAINOPS": "Other train operators",
        "X_PTA": "Public transport",
        "X_EXPBUS": "Express buses",
    }
    raw_types = params.get("service_types")
    if raw_types and raw_types != ["ALL"]:
        filter_label = " (filter: " + ", ".join(
            service_type_names.get(t, t) for t in raw_types
        ) + ")"
    else:
        filter_label = ""

    booking_id = None
    dry_run_result = {}

    # 1. Outbound
    if do_out and out_search_id:
        target_time = params.get("time_leave")
        pinfo(f"searching {date_str}: {origin} → {dest} at {target_time}{filter_label}")

        best_out = poll_and_select(
            client,
            access_token,
            out_search_id,
            target_time,
            params["comfort_class"],
            params.get("select_closest_ticket_available", False),
            allow_fallback,
        )
        if best_out:
            if dry_run:
                # Collect dry-run info: check offer availability
                offers = client.get_offers(access_token, best_out["id"], passenger_token)
                offer_id = find_offer_id(offers, best_out["class"], flexibility)
                dry_run_result["outbound"] = {
                    "departure": best_out["time_str"],
                    "arrival": _get_arrival_time(best_out["departure"]),
                    "class": best_out["class"],
                    "flexibility": flexibility if offer_id else None,
                    "has_offer": offer_id is not None,
                }
            else:
                logger.info(f"selected outbound: {best_out['time_str']}")
                offers = client.get_offers(access_token, best_out["id"], passenger_token)
                offer_id = find_offer_id(offers, best_out["class"], flexibility)
                if offer_id:
                    pinfo(f"creating booking with outbound at {best_out['time_str']} ...")
                    b_resp = client.create_provisional_booking(
                        access_token, offer_id, passenger_token
                    )
                    booking_id = b_resp.get("bookingId") or b_resp.get("id")
                else:
                    pinfo("no valid 0-price offer found for outbound, skipping date")
                    if not book_partial:
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
            if not book_partial:
                return None

    # 2. Inbound
    if do_in and in_search_id:
        target_time = params.get("time_return", "17:00")
        pinfo(f"searching {date_str}: {dest} → {origin} at {target_time}{filter_label}")

        best_in = poll_and_select(
            client,
            access_token,
            in_search_id,
            target_time,
            params["comfort_class"],
            params.get("select_closest_ticket_available", False),
            allow_fallback,
        )
        if best_in:
            if dry_run:
                offers = client.get_offers(access_token, best_in["id"], passenger_token)
                offer_id = find_offer_id(offers, best_in["class"], flexibility)
                dry_run_result["inbound"] = {
                    "departure": best_in["time_str"],
                    "arrival": _get_arrival_time(best_in["departure"]),
                    "class": best_in["class"],
                    "flexibility": flexibility if offer_id else None,
                    "has_offer": offer_id is not None,
                }
            else:
                logger.info(f"selected inbound: {best_in['time_str']}")
                offers = client.get_offers(access_token, best_in["id"], passenger_token)
                offer_id = find_offer_id(offers, best_in["class"], flexibility)

                if offer_id:
                    if booking_id:
                        # Add return leg to existing booking
                        pinfo(f"adding return leg at {best_in['time_str']} ...")
                        client.add_offer_to_booking(
                            access_token, booking_id, offer_id, passenger_token
                        )
                    elif not do_out or book_partial:
                        # Inbound-only or partial booking: create standalone booking
                        pinfo(f"creating booking with inbound at {best_in['time_str']} ...")
                        b_resp = client.create_provisional_booking(
                            access_token, offer_id, passenger_token
                        )
                        booking_id = b_resp.get("bookingId") or b_resp.get("id")
                elif booking_id:
                    pinfo("no valid 0-price offer for return leg, booking outbound only")
                else:
                    pinfo("no valid 0-price offer found for inbound, skipping")
                    return None
        elif dry_run:
            dry_run_result["inbound"] = {
                "departure": "—",
                "arrival": "—",
                "class": "—",
                "flexibility": "—",
                "has_offer": False,
            }
        elif not booking_id:
            pinfo("no departure found for inbound")
            return None

    if dry_run:
        return dry_run_result

    # 3. Checkout
    if not booking_id:
        logger.error("failed to create booking")
        return None

    try:
        email = cfg["auth"]["email"]
        phone = cfg["auth"].get("phone", "+46700000000")
        client.update_booking_customer(access_token, booking_id, email, phone)
        client.checkout_booking(access_token, booking_id)
        pinfo(f"booking {booking_id} checked out successfully")
    except Exception as e:
        logger.error(f"checkout failed for booking {booking_id}: {e}")
        pinfo(f"checkout failed: {e}")

    return None


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
        Dry-run result dict if in dry-run mode, None otherwise.

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
    has_outbound = check_existing_booking(
        existing_bookings, origin_id, dest_id, date_str
    )
    has_inbound = False
    if do_roundtrip:
        has_inbound = check_existing_booking(
            existing_bookings, dest_id, origin_id, date_str
        )

    if has_outbound and (not do_roundtrip or has_inbound):
        pinfo(f"{date_str}: already fully booked, skipping")
        return None

    req_outbound = not has_outbound
    req_inbound = do_roundtrip and not has_inbound

    service_types = params.get("service_types")
    if service_types and service_types == ["ALL"]:
        service_types = None

    # Search
    if req_outbound and req_inbound:
        # Roundtrip search
        logger.info("booking roundtrip (both legs missing)")
        search_resp = client.search_journey(
            access_token, origin_name, dest_name, date_str, date_str, tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        out_id = search_resp.get("departureSearchId")
        in_id = search_resp.get("returnDepartureSearchId")

        if out_id and in_id:
            return handle_booking_process(
                client, access_token, cfg, passenger_token,
                out_id, in_id, True, True, dry_run, date_str
            )
        logger.error("failed to get both outbound and inbound search IDs")
        return None

    if req_outbound:
        logger.info("booking outbound only")
        search_resp = client.search_journey(
            access_token, origin_name, dest_name, date_str, None, tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        out_id = search_resp.get("departureSearchId")
        if out_id:
            return handle_booking_process(
                client, access_token, cfg, passenger_token,
                out_id, None, True, False, dry_run, date_str
            )
        return None

    if req_inbound:
        # Inbound-only: swap origin/dest
        logger.info("booking inbound only (return leg)")
        search_resp = client.search_journey(
            access_token, dest_name, origin_name, date_str, None, tp_product_id,
            service_types,
        )
        passenger_token = search_resp.get("passengerListId") or tp_token_id
        in_id = search_resp.get("departureSearchId")
        if in_id:
            return handle_booking_process(
                client, access_token, cfg, passenger_token,
                None, in_id, False, True, dry_run, date_str
            )
        return None

    return None


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

    results = []
    curr = start_date

    while curr <= end_date:
        # Mid-run token refresh
        access_token = ensure_valid_token(client, token_manager, access_token)

        date_str = curr.strftime("%Y-%m-%d")

        try:
            result = process_booking_flow(
                client, access_token, cfg, curr,
                tp_product_id, tp_token_id, existing_bookings, dry_run
            )
            if dry_run and result:
                # Flatten outbound/inbound into separate rows
                if "outbound" in result:
                    out = result["outbound"]
                    results.append({
                        "date": date_str,
                        "direction": "Outbound",
                        "departure": out.get("departure", "—"),
                        "arrival": out.get("arrival", "—"),
                        "comfort_class": out.get("class", "—"),
                        "flexibility": out.get("flexibility") or "—",
                        "note": "" if out.get("has_offer") else "no 0-price offer",
                    })
                if "inbound" in result:
                    inb = result["inbound"]
                    results.append({
                        "date": date_str,
                        "direction": "Return",
                        "departure": inb.get("departure", "—"),
                        "arrival": inb.get("arrival", "—"),
                        "comfort_class": inb.get("class", "—"),
                        "flexibility": inb.get("flexibility") or "—",
                        "note": "" if inb.get("has_offer") else "no 0-price offer",
                    })
        except Exception as e:
            logger.error(f"error processing {date_str}: {e}")
            pinfo(f"error processing {date_str}: {e}")

        curr += timedelta(days=1)
        if curr <= end_date:
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

    Per SPEC §5.3: find bookings for the date, ask which direction(s) to cancel,
    confirm with y/N.
    """
    params = cfg["search_parameters"]
    origin_name = params["station_from"]
    dest_name = params["station_to"]
    origin_id = client.resolve_station(origin_name)
    dest_id = client.resolve_station(dest_name)

    # Fetch bookings for that date
    bookings = fetch_all_bookings(client, access_token, cancel_date, cancel_date)

    # Find matching bookings for the route
    outbound_booking = None
    inbound_booking = None

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
                dep_time = dt_str.split("T")[1][:5] if "T" in dt_str else ""
                arr_dt = seg.get("arrivalDateTime", "")
                arr_time = arr_dt.split("T")[1][:5] if "T" in arr_dt else ""

                if l_date != cancel_date:
                    continue

                if l_origin == origin_id and l_dest == dest_id:
                    outbound_booking = {
                        "item": item,
                        "dep_time": dep_time,
                        "arr_time": arr_time,
                    }
                elif l_origin == dest_id and l_dest == origin_id:
                    inbound_booking = {
                        "item": item,
                        "dep_time": dep_time,
                        "arr_time": arr_time,
                    }

    if not outbound_booking and not inbound_booking:
        pinfo(f"no bookings found for {cancel_date} on route {origin_name} → {dest_name}")
        return

    # Determine what to cancel
    to_cancel = []

    if outbound_booking and inbound_booking:
        pinfo(f"found bookings for {cancel_date} ({origin_name} → {dest_name}):")
        pinfo(f"  1. outbound  {outbound_booking['dep_time']} → {outbound_booking['arr_time']}")
        pinfo(f"  2. return    {inbound_booking['dep_time']} → {inbound_booking['arr_time']}")
        pinfo(f"  3. both")
        choice = input("cancel which? [1/2/3]: ").strip()

        if choice == "1":
            to_cancel = [("outbound", outbound_booking)]
        elif choice == "2":
            to_cancel = [("return", inbound_booking)]
        elif choice == "3":
            to_cancel = [("outbound", outbound_booking), ("return", inbound_booking)]
        else:
            pinfo("invalid selection, aborting")
            return
    elif outbound_booking:
        to_cancel = [("outbound", outbound_booking)]
    else:
        # Guaranteed non-None: early return on line 740 handles both-None case
        assert inbound_booking is not None
        to_cancel = [("return", inbound_booking)]

    # Confirm and cancel
    for direction, bk in to_cancel:
        src = origin_name if direction == "outbound" else dest_name
        dst = dest_name if direction == "outbound" else origin_name
        confirm = input(
            f"cancel {direction} {cancel_date} {bk['dep_time']} {src} → {dst}? [y/n]: "
        ).strip().lower()

        if confirm != "y":
            pinfo(f"skipping {direction} cancellation")
            continue

        item = bk["item"]
        assert isinstance(item, dict)
        booking_data = item.get("booking", {})
        b_id = (
            booking_data.get("bookingId")
            or booking_data.get("id")
            or item.get("bookingId")
        )

        try:
            client.cancel_provisional_booking(access_token, b_id)
            pinfo(f"{direction} booking {b_id} cancelled")
        except Exception as e:
            logger.error(f"failed to cancel {direction} booking {b_id}: {e}")
            pinfo(f"failed to cancel {direction}: {e}")


def handle_cancel_booking(
    client: SJClient,
    access_token: str,
    travel_pass: dict,
    booking_number: str,
) -> None:
    """
    Cancel a booking by its booking number.

    Fetches all bookings, finds the one matching the given booking number,
    displays its details, and asks for confirmation before cancelling.
    """
    # Fetch all bookings within the travel pass validity
    valid_end = travel_pass.get("endTravelValidityDateTime")
    now_date = datetime.now()
    b_start = now_date.strftime("%Y-%m-%d")

    if valid_end:
        vp_end = datetime.fromisoformat(valid_end.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        b_end = (vp_end + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        b_end = (now_date + timedelta(days=90)).strftime("%Y-%m-%d")

    pinfo(f"searching for booking {booking_number} ...")
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
    b_id = (
        booking.get("bookingId")
        or booking.get("id")
        or matched_item.get("bookingId")
    )

    # Check if there's a pending cancellation that needs to be resolved
    booking_status = booking.get("bookingStatus", "")
    possible_actions = (
        booking.get("possibleActions", [])
        + booking.get("bookingPossibleActions", [])
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
            direction = seg.get("direction", "OUTBOUND").capitalize()
            dep_dt = seg.get("departureDateTime", "")
            arr_dt = seg.get("arrivalDateTime", "")
            duration = seg.get("duration", "")
            prod_name = seg.get("productFamily", {}).get("name", "—")
            dep_station = seg.get("departureStation", {}).get("name", "—")
            arr_station = seg.get("arrivalStation", {}).get("name", "—")
            service_id = seg.get("serviceIdentifier", "")

            # Determine if segment is in the past
            in_past = "N"
            try:
                dep_parsed = datetime.fromisoformat(
                    dep_dt.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if dep_parsed < now:
                    in_past = "Y"
                    has_past_segment = True
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

            # Collect passenger IDs from the segment
            passenger_ids = [
                p.get("id", p.get("passengerId", ""))
                for p in seg.get("passengers", journey.get("passengers", []))
                if p.get("id") or p.get("passengerId")
            ]
            if not passenger_ids:
                passenger_ids = ["passenger_1"]

            display_rows.append({
                "date": date_str,
                "direction": direction,
                "departure": dep_time,
                "arrival": arr_time,
                "duration": duration,
                "comfort_class": prod_name,
                "route": f"{dep_station} → {arr_station}",
                "booking_number": booking_number,
                "past": in_past,
            })

            if in_past == "N" and service_id and passenger_ids:
                segments_to_cancel.append({
                    "serviceIdentifier": service_id,
                    "passengerIds": passenger_ids,
                    "_label": f"{direction}  {date_str}  {dep_time} → {arr_time}  "
                              f"{dep_station} → {arr_station}",
                    "_date": date_str,
                    "_direction": direction,
                    "_dep_time": dep_time,
                    "_arr_time": arr_time,
                    "_route": f"{dep_station} → {arr_station}",
                })

    print_bookings_table(display_rows, f"Booking {booking_number}")

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
            for part in choice.split(","):
                part = part.strip()
                if part in valid_nums:
                    indices.append(int(part) - 1)
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
        print(f"\n{format_table(confirm_headers, confirm_table_rows, title='Selected for cancellation')}")
        confirm = input("cancel selected journey(s)? [y/n]: ").strip().lower()
        if confirm != "y":
            pinfo("cancellation aborted")
            return

    # Clean up internal labels before sending to API
    payload = [
        {"serviceIdentifier": s["serviceIdentifier"], "passengerIds": s["passengerIds"]}
        for s in selected
    ]

    # Step 1: Provisional cancel
    success = client.cancel_booking_with_patch(access_token, b_id, payload)
    if not success:
        pinfo(f"failed to cancel booking {booking_number}")
        return

    # Step 2: Confirm the cancellation (checkout)
    confirmed = client.finalize_cancellation(access_token, b_id)
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

    # Determine date range from travel pass validity
    valid_end = travel_pass.get("endTravelValidityDateTime")

    now_date = datetime.now()
    b_start = now_date.strftime("%Y-%m-%d")

    if valid_end:
        vp_end = datetime.fromisoformat(valid_end.replace("Z", "+00:00")).replace(
            tzinfo=None
        )
        b_end_dt = vp_end + timedelta(days=1)
        b_end = b_end_dt.strftime("%Y-%m-%d")
    else:
        # Fallback: 3 months from now
        b_end = (now_date + timedelta(days=90)).strftime("%Y-%m-%d")

    pinfo("fetching bookings ...")
    all_bookings = fetch_all_bookings(client, access_token, b_start, b_end)

    # Transform raw API items into display rows
    display_rows = []
    for item in all_bookings:
        booking = item.get("booking", {})
        if booking.get("bookingStatus") == "CANCELLED":
            continue

        booking_number = booking.get("bookingNumber", "—")
        for journey in booking.get("journeys", []):
            for segment in journey.get("segments", []):
                direction = segment.get("direction", "OUTBOUND").capitalize()
                dep_dt = segment.get("departureDateTime", "")
                arr_dt = segment.get("arrivalDateTime", "")
                duration = segment.get("duration", "")
                prod_name = segment.get("productFamily", {}).get("name", "—")
                dep_station = segment.get("departureStation", {}).get("name", "—")
                arr_station = segment.get("arrivalStation", {}).get("name", "—")

                try:
                    date_str = dep_dt.split("T")[0]
                    dep_time = dep_dt.split("T")[1][:5]
                    arr_time = arr_dt.split("T")[1][:5]
                except (IndexError, AttributeError):
                    date_str = dep_dt
                    dep_time = dep_dt
                    arr_time = arr_dt

                display_rows.append({
                    "date": date_str,
                    "direction": direction,
                    "departure": dep_time,
                    "arrival": arr_time,
                    "duration": duration,
                    "comfort_class": prod_name,
                    "route": f"{dep_station} → {arr_station}",
                    "booking_number": booking_number,
                    "_sort_key": dep_dt,
                })

    # Sort by date, then departure time
    display_rows.sort(key=lambda r: r.get("_sort_key", ""))

    print_bookings_table(display_rows, pass_name)


def _get_arrival_time(departure: dict) -> str:
    """Extract arrival time string from a departure dict."""
    try:
        arr_dt = departure.get("arrivalDateTime", "")
        return arr_dt.split("T")[1][:5]
    except (IndexError, AttributeError):
        return "—"
