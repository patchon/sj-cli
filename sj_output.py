"""User-facing output helpers for the SJ API client."""

import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta

from sj_calendar import parse_api_datetime, sweden_now, to_sweden

logger = logging.getLogger(__name__)

_BRAILLE_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Serialises stdout writes between the spinner thread and pinfo/pdim so a
# message printed while a spinner is running does not interleave with a frame.
_stdout_lock = threading.Lock()
_spinner_active = False

# ANSI SGR codes used for output styling
BOLD = "1"
DIM = "2"
GREEN = "32"
YELLOW = "33"
MAGENTA = "35"
CYAN = "36"


def color_enabled() -> bool:
    """Colour only when writing to a terminal and NO_COLOR is not set."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def style(text: str, *codes: str) -> str:
    """Wrap text in ANSI SGR codes when colour is enabled, else return it unchanged."""
    if not codes or not text or not color_enabled():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def visible_len(text: str) -> int:
    """Length of text as displayed, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", text))


def pad(text: str, width: int) -> str:
    """Left-justify text to width, measuring visible characters only."""
    return text + " " * max(0, width - visible_len(text))


@contextmanager
def spinner(msg: str, interval: float = 0.08):
    """
    Context manager that shows a braille spinner while work runs.

    Usage:
        with spinner("fetching bookings"):
            do_slow_work()

    The spinner line is erased when the block completes and replaced by a
    dim trail line: "✓ msg" on success, "✗ msg" if the block raised. When
    stdout is not a TTY the spinner is skipped and only the trail line is
    printed.

    """
    stop = threading.Event()
    text = msg.lower()

    if not sys.stdout.isatty():
        try:
            yield
        except BaseException:
            print(style(f"\u2717 {text}", DIM), file=sys.stdout)
            raise
        print(style(f"\u2713 {text}", DIM), file=sys.stdout)
        return

    def _spin():
        i = 0
        while not stop.is_set():
            frame = _BRAILLE_FRAMES[i % len(_BRAILLE_FRAMES)]
            with _stdout_lock:
                sys.stdout.write(f"\r{frame} {text}")
                sys.stdout.flush()
            i += 1
            stop.wait(interval)
        # Erase the spinner line
        with _stdout_lock:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

    global _spinner_active  # noqa: PLW0603
    _spinner_active = True
    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    mark = "\u2713"
    try:
        yield
    except BaseException:
        mark = "\u2717"
        raise
    finally:
        stop.set()
        t.join()
        _spinner_active = False
        _emit(style(f"{mark} {text}", DIM))


def _emit(line: str) -> None:
    """Print a line to stdout; if a spinner is drawing, erase its frame first."""
    with _stdout_lock:
        if _spinner_active:
            sys.stdout.write("\r\033[2K")
        print(line, file=sys.stdout)


def pinfo(msg: str) -> None:
    """Print a status message to stdout."""
    _emit(msg.lower())


def pdim(msg: str) -> None:
    """Print a low-emphasis context message (dimmed when colour is enabled)."""
    _emit(style(msg.lower(), DIM))


def format_duration(iso_duration: str) -> str:
    """Format an ISO 8601 duration like PT4H37M into 4h 37m."""
    if not iso_duration:
        return "\u2014"
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", iso_duration)
    if not m:
        return iso_duration
    hours, minutes = m.group(1), m.group(2)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    return " ".join(parts) if parts else iso_duration


def format_class_name(raw_name: str) -> str:
    """Clean up class name by stripping refund/flexibility suffix."""
    if not raw_name or raw_name == "\u2014":
        return raw_name or "\u2014"
    return raw_name.split(",", maxsplit=1)[0].strip()


def format_table(headers: list[str], rows: list[list[str]], title: str = "") -> str:
    """
    Format data as an aligned text table.

    Args:
        headers: Column header strings.
        rows: List of rows, each row a list of cell strings.
        title: Optional table title.

    Returns:
        The formatted table as a multi-line string.

    """
    col_widths = []
    for i, header in enumerate(headers):
        max_width = len(header)
        for row in rows:
            if i < len(row):
                max_width = max(max_width, visible_len(row[i]))
        col_widths.append(max_width + 2)

    total_width = sum(col_widths)
    separator = style("\u2500" * total_width, DIM)

    lines = []
    if title:
        lines.append(style(title.lower(), BOLD))

    lines.append(separator)
    header_line = "".join(
        pad(style(h.lower(), BOLD), w) for h, w in zip(headers, col_widths, strict=False)
    )
    lines.append(header_line)
    lines.append(separator)

    for row in rows:
        cells = []
        for i, w in enumerate(col_widths):
            cell = row[i] if i < len(row) else "\u2014"
            cells.append(pad(cell, w))
        lines.append("".join(cells))

    lines.append(separator)
    return "\n".join(lines)


