#!/usr/bin/env python3
"""Entry point for the SJ API client."""

import argparse
import contextlib
import logging
import os
import re
import sys
from datetime import datetime, timedelta

from sj_auth import ensure_authenticated, handle_logout
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
from sj_calendar import SWEDEN, parse_api_datetime, sweden_now, to_sweden
from sj_client import SJClient
from sj_config import CfgManager
from sj_errors import SJAPIError, SJAuthError, SJConfigError
from sj_logger import setup_logging
from sj_output import (
    DIM,
    RED,
    ask,
    blank,
    pinfo,
    print_fact,
    print_header_box,
    print_status_card,
    print_travelpasses,
    spinner,
    style,
)
from sj_token import TokenManager

setup_logging(os.getenv("LOG_LEVEL", ""))

logger = logging.getLogger(__name__)


def _parse_one_date(token: str, errors: list[str]):
    """Parse one YYYY-MM-DD token; echo the value in the error like sj_config does."""
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token):
            errors.append(f"'{token}' is not a real calendar date")
        else:
            errors.append(f"'{token}' must be a date formatted YYYY-MM-DD")
        return None


def parse_cancel_dates(value: str) -> tuple[list[str], list[str]]:
    """
    Parse the --cancel-date value into individual dates.

    Accepts a date, a comma-separated list, and/or inclusive start..end
    ranges — mixed freely.

    Validate-first contract: every token is checked and ALL problems are
    collected before any cancellation work may start.

    Returns:
        (sorted unique ISO dates, errors). The dates are only meaningful
        when errors is empty.

    """
    dates: set = set()
    errors: list[str] = []
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            errors.append("empty entry in the date list")
            continue
        if ".." in token:
            parts = token.split("..")
            if len(parts) != 2:
                errors.append(f"'{token}' must be a single range start..end")
                continue
            start = _parse_one_date(parts[0], errors)
            end = _parse_one_date(parts[1], errors)
            if not (start and end):
                continue
            if start > end:
                errors.append(f"range '{token}' must run forwards (start before end)")
            elif (end - start).days > 366:
                errors.append(f"range '{token}' spans more than a year")
            else:
                curr = start
                while curr <= end:
                    dates.add(curr)
                    curr += timedelta(days=1)
        else:
            d = _parse_one_date(token, errors)
            if d:
                dates.add(d)
    if errors:
        return [], errors
    return [d.isoformat() for d in sorted(dates)], []


def parse_booking_numbers(value: str) -> tuple[list[str], list[str]]:
    """
    Parse the --cancel-booking value into booking numbers.

    Comma-separated, case-insensitive, deduplicated with order kept.
    Validate-first contract: ALL problems are collected before any
    cancellation work may start; date-like values get a --cancel-date hint.

    Returns:
        (booking numbers upper-cased, errors). Only meaningful when errors
        is empty.

    """
    numbers: list[str] = []
    errors: list[str] = []
    date_like = False
    for raw in value.split(","):
        token = raw.strip()
        if not token:
            errors.append("empty entry in the booking number list")
            continue
        if not token.isalnum():
            errors.append(
                f"'{token}' is not a booking number (letters and digits only, e.g. 3HT2NEIL)"
            )
            if ".." in token or re.search(r"\d{4}-\d{2}-\d{2}", token):
                date_like = True
            continue
        if token.upper() not in numbers:
            numbers.append(token.upper())
    if date_like:
        errors.append("these look like dates — did you mean --cancel-date?")
    if errors:
        return [], errors
    return numbers, []


