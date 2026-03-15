#!/usr/bin/env python3
"""Entry point for the SJ API client."""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

from sj_auth import ensure_authenticated
from sj_booking import (
    cleanup_stale_provisionals,
    fetch_all_bookings,
    handle_cancel_booking,
    handle_cancel_mode,
    handle_list_bookings,
    process_date_range,
)
from sj_client import SJClient
from sj_config import CfgManager
from sj_errors import SJAuthError, SJConfigError
from sj_logger import setup_logging
from sj_output import pinfo, print_dry_run_table, print_travelpasses_table
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
        "--dry-run",
        action="store_true",
        help="Search and display what would be booked, without booking.",
    )
    group.add_argument(
        "--cancel-date",
        metavar="YYYY-MM-DD",
        help="Cancel bookings for a specific date.",
    )
    group.add_argument(
        "--cancel-booking",
        metavar="BOOKING_NUMBER",
        help="Cancel a booking by its booking number (e.g. 3HT2NEIL).",
    )
    group.add_argument(
        "--list-current-bookings",
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


def resolve_travel_pass(travel_passes: list) -> dict:
    """
    Select the travel pass to use.

    Auto-picks if only one valid pass exists. Prompts for selection if multiple.

    Returns:
        The selected travel pass dict.

    Raises:
        SystemExit: If no valid passes found.

    """
    if not travel_passes:
        pinfo("no travel pass found")
        sys.exit(1)

    if len(travel_passes) == 1:
        return travel_passes[0]

    # Multiple passes: prompt for selection
    print("\nAvailable travel passes:")
    for i, tp in enumerate(travel_passes, 1):
        name = tp.get("name", "Unknown")
        valid_start = tp.get("startTravelValidityDateTime", "")[:10]
        valid_end = tp.get("endTravelValidityDateTime", "")[:10]
        print(f"  {i}. {name} ({valid_start} → {valid_end})")

    while True:
        choice = input(f"\nSelect pass [1-{len(travel_passes)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(travel_passes):
                return travel_passes[idx]
        except ValueError:
            pass
        print("Invalid selection, try again.")


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

    vp_start = datetime.fromisoformat(
        valid_start.replace("Z", "+00:00")
    ).replace(tzinfo=None)
    vp_end = datetime.fromisoformat(
        valid_end.replace("Z", "+00:00")
    ).replace(tzinfo=None)

    params = cfg.get("search_parameters", {})
    s_start = datetime.strptime(params["date_start"], "%Y-%m-%d")
    s_end = datetime.strptime(params["date_end"], "%Y-%m-%d")

    if s_start < vp_start or s_end > vp_end:
        pinfo(
            f"search dates ({params['date_start']} – {params['date_end']}) "
            f"are outside travel pass validity "
            f"({vp_start.strftime('%Y-%m-%d')} – {vp_end.strftime('%Y-%m-%d')})"
        )
        sys.exit(1)


def handle_list_travelpasses(
    client: SJClient, access_token: str, travel_passes: list
) -> None:
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
    token_data = tm.load()

    if not token_data:
        pinfo("not logged in: no cached token found")
        sys.exit(1)

    if not tm.is_valid():
        pinfo("not logged in: cached token is expired")
        sys.exit(1)

    pinfo("logged in: valid cached token exists")
    sys.exit(0)


def main():
    """Main entry point."""
    args = parse_args()

    # Handle --test-if-already-logged-in early (no config or client needed)
    if args.test_if_already_logged_in:
        handle_test_logged_in()

    client = SJClient()

    # 1. Load and validate config
    try:
        cm = CfgManager()
        cfg = cm.load()
        cm.verify_cfg(cfg)
    except SJConfigError as e:
        pinfo(str(e))
        sys.exit(1)

    email = cfg["auth"]["email"]
    password = cfg["auth"]["password"]

    # 2. Authenticate
    try:
        tm = TokenManager()
        access_token = ensure_authenticated(client, tm, email, password)
    except KeyboardInterrupt:
        print()
        pinfo("interrupted by user")
        sys.exit(130)
    except SJAuthError as e:
        pinfo(str(e))
        sys.exit(1)

    # Handle --login-only: authenticate and exit
    if args.login_only:
        pinfo("login successful, token cached")
        sys.exit(0)

    # 3. Fetch membership and travel pass
    try:
        membership = client.get_membership(access_token)
        pinfo(
            f"logged in as {membership.get('firstName')} {membership.get('lastName')}"
        )

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

        pinfo(f"travel pass: {active_pass.get('name', 'Unknown')}")

    except SystemExit:
        raise
    except Exception as e:
        pinfo(f"initialization failed: {e}")
        sys.exit(1)

    # 4. Mode dispatch
    try:
        if args.list_travelpasses:
            handle_list_travelpasses(client, access_token, travel_passes)

        elif args.list_current_bookings:
            handle_list_bookings(client, access_token, active_pass)

        elif args.cancel_date:
            validate_dates_against_pass(cfg, active_pass)
            handle_cancel_mode(client, access_token, cfg, args.cancel_date)

        elif args.cancel_booking:
            handle_cancel_booking(client, access_token, active_pass, args.cancel_booking)

        else:
            # Default booking mode or dry-run
            validate_dates_against_pass(cfg, active_pass)

            # Fetch existing bookings for duplicate check
            params = cfg["search_parameters"]
            now_date = datetime.now()
            start_dt = now_date + timedelta(days=1)
            b_start = start_dt.strftime("%Y-%m-%d")

            valid_end = active_pass.get("endTravelValidityDateTime")
            if valid_end:
                vp_end = datetime.fromisoformat(
                    valid_end.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                b_end = (vp_end + timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                b_end = params["date_end"]

            pinfo("fetching existing bookings ...")
            bookings_list = fetch_all_bookings(client, access_token, b_start, b_end)

            # Cleanup stale provisionals (unless dry-run)
            if not args.dry_run:
                bookings_list = cleanup_stale_provisionals(
                    client, access_token, bookings_list
                )

            pinfo(f"found {len(bookings_list)} active bookings")

            # Process date range
            results = process_date_range(
                client, access_token, tm, cfg,
                tp_product_id, tp_token_id, bookings_list,
                dry_run=args.dry_run,
            )

            if args.dry_run and results:
                print_dry_run_table(results)

            pinfo("done")

    except KeyboardInterrupt:
        print()
        pinfo("interrupted by user")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:
        logger.error(f"error: {e}")
        pinfo(f"error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