def print_dry_run_table(results: list[dict]) -> None:
    """
    Print dry-run results as a formatted table.

    Args:
        results: List of dicts with keys: date, direction, departure,
                 arrival, comfort_class, flexibility, note. A non-empty
                 note means the leg cannot be booked; it is shown in place
                 of the flexibility cell.

    """
    headers = ["Date", "Direction", "Departure", "Arrival", "Class", "Flexibility"]
    rows = []
    for r in results:
        note = r.get("note", "")
        rows.append([
            r.get("date", "\u2014"),
            r.get("direction", "\u2014"),
            r.get("departure", "\u2014"),
            r.get("arrival", "\u2014"),
            r.get("comfort_class", "\u2014"),
            style(note, DIM) if note else r.get("flexibility", "\u2014"),
        ])

    table = format_table(headers, rows, title="\U0001f50d dry run results")
    print(f"\n{table}", file=sys.stdout)


def _format_date_label(date_str: str) -> str:
    """Turn 2026-08-18 into 'tue 18 aug 2026'; fall back to the raw string."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %d %b %Y").lower()
    except (ValueError, TypeError):
        return date_str or "\u2014"


def _reverse_route(route: str) -> str:
    """'A → B' becomes 'B → A'; anything else is returned unchanged."""
    parts = [x.strip() for x in route.split("\u2192")]
    if len(parts) != 2:
        return route
    return f"{parts[1]} \u2192 {parts[0]}"


def _group_route(legs: list[dict]) -> str:
    """Summarise a booking's route: 'A ⇄ B' for a round trip, else the distinct routes."""
    routes = []
    for leg in legs:
        r = leg.get("route", "\u2014")
        if r not in routes:
            routes.append(r)
    if len(routes) == 2 and routes[1] == _reverse_route(routes[0]):
        a, b = (x.strip() for x in routes[0].split("\u2192"))
        return f"{a} \u21c4 {b}"
    return " \u00b7 ".join(routes)


def print_bookings_table(bookings: list[dict], pass_name: str, summary: bool = True) -> None:
    """
    Print bookings as one card per travel day, legs indented beneath.

    Grouping is by date rather than booking number because with
    `book_partial` each leg is its own booking; the booking number is shown
    on every leg line so it is always visible for cancellation.

    Args:
        bookings: Leg rows (sorted by departure) with keys: date, direction,
                  departure, arrival, duration, comfort_class, route,
                  booking_number, past ("Y"/"N"), and optionally train, seat.
        pass_name: Travel pass name (or other context) for the title.
        summary: Print the "N day(s) · N booking(s) · …" footer line.

    """
    # Group legs by date, preserving first-seen order
    groups: dict[str, list[dict]] = {}
    for leg in bookings:
        groups.setdefault(leg.get("date", "\u2014"), []).append(leg)

    # Column widths across all legs so cards line up with each other
    def w(key: str) -> int:
        return max((visible_len(leg.get(key) or "\u2014") for leg in bookings), default=0)

    w_dur, w_train, w_seat, w_class = w("duration"), w("train"), w("seat"), w("comfort_class")

    lines = ["", style(f"\U0001f3ab {pass_name.lower()}", BOLD), ""]
    past_legs = 0
    for date_str, legs in groups.items():
        all_past = all(leg.get("past") == "Y" for leg in legs)
        header = f"{style(_format_date_label(date_str), BOLD)}   {_group_route(legs)}"
        if all_past:
            header = style(_ANSI_RE.sub("", header), DIM)
            if not color_enabled():
                header += "   past"  # dimming is invisible here, so say it
        lines.append(header)

        # A standalone return booking is "OUTBOUND" to the API, so infer the
        # arrow from the route: reverse of the day's first leg → return.
        first_route = legs[0].get("route", "")
        for leg in legs:
            is_past = leg.get("past") == "Y"
            past_legs += is_past
            is_return = leg.get("route") == _reverse_route(first_route)
            arrow = style("\u2190", MAGENTA) if is_return else style("\u2192", CYAN)
            cells = [
                f"{leg.get('departure', '\u2014')} \u2013 {leg.get('arrival', '\u2014')}",
                pad(leg.get("duration") or "\u2014", w_dur),
                pad(leg.get("train") or "\u2014", w_train),
                pad(leg.get("seat") or "\u2014", w_seat),
                pad(leg.get("comfort_class") or "\u2014", w_class),
            ]
            body = "   ".join(cells)
            number = leg.get("booking_number") or "\u2014"
            if is_past:
                arrow = style(_ANSI_RE.sub("", arrow), DIM)
                body, number = style(body, DIM), style(number, DIM)
            else:
                number = style(number, BOLD)
            lines.append(f"  {arrow} {body}   {number}")
        lines.append("")

    if summary:
        n_bookings = len({leg.get("booking_number") for leg in bookings})
        footer = (
            f"\U0001f686 {len(groups)} day(s) \u00b7 {n_bookings} booking(s) "
            f"\u00b7 {len(bookings)} leg(s)"
        )
        if past_legs:
            footer += f" \u00b7 {past_legs} in the past"
        lines.append(style(footer, DIM))
    else:
        lines.pop()  # drop trailing blank line
    print("\n".join(lines))