class SJArgumentParser(argparse.ArgumentParser):
    """
    ArgumentParser in the app's voice.

    Help always ends with a blank line. A usage error shows the full help
    (not just the usage line) and closes with the red ● status line naming
    the problem, then exits 1.
    """

    def print_help(self, file=None):
        super().print_help(file)
        print(file=file or sys.stdout)

    def error(self, message):
        self.print_help(sys.stderr)
        print(f" {style('●', RED)} {style(message, DIM)}", file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. A mode flag is required; bare invocation prints help."""
    parser = SJArgumentParser(
        description="SJ API client for automated train ticket booking."
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview modifier for --book, --cancel-date and --cancel-booking: "
            "show what would happen without doing any of it."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--book",
        action="store_true",
        help="Book tickets.",
    )
    group.add_argument(
        "--cancel-date",
        metavar="DATES",
        help=(
            "Cancel bookings for one or more dates: a YYYY-MM-DD date, a "
            "comma-separated list, and/or inclusive START..END ranges "
            "(e.g. 2026-09-16,2026-09-21..2026-09-25)."
        ),
    )
    group.add_argument(
        "--cancel-booking",
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
        "--login",
        action="store_true",
        help="Authenticate and cache the token, then exit.",
    )
    group.add_argument(
        "--logout",
        action="store_true",
        help="Log out: end the sj.se session and delete the cached token and cookies.",
    )
    group.add_argument(
        "--login-status",
        action="store_true",
        help="Exit 0 if logged in (valid or refreshable cached token), 1 if not (for scripting).",
    )

    args = parser.parse_args(argv)

    # No implicit default mode: a bare invocation (or a bare --dry-run, which
    # is only a modifier) shows the help and fails.
    if not any(v for k, v in vars(args).items() if k != "dry_run"):
        parser.error("no operation given, choose one of the flags above")

    if args.dry_run and not (args.book or args.cancel_date or args.cancel_booking):
        parser.error("--dry-run only applies to --book, --cancel-date and --cancel-booking")

    # Validate-first: every cancel date is parsed and checked here, before
    # any auth or API work can start.
    if args.cancel_date:
        args.cancel_dates, errors = parse_cancel_dates(args.cancel_date)
        if errors:
            print_status_card(False, "invalid --cancel-date", lines=errors)
            sys.exit(1)
    if args.cancel_booking:
        args.cancel_booking_numbers, errors = parse_booking_numbers(args.cancel_booking)
        if errors:
            print_status_card(False, "invalid --cancel-booking", lines=errors)
            sys.exit(1)

    return args


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

    blank()
    while True:
        choice = ask(f"select pass [1-{len(travel_passes)}]: ").strip()
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

    print_travelpasses(travel_passes, receipt_info)


def _format_epoch(ts) -> str | None:
    """Format a Unix timestamp as Swedish wall-clock ('fri 22 aug 18:04')."""
    if not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts, tz=SWEDEN).strftime("%a %d %b %H:%M").lower()


def _relative_epoch(ts: float) -> str:
    """Coarse human distance to a future Unix timestamp ('in 23h')."""
    secs = int(ts - sweden_now().timestamp())
    if secs < 3600:
        return f"in {max(1, secs // 60)}m"
    if secs < 172800:
        return f"in {secs // 3600}h"
    return f"in {secs // 86400}d"


def _cached_email(tm: TokenManager) -> str | None:
    """Email from the cached token's profile_info, ignoring a corrupt cache."""
    with contextlib.suppress(SJAuthError):
        tm.load()
    return tm.profile_email()


def _print_auth_header(operation: str, email: str | None) -> None:
    """Header box for the session-scoped auth modes: operation + account."""
    rows = [("operation", operation)]
    if email:
        rows.append(("account", email))
    print_header_box(rows)
    blank()


def print_login_failed(e: SJAuthError) -> None:
    """Render the --login failure card, with code/message from an API-error cause."""
    blank()
    cause = e.__cause__
    if isinstance(cause, SJAPIError) and cause.message:
        facts = [("reason", cause.message)]
        if cause.code:
            facts.append(("code", cause.code))
    else:
        facts = [("reason", str(e))]
    print_status_card(False, "login failed", facts)


def handle_login_status(tm: TokenManager | None = None, verdict: str = "logged in") -> None:
    """
    Report whether the tool is logged in, and exit accordingly.

    "Logged in" means the next run needs no interaction, judged from the
    cache alone (no network): a valid access token, or a still-usable
    refresh token — the same ladder ensure_authenticated walks. Cached SSO
    cookies are not considered; they cannot be verified offline. Renders a
    status card: who is logged in (email from profile_info), the session
    horizon (extended automatically by every run) with a relative time, and
    the access-token expiry — kept even when the token has lapsed.
    """
    tm = tm or TokenManager()
    try:
        token_data = tm.load()
    except SJAuthError as e:
        pinfo(str(e))
        print()
        sys.exit(1)

    if not token_data:
        print_status_card(
            False, "not logged in", [("session", "no cached login found · log in with --login")]
        )
        sys.exit(1)

    valid = tm.is_valid()
    refreshable = tm.has_refresh_token()

    if valid or refreshable:
        facts = []
        at_exp = token_data.get("expires_on")
        rt_exp = token_data.get("refresh_token_expires_on")
        horizon = rt_exp if refreshable and isinstance(rt_exp, (int, float)) else None
        if horizon is None and valid:
            horizon = at_exp if isinstance(at_exp, (int, float)) else None
        if horizon is not None:
            session = f"valid until {_format_epoch(horizon)} (expires {_relative_epoch(horizon)})"
        else:
            session = "renews on next run"
        facts.append(("session", session))

        at_txt = _format_epoch(at_exp)
        if valid:
            if at_txt:
                facts.append(("token", f"valid until {at_txt}"))
        else:
            expired = f"expired {at_txt}" if at_txt else "expired"
            facts.append(("token", f"{expired} · renews automatically on next run"))

        print_status_card(True, verdict, facts)
        sys.exit(0)

    print_status_card(False, "not logged in", [("session", "expired · log in with --login")])
    sys.exit(1)


def main():
    """Main entry point."""
    print()
    args = parse_args()

    # Handle --login-status early (no config or client needed)
    if args.login_status:
        tm = TokenManager()
        _print_auth_header("checking login status", _cached_email(tm))
        handle_login_status(tm)

    client = SJClient()
    try:
        _run(args, client)
    finally:
        client.close()


def _run(args: argparse.Namespace, client: SJClient) -> None:
    """Everything after argument parsing; split out so main() can close the client."""
    # Handle --logout early: no config needed, only the client (for the
    # server-side end-session call) and the caches.
    if args.logout:
        tm = TokenManager()
        _print_auth_header("logging out", _cached_email(tm))
        try:
            handle_logout(client, tm)
        except SJAuthError as e:
            blank()
            print_status_card(False, "logout failed", lines=[str(e)])
            sys.exit(1)
        sys.exit(0)

    # 1. Load and validate config; offer first-run setup when it is missing
    try:
        cm = CfgManager()
        if not cm.path.exists():
            if sys.stdin.isatty() and cm.create_interactive():
                sys.exit(0)
            print_status_card(False, "no configuration", lines=[
                f"expected a config file at {cm.path}",
                "run any command in a terminal to create it, or copy config.example.toml",
            ])
            sys.exit(1)
        cfg = cm.load()
        cm.verify_cfg(cfg)
    except SJConfigError as e:
        print_status_card(False, "invalid configuration", lines=e.errors or [str(e)])
        sys.exit(1)

    email = cfg["auth"]["email"]
    password = cfg["auth"]["password"]

    # --login opens with its header box before the auth trail
    if args.login:
        _print_auth_header("logging in", email)

    # 2. Authenticate
    try:
        tm = TokenManager()
        access_token, auth_method = ensure_authenticated(client, tm, email, password)
    except SJAuthError as e:
        if args.login:
            print_login_failed(e)
        else:
            pinfo(str(e))
            print()
        sys.exit(1)

    # Handle --login: authenticate, then show the same card as --login-status.
    # A login that needed no full login is "already logged in".
    if args.login:
        handle_login_status(
            tm, verdict="logged in" if auth_method == "full" else "already logged in"
        )

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

        # Header-box identity rows: account is always the second row app-wide;
        # pass and holder keep their real casing
        pass_rows = [
            ("account", email),
            ("travelpass", active_pass.get("name", "Unknown")),
            ("holder", user_name),
        ]

    except SystemExit:
        raise
    except Exception as e:
        pinfo(f"initialization failed: {e}")
        print()
        sys.exit(1)

    # 4. Mode dispatch. Every pass-scoped mode opens with the header box
    # (operation / travelpass / holder); auth modes lead with their cards.
    try:
        if args.list_travelpasses:
            print_header_box([
                ("operation", "listing travel passes"), ("account", email), ("holder", user_name),
            ])
            blank()
            handle_list_travelpasses(client, travel_passes)

        elif args.list_bookings:
            print_header_box([("operation", "listing bookings"), *pass_rows])
            blank()
            handle_list_bookings(client, access_token, active_pass)

        elif args.cancel_date:
            operation = ("dry run · " if args.dry_run else "") + "cancelling bookings"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            validate_dates_against_pass(cfg, active_pass)
            for i, cancel_date in enumerate(args.cancel_dates):
                if i:
                    blank()
                handle_cancel_mode(client, access_token, cfg, cancel_date, dry_run=args.dry_run)

        elif args.cancel_booking:
            operation = ("dry run · " if args.dry_run else "") + "cancelling bookings"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            for i, bn in enumerate(args.cancel_booking_numbers):
                if i:
                    blank()
                handle_cancel_booking(client, access_token, active_pass, bn,
                                      dry_run=args.dry_run)

        else:
            # --book, with --dry-run as the preview modifier.
            validate_dates_against_pass(cfg, active_pass)

            operation = ("dry run · " if args.dry_run else "") + "booking tickets"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            for label, value in describe_run(cfg["search_parameters"]):
                print_fact(label, value)
            blank()

            # Fetch existing bookings for the duplicate check (quietly: a
            # routine step, not part of the day-by-day trail)
            b_start, b_end = booking_date_range(active_pass, start_offset_days=1)
            with spinner("fetching existing bookings", trail=False):
                bookings_list = fetch_all_bookings(client, access_token, b_start, b_end)

            # Cleanup stale provisionals (never in a dry run: no mutations)
            if not args.dry_run:
                bookings_list = cleanup_stale_provisionals(
                    client, access_token, bookings_list
                )

            # Process date range: one card per day, summary footer at the end
            process_date_range(
                client, access_token, tm, cfg,
                tp_product_id, tp_token_id, bookings_list,
                dry_run=args.dry_run,
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
