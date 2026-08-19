#!/usr/bin/env python3
"""Entry point for the SJ API client."""

import argparse
import logging
import os
import sys
from datetime import datetime

from sj_auth import ensure_authenticated
from sj_booking import (
    booking_date_range,
    cleanup_stale_provisionals,
    describe_run,
    fetch_all_bookings,
    handle_cancel_booking,
    handle_cancel_mode,
    handle_list_bookings,
    process_date_range,
)
from sj_calendar import parse_api_datetime, sweden_now, to_sweden
from sj_client import SJClient
from sj_config import CfgManager
from sj_errors import SJAuthError, SJConfigError
from sj_logger import setup_logging
from sj_output import pdim, pinfo, print_title, print_travelpasses_table, spinner
from sj_token import TokenManager

setup_logging(os.getenv("LOG_LEVEL", ""))

logger = logging.getLogger(__name__)


class SJArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that exits with code 1 on error."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message}", file=sys.stderr)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = SJArgumentParser(
        description="SJ API client for automated train ticket booking."
    )

    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--book",
        action="store_true",
        help="Book tickets for real (default is dry-run).",
    )
    group.add_argument(
        "--cancel-date",
        metavar="YYYY-MM-DD",
        help="Cancel bookings for a specific date.",
    )
    group.add_argument(
        "--cancel-bookings",
        metavar="BOOKING_NUMBER",
        help="Cancel booking(s) by number, comma-separated (e.g. 3HT2NEIL or 3HT2NEIL,ABCD1234).",
    )
    group.add_argument(
        "--list-bookings",
        action="store_true",
        help="Display all active bookings in a table.",
    )
    group.add_argument(
        "--list-travelpasses",
        action="store_true",
        help="Display travel passes with validity and receipt details.",
    )
    group.add_argument(
        "--login-only",
        action="store_true",
        help="Perform login only (authenticate and cache token), then exit.",
    )
    group.add_argument(
        "--test-if-already-logged-in",
        action="store_true",
        help="Test if a valid cached token exists, exit 0 if yes, 1 if no.",
    )

    return parser.parse_args()


def _is_expired(travel_pass: dict) -> bool:
    """True if the pass's validity end lies in the past (unknown end = not expired)."""
    end = travel_pass.get("endTravelValidityDateTime")
    if not end:
        return False
    try:
        return parse_api_datetime(end) < sweden_now()
    except (ValueError, TypeError):
        return False