def _format_tp_date(iso_str: str | None, exclusive: bool = False) -> str:
    """
    Format an ISO datetime string to YYYY-MM-DD in Swedish local time.

    Args:
        iso_str: ISO datetime string from the API.
        exclusive: If True, subtract one day (API end dates are exclusive).

    """
    if not iso_str:
        return "\u2014"
    try:
        local_dt = to_sweden(iso_str)
        if exclusive:
            local_dt -= timedelta(days=1)
        return local_dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str[:10]


def _days_remaining(end_iso: str | None) -> str:
    """Calculate days remaining until end of last valid day."""
    if not end_iso:
        return "\u2014"
    try:
        # End date is exclusive, so the last valid day ends at end_dt
        days = (parse_api_datetime(end_iso) - sweden_now()).days
        if days < 0:
            return "expired"
        return str(days)
    except (ValueError, TypeError):
        return "\u2014"


def print_travelpasses_table(
    travel_passes: list[dict], receipt_info: dict[str, dict] | None = None
) -> None:
    """
    Print travel passes as a formatted table.

    Args:
        travel_passes: List of travel pass dicts from the API.
        receipt_info: Optional dict mapping booking ID to receipt data.

    """
    if not travel_passes:
        pinfo("no travel passes found")
        return

    headers = ["Name", "Card Number", "Holder", "Valid From", "Valid To", "Days Left", "Price"]
    rows = []

    for tp in travel_passes:
        name = tp.get("name", "\u2014")
        card_number = tp.get("code", "\u2014")

        holder_data = tp.get("holder", {})
        first = holder_data.get("firstName", "")
        last = holder_data.get("lastName", "")
        email = holder_data.get("email", "")
        holder = f"{first} {last} ({email})".strip() if first or last else "\u2014"

        valid_from = _format_tp_date(tp.get("startTravelValidityDateTime"))
        valid_to = _format_tp_date(tp.get("endTravelValidityDateTime"), exclusive=True)
        days_left = _days_remaining(tp.get("endTravelValidityDateTime"))

        # Try to get price from receipt info
        price = "\u2014"
        if receipt_info:
            booking_id = tp.get("travelPassCreationBookingId", "")
            receipt = receipt_info.get(booking_id)
            if receipt:
                price = _extract_price(receipt)

        rows.append([name, card_number, holder, valid_from, valid_to, days_left, price])

    table = format_table(headers, rows, title="\U0001f3ab travel passes")
    print(f"\n{table}")
    pinfo(f"{len(travel_passes)} travel pass(es) shown")


def _format_amount(raw_amount: str | float, currency: str = "SEK") -> str:
    """Format a raw amount value into a readable price string."""
    try:
        num = float(raw_amount)
        # Display as integer if no meaningful decimals, otherwise 2 decimals
        if num == int(num):
            return f"{int(num)} {currency}"
        return f"{num:.2f} {currency}"
    except (ValueError, TypeError):
        return f"{raw_amount} {currency}"


def _extract_price(receipt_data: dict) -> str:
    """Extract a formatted price string from receipt data."""
    # Handle flat amount + currency fields (e.g. from receipt search)
    amount = receipt_data.get("amount")
    if amount is not None:
        currency = receipt_data.get("currency", "SEK")
        return _format_amount(amount, currency)

    # Try other common price fields
    for key in ["totalAmount", "total", "price", "totalPrice"]:
        val = receipt_data.get(key)
        if val is not None:
            if isinstance(val, dict):
                amt = val.get("amount", val.get("value", ""))
                cur = val.get("currency", val.get("currencyCode", "SEK"))
                if amt:
                    return _format_amount(amt, cur)
            elif isinstance(val, (int, float, str)) and val:
                return _format_amount(val)

    logger.debug(f"could not extract price from receipt: {list(receipt_data.keys())}")
    return "\u2014"
