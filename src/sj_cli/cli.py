"""Command-line interface: argument parsing and top-level orchestration."""

import argparse
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, NoReturn, override

from sj_cli.auth import ensure_authenticated, handle_logout
from sj_cli.booking import (
    booking_date_range,
    cleanup_stale_provisionals,
    describe_run,
    fetch_all_bookings,
    handle_cancel_booking,
    handle_cancel_mode,
    handle_change_seat,
    handle_list_bookings,
    process_date_range,
)
from sj_cli.client import SJClient
from sj_cli.config import CfgManager
from sj_cli.dates import (
    SWEDEN,
    booking_dates,
    parse_api_datetime,
    parse_date_selection,
    skip_reason,
    sweden_now,
    to_sweden,
)
from sj_cli.errors import SJAPIError, SJAuthError, SJConfigError, error_text
from sj_cli.logger import setup_logging
from sj_cli.output import (
    DIM,
    RED,
    ask,
    blank,
    pinfo,
    print_fact,
    print_header_box,
    print_status_card,
    print_travelpasses,
    pstatus,
    pwarn,
    spinner,
    style,
)
from sj_cli.tokens import TokenManager

if TYPE_CHECKING:
    from _typeshed import SupportsWrite

logger = logging.getLogger(__name__)


def parse_cancel_dates(value: str) -> tuple[list[str], list[str]]:
    """
    Parse the --cancel-date value into individual dates.

    The shared selection grammar (dates.parse_date_selection): dates, ISO
    weeks (W43, 2027-W02), comma-separated lists and inclusive start..end
    ranges — mixed freely.

    Validate-first contract: every term is checked and ALL problems are
    collected before any cancellation work may start.

    Returns:
        (sorted unique ISO dates, errors). The dates are only meaningful
        when errors is empty.

    """
    dates, errors = parse_date_selection(value)
    return [d.isoformat() for d in dates], errors


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
        looks_like_a_date = ".." in token or bool(
            re.search(r"\d{4}-\d{2}-\d{2}|^\d{4}-W\d{1,2}$|^W\d{1,2}$", token, re.IGNORECASE)
        )
        if not token.isalnum() or looks_like_a_date:
            if looks_like_a_date:
                errors.append(f"'{token}' looks like a date or week, not a booking number")
                date_like = True
            else:
                errors.append(
                    f"'{token}' is not a booking number (letters and digits only, e.g. 3HT2NEIL)"
                )
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

    @override
    def print_help(self, file: "SupportsWrite[str] | None" = None) -> None:
        super().print_help(file)
        print(file=file or sys.stdout)

    @override
    def error(self, message: str) -> NoReturn:
        self.print_help(sys.stderr)
        print(f" {style('●', RED)} {style(message, DIM)}", file=sys.stderr)
        print(file=sys.stderr)
        sys.exit(1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments. A mode flag is required; bare invocation prints help."""
    # allow_abbrev=False: argparse would otherwise take any unique prefix as
    # an alias, so that `--logo` (a typo of --login) logs the user out.
    parser = SJArgumentParser(
        description="SJ API client for automated train ticket booking.", allow_abbrev=False
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Preview modifier for --book, --cancel-date, --cancel-booking, "
            "--change-seat-date and --change-seat-booking: show what would "
            "happen without doing any of it."
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
            "Cancel bookings for one or more dates: a YYYY-MM-DD date, an ISO week "
            "(W43; 2027-W02 for another year), a comma-separated list, and/or "
            "inclusive START..END ranges (e.g. 2026-09-16,2026-09-21..2026-09-25 or W43,W45..46)."
        ),
    )
    group.add_argument(
        "--cancel-booking",
        metavar="BOOKING_NUMBER",
        help="Cancel booking(s) by number, comma-separated (e.g. 3HT2NEIL or 3HT2NEIL,ABCD1234).",
    )
    group.add_argument(
        "--change-seat-date",
        metavar="DATES",
        help=(
            "Change seats for one or more dates on the configured route, using "
            "seat_preference: same date grammar as --cancel-date."
        ),
    )
    group.add_argument(
        "--change-seat-booking",
        metavar="BOOKING_NUMBER",
        help="Change seats on booking(s) by number, comma-separated, using seat_preference.",
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
    # is only a modifier) shows the help and fails. An empty value (`--cancel-date ""`)
    # is an operation with an invalid argument, reported as such below.
    given = [k for k, v in vars(args).items() if k != "dry_run" and v not in (None, False)]
    if not given:
        parser.error("no operation given, choose one of the flags above")

    if args.dry_run and not (
        args.book
        or args.cancel_date is not None
        or args.cancel_booking is not None
        or args.change_seat_date is not None
        or args.change_seat_booking is not None
    ):
        parser.error(
            "--dry-run only applies to --book, --cancel-date, --cancel-booking, "
            "--change-seat-date and --change-seat-booking"
        )

    # Validate-first: every cancel date is parsed and checked here, before
    # any auth or API work can start.
    if args.cancel_date is not None:
        args.cancel_dates, errors = parse_cancel_dates(args.cancel_date)
        if errors:
            print_status_card(False, "invalid --cancel-date", lines=errors)
            sys.exit(1)
    if args.cancel_booking is not None:
        args.cancel_booking_numbers, errors = parse_booking_numbers(args.cancel_booking)
        if errors:
            print_status_card(False, "invalid --cancel-booking", lines=errors)
            sys.exit(1)
    if args.change_seat_date is not None:
        args.change_seat_dates, errors = parse_cancel_dates(args.change_seat_date)
        if errors:
            print_status_card(False, "invalid --change-seat-date", lines=errors)
            sys.exit(1)
    if args.change_seat_booking is not None:
        args.change_seat_booking_numbers, errors = parse_booking_numbers(args.change_seat_booking)
        if errors:
            print_status_card(False, "invalid --change-seat-booking", lines=errors)
            sys.exit(1)

    return args


def _pass_validity(travel_pass: dict) -> tuple[date | None, date | None]:
    """
    (first valid day, last valid day) of a pass as Swedish calendar dates.

    The API's validity instants are midnight UTC and the end is exclusive
    (the day after the last valid day). None for an unknown or unparsable
    bound.
    """

    def day(key: str, exclusive: bool) -> date | None:
        raw = travel_pass.get(key)
        if not raw:
            return None
        try:
            local = to_sweden(raw)
        except (ValueError, TypeError):
            return None
        return (local - timedelta(days=1)).date() if exclusive else local.date()

    return day("startTravelValidityDateTime", False), day("endTravelValidityDateTime", True)


def _is_expired(travel_pass: dict) -> bool:
    """True if the pass's validity end lies in the past (unknown end = not expired)."""
    end = travel_pass.get("endTravelValidityDateTime")
    if not end:
        return False
    try:
        return parse_api_datetime(end) < sweden_now()
    except (ValueError, TypeError):
        return False


def _covers(travel_pass: dict, window: tuple[date, date]) -> bool:
    """True if the pass's known validity contains the whole window."""
    first, last = _pass_validity(travel_pass)
    if first is None or last is None:
        return False
    return first <= window[0] and window[1] <= last


def resolve_travel_pass(
    travel_passes: list,
    for_dates: tuple[date, date] | None = None,
    choose: bool = True,
) -> dict:
    """
    Select the travel pass to use.

    Expired passes are dropped (SPEC §9.1). One pass left: that one. Several:
    the single pass whose validity covers ``for_dates`` (a renewal bought
    ahead never needs a question); otherwise a numbered prompt — only at a
    terminal, and an empty answer (Ctrl-D) ends the run rather than asking
    forever. With ``choose`` false (modes that only need a date range, not
    a product) the longest-lived pass is taken silently.

    Returns:
        The selected travel pass dict.

    Raises:
        SystemExit: If no valid pass exists, no terminal is there to choose
            at, or nothing was chosen.

    """
    valid = [tp for tp in travel_passes if not _is_expired(tp)]
    if len(valid) < len(travel_passes):
        logger.info(f"ignoring {len(travel_passes) - len(valid)} expired travel pass(es)")

    if not valid:
        pstatus(False, "no valid travel pass found" if travel_passes else "no travel pass found")
        print()
        sys.exit(1)

    if len(valid) == 1:
        return valid[0]

    if for_dates:
        covering = [tp for tp in valid if _covers(tp, for_dates)]
        if len(covering) == 1:
            logger.info(f"using the pass that covers {for_dates[0]} – {for_dates[1]}")
            return covering[0]
        if covering:
            valid = covering

    if not choose:
        return max(valid, key=lambda tp: tp.get("endTravelValidityDateTime") or "")

    if not sys.stdin.isatty():
        pstatus(False, f"{len(valid)} valid travel passes · run in a terminal to choose one")
        print()
        sys.exit(1)

    pinfo("available travel passes:")
    for i, tp in enumerate(valid, 1):
        name = tp.get("name", "Unknown")
        first, last = _pass_validity(tp)
        pinfo(f"  {i}. {name} ({first or '\u2014'} \u2192 {last or '\u2014'})")

    blank()
    while True:
        choice = ask(f"select pass [1-{len(valid)}]: ").strip()
        if not choice:  # end of input, or an empty answer: not a selection
            blank()
            pstatus(False, "no travel pass selected")
            print()
            sys.exit(1)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(valid):
                return valid[idx]
        except ValueError:
            pass
        pinfo("invalid selection, try again")


def booking_window(params: dict, today: date | None = None) -> tuple[date, date] | None:
    """
    First and last date a --book run can book.

    The selection from today on, minus the days the calendar filter skips.
    None when every day is skipped.
    """
    skip_w, skip_h = params.get("skip_weekends", True), params.get("skip_holidays", True)
    bookable = [d for d in booking_dates(params, today) if not skip_reason(d, skip_w, skip_h)]
    return (bookable[0], bookable[-1]) if bookable else None


def validate_dates_against_pass(cfg: dict, travel_pass: dict, today: date | None = None) -> None:
    """
    Validate that the booking window falls within the travel pass validity.

    The window covers bookable days only: it starts today when the selection
    began earlier (the booking loop does the same) and ignores the days the
    calendar filter skips, so a week ending on a Sunday after a pass that
    ends on the Friday is fine.

    Raises:
        SystemExit: If dates are outside validity.

    """
    vp_start, vp_end = _pass_validity(travel_pass)
    if vp_start is None or vp_end is None:
        return

    # Compare Swedish calendar dates: config dates are Swedish dates, and the
    # API's validity instants are midnight UTC (01:00/02:00 Swedish), so a
    # datetime comparison would reject the pass's first day. The end instant
    # is exclusive — the day after the last valid day — exactly as
    # --list-travelpasses shows it.
    window = booking_window(cfg.get("search_parameters", {}), today)
    if window is None:
        return
    s_start, s_end = window

    if s_start < vp_start or s_end > vp_end:
        pstatus(
            False,
            f"search dates ({s_start} \u2013 {s_end}) "
            f"are outside travel pass validity ({vp_start} \u2013 {vp_end})",
        )
        print()
        sys.exit(1)


def handle_list_travelpasses(client: SJClient, travel_passes: list) -> None:
    """Display travel passes with validity and receipt details."""
    receipt_info: dict[str, dict] = {}

    for tp in travel_passes:
        booking_id = tp.get("travelPassCreationBookingId", "")
        if not booking_id:
            continue

        # The receipt is a detail (the price): a failure must not hide the cards
        try:
            receipts = client.get_receipt_search(booking_id)
        except Exception as e:
            logger.warning(f"receipt search failed for {booking_id}: {e}")
            pwarn(f"could not fetch the receipt for {tp.get('name', '\u2014')}: {error_text(e)}")
            continue
        if not receipts:
            continue

        # Use first receipt from search results (contains amount/currency directly)
        receipt = receipts[0] if isinstance(receipts, list) else receipts
        if receipt:
            receipt_info[booking_id] = receipt

    print_travelpasses(travel_passes, receipt_info)


def _format_epoch(ts: object) -> str | None:
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
        # A session established in this run (tm.token) is the truth even when
        # the cache could not be written: never re-read a stale or absent file
        # over it. A fresh manager reads the cache.
        token_data = tm.token if tm.token is not None else tm.load()
    except SJAuthError as e:
        pstatus(False, str(e))
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


def main() -> None:
    """Entry point (script and console script): run the tool, exit 130 on Ctrl-C."""
    setup_logging(os.getenv("LOG_LEVEL", ""))
    print()
    try:
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
    except KeyboardInterrupt:
        pinfo("\ninterrupted by user")
        print()
        sys.exit(130)


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
    cm = CfgManager()
    if not cm.path.exists():
        # First-run setup is offered only by --login (the one operation that
        # makes sense without an existing session) and only on a terminal —
        # the prompts go to stdout, so a redirected run must not wait on one.
        # On success the run falls through into the login itself.
        offered = args.login and sys.stdin.isatty() and sys.stdout.isatty()
        try:
            created = offered and cm.create_interactive()
        except SJConfigError as e:
            blank()
            print_status_card(False, "config not created", lines=[str(e)])
            sys.exit(1)
        if not created:
            # Declined: the wizard already named the path, so only the hint.
            lines = (
                ["re-run --login when you want to create it"]
                if offered
                else [
                    f"expected a config file at {cm.path}",
                    "run --login in a terminal to create it",
                ]
            )
            print_status_card(False, "no configuration", lines=lines)
            sys.exit(1)

    try:
        cfg = cm.load()
        # Route/dates are only needed by the booking-shaped operations, the
        # dates selection only by --book (--cancel-date and --change-seat-date
        # take their own dates); seat_preference only by the change-seat modes.
        cm.verify_cfg(
            cfg,
            require_search=(
                args.book or args.cancel_date is not None or args.change_seat_date is not None
            ),
            require_dates=args.book,
            require_seat_preference=(
                args.change_seat_date is not None or args.change_seat_booking is not None
            ),
        )
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
            pstatus(False, error_text(e))
            print()
        sys.exit(1)

    # Handle --login: authenticate, then show the same card as --login-status.
    # A login that needed no full login is "already logged in".
    if args.login:
        if tm.save_error:
            pwarn(
                f"token cache not saved: {tm.save_error} · the next run will need to log in again"
            )
            blank()
        handle_login_status(
            tm, verdict="logged in" if auth_method == "full" else "already logged in"
        )

    # 3. Fetch membership and travel passes
    try:
        membership = client.get_membership(access_token)
        user_name = f"{membership.get('firstName')} {membership.get('lastName')}"

        tp_resp = client.get_travel_passes(access_token)
        travel_passes = tp_resp if isinstance(tp_resp, list) else tp_resp.get("travelPasses", [])
    except Exception as e:
        pstatus(False, f"initialization failed: {error_text(e)}")
        print()
        sys.exit(1)

    # --list-travelpasses shows every pass, expired ones included: no selection
    if args.list_travelpasses:
        print_header_box(
            [
                ("operation", "listing travel passes"),
                ("account", email),
                ("holder", user_name),
            ]
        )
        blank()
        try:
            handle_list_travelpasses(client, travel_passes)
        except Exception as e:
            _fail(e)
        print()
        return

    # Pick the pass: --book needs the product (the one covering its window,
    # or a choice at the terminal); the other modes only a date range.
    window = booking_window(cfg["search_parameters"]) if args.book else None
    active_pass = resolve_travel_pass(travel_passes, for_dates=window, choose=args.book)
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

    # 4. Mode dispatch. Every pass-scoped mode opens with the header box
    # (operation / travelpass / holder); auth modes lead with their cards.
    try:
        if args.list_bookings:
            print_header_box([("operation", "listing bookings"), *pass_rows])
            blank()
            handle_list_bookings(client, access_token, active_pass)

        elif args.cancel_date is not None:
            # Cancelling needs the route only: the config's dates selection
            # and the pass validity say nothing about the dates given here.
            operation = ("dry run · " if args.dry_run else "") + "cancelling bookings"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            ok = True
            for i, cancel_date in enumerate(args.cancel_dates):
                if i:
                    blank()
                ok &= handle_cancel_mode(
                    client, access_token, cfg, cancel_date, dry_run=args.dry_run
                )
            if not ok:
                print()
                sys.exit(1)

        elif args.cancel_booking is not None:
            operation = ("dry run · " if args.dry_run else "") + "cancelling bookings"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            ok = True
            for i, bn in enumerate(args.cancel_booking_numbers):
                if i:
                    blank()
                ok &= handle_cancel_booking(
                    client, access_token, active_pass, bn, dry_run=args.dry_run
                )
            if not ok:
                print()
                sys.exit(1)

        elif args.change_seat_date is not None:
            operation = ("dry run · " if args.dry_run else "") + "changing seats"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            if not handle_change_seat(
                client, access_token, cfg, dates=args.change_seat_dates, dry_run=args.dry_run
            ):
                print()
                sys.exit(1)

        elif args.change_seat_booking is not None:
            operation = ("dry run · " if args.dry_run else "") + "changing seats"
            print_header_box([("operation", operation), *pass_rows])
            blank()
            if not handle_change_seat(
                client,
                access_token,
                cfg,
                booking_numbers=args.change_seat_booking_numbers,
                travel_pass=active_pass,
                dry_run=args.dry_run,
            ):
                print()
                sys.exit(1)

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
            # routine step, not part of the day-by-day trail). From today:
            # the selection may start today, and today's stale provisional must be
            # visible to the cleanup too.
            b_start, b_end = booking_date_range(active_pass)
            with spinner("fetching existing bookings", trail=False):
                bookings_list = fetch_all_bookings(client, access_token, b_start, b_end)

            # Cleanup stale provisionals (never in a dry run: no mutations),
            # only our own: on the configured route and not brand new.
            if not args.dry_run:
                params = cfg["search_parameters"]
                route = (
                    client.resolve_station(params["station_from"]),
                    client.resolve_station(params["station_to"]),
                )
                bookings_list = cleanup_stale_provisionals(
                    client, access_token, bookings_list, route=route
                )

            # Process date range: one card per day, summary footer at the end
            counts = process_date_range(
                client,
                access_token,
                tm,
                cfg,
                tp_product_id,
                tp_token_id,
                bookings_list,
                dry_run=args.dry_run,
            )
            # SPEC §5.7: a checkout failure or an error on any day is a failure
            if counts.get("failed") or counts.get("error"):
                print()
                sys.exit(1)

    except SystemExit:
        raise
    except Exception as e:
        _fail(e)

    print()


def _fail(e: Exception) -> NoReturn:
    """Close a mode that died on an unexpected error: red ● line, exit 1."""
    logger.error(f"error: {e}")
    pstatus(False, f"error: {error_text(e)}")
    print()
    sys.exit(1)


if __name__ == "__main__":
    main()