def resolve_travel_pass(travel_passes: list) -> dict:
    """
    Select the travel pass to use.

    Expired passes are dropped (SPEC §9.1). Auto-picks if only one pass
    remains. Prompts for selection if multiple.

    Returns:
        The selected travel pass dict.

    Raises:
        SystemExit: If no valid passes found.

    """
    valid = [tp for tp in travel_passes if not _is_expired(tp)]
    if len(valid) < len(travel_passes):
        logger.info(f"ignoring {len(travel_passes) - len(valid)} expired travel pass(es)")

    if not valid:
        pinfo("no valid travel pass found" if travel_passes else "no travel pass found")
        sys.exit(1)
    travel_passes = valid

    if len(travel_passes) == 1:
        return travel_passes[0]

    # Multiple passes: prompt for selection
    pinfo("available travel passes:")
    for i, tp in enumerate(travel_passes, 1):
        name = tp.get("name", "Unknown")
        valid_start = tp.get("startTravelValidityDateTime", "")[:10]
        valid_end = tp.get("endTravelValidityDateTime", "")[:10]
        pinfo(f"  {i}. {name} ({valid_start} → {valid_end})")

    while True:
        choice = input(f"select pass [1-{len(travel_passes)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(travel_passes):
                return travel_passes[idx]
        except ValueError:
            pass
        pinfo("invalid selection, try again")


def validate_dates_against_pass(cfg: dict, travel_pass: dict) -> None:
    """
    Validate that search dates fall within travel pass validity.

    Raises:
        SystemExit: If dates are outside validity.

    """
    valid_start = travel_pass.get("startTravelValidityDateTime")
    valid_end = travel_pass.get("endTravelValidityDateTime")

    if not valid_start or not valid_end:
        return

    # Compare Swedish wall-clock times: config dates are Swedish dates.
    vp_start = to_sweden(valid_start).replace(tzinfo=None)
    vp_end = to_sweden(valid_end).replace(tzinfo=None)

    params = cfg.get("search_parameters", {})
    s_start = datetime.strptime(params["date_start"], "%Y-%m-%d")
    s_end = datetime.strptime(params["date_end"], "%Y-%m-%d")

    if s_start < vp_start or s_end > vp_end:
        pinfo(
            f"search dates ({params['date_start']} – {params['date_end']}) "
            f"are outside travel pass validity "
            f"({vp_start.strftime('%Y-%m-%d')} – {vp_end.strftime('%Y-%m-%d')})"
            f"\n"
        )
        sys.exit(1)


def handle_list_travelpasses(client: SJClient, travel_passes: list) -> None:
    """Display travel passes with validity and receipt details."""
    receipt_info: dict[str, dict] = {}

    for tp in travel_passes:
        booking_id = tp.get("travelPassCreationBookingId", "")
        if not booking_id:
            continue

        receipts = client.get_receipt_search(booking_id)
        if not receipts:
            continue

        # Use first receipt from search results (contains amount/currency directly)
        receipt = receipts[0] if isinstance(receipts, list) else receipts
        if receipt:
            receipt_info[booking_id] = receipt

    print_travelpasses_table(travel_passes, receipt_info)


def handle_test_logged_in() -> None:
    """Test if a valid cached token exists and exit accordingly."""
    tm = TokenManager()
    try:
        token_data = tm.load()
    except SJAuthError as e:
        pinfo(str(e))
        print()
        sys.exit(1)

    if not token_data:
        pinfo("not logged in: no cached token found")
        print()
        sys.exit(1)

    if not tm.is_valid():
        pinfo("not logged in: cached token is expired")
        print()
        sys.exit(1)

    pinfo("logged in: valid cached token exists")
    print()
    sys.exit(0)


def main():
    """Main entry point."""
    print()
    args = parse_args()

    # Handle --test-if-already-logged-in early (no config or client needed)
    if args.test_if_already_logged_in:
        handle_test_logged_in()

    client = SJClient()
    try:
        _run(args, client)
    finally:
        client.close()


def _run(args: argparse.Namespace, client: SJClient) -> None:
    """Everything after argument parsing; split out so main() can close the client."""
    # 1. Load and validate config
    try:
        cm = CfgManager()
        cfg = cm.load()
        cm.verify_cfg(cfg)
    except SJConfigError as e:
        pinfo(str(e))
        print()
        sys.exit(1)

    email = cfg["auth"]["email"]
    password = cfg["auth"]["password"]

    # 2. Authenticate
    try:
        tm = TokenManager()
        access_token = ensure_authenticated(client, tm, email, password)
    except SJAuthError as e:
        pinfo(str(e))
        print()
        sys.exit(1)

    # Handle --login-only: authenticate and exit
    if args.login_only:
        pinfo("login successful, token cached")
        print()
        sys.exit(0)

    # 3. Fetch membership and travel pass
    try:
        membership = client.get_membership(access_token)
        user_name = f"{membership.get('firstName')} {membership.get('lastName')}"

        tp_resp = client.get_travel_passes(access_token)
        travel_passes = (
            tp_resp if isinstance(tp_resp, list) else tp_resp.get("travelPasses", [])
        )

        active_pass = resolve_travel_pass(travel_passes)
        tp_product_id: str = active_pass.get("travelPassId", "")

        # Resolve passenger token
        tp_token_id: str = (
            active_pass.get("passengerToken")
            or active_pass.get("travelPassCreationBookingId")
            or tp_product_id
        )

        # Title context, lowercase by convention: "sj årskort silver · john doe"
        who = f"{active_pass.get('name', 'Unknown')} \u00b7 {user_name}".lower()

    except SystemExit:
        raise
    except Exception as e:
        pinfo(f"initialization failed: {e}")
        print()
        sys.exit(1)

    # 4. Mode dispatch. Every mode opens with a bold title line; book/dry-run add
    # two dim lines describing the run (route, dates, times, class, filter).
    try:
        if args.list_travelpasses:
            print_title(f"\U0001f3ab travel passes \u00b7 {user_name.lower()}")
            handle_list_travelpasses(client, travel_passes)

        elif args.list_bookings:
            print_title(f"\U0001f3ab {who}")
            handle_list_bookings(client, access_token, active_pass)

        elif args.cancel_date:
            print_title(f"\U0001f3ab {who}")
            validate_dates_against_pass(cfg, active_pass)
            handle_cancel_mode(client, access_token, cfg, args.cancel_date)

        elif args.cancel_bookings:
            print_title(f"\U0001f3ab {who}")
            booking_numbers = [
                b.strip().upper() for b in args.cancel_bookings.split(",") if b.strip()
            ]
            for bn in booking_numbers:
                handle_cancel_booking(client, access_token, active_pass, bn)

        else:
            # Default: dry-run. With --book: book for real.
            validate_dates_against_pass(cfg, active_pass)

            mode = "\U0001f686 booking" if args.book else "\U0001f50d dry run"
            print_title(f"{mode} \u00b7 {who}")
            for line in describe_run(cfg["search_parameters"]):
                pdim(line)

            # Fetch existing bookings for the duplicate check (quietly: a
            # routine step, not part of the day-by-day trail)
            b_start, b_end = booking_date_range(active_pass, start_offset_days=1)
            with spinner("fetching existing bookings", trail=False):
                bookings_list = fetch_all_bookings(client, access_token, b_start, b_end)

            # Cleanup stale provisionals (only when booking)
            if args.book:
                bookings_list = cleanup_stale_provisionals(
                    client, access_token, bookings_list
                )

            # Process date range: one card per day, summary footer at the end
            process_date_range(
                client, access_token, tm, cfg,
                tp_product_id, tp_token_id, bookings_list,
                dry_run=not args.book,
            )

    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"error: {e}")
        pinfo(f"error: {e}")
        print()
        sys.exit(1)

    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pinfo("\ninterrupted by user")
        print()
        sys.exit(130)
