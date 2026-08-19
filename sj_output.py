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


_indent = ""


@contextmanager
def indented(prefix: str = "  "):
    """
    Indent everything printed through pinfo/pdim/spinner/print_* inside the block.

    Used to nest a day's progress and result lines under its day header.
    """
    global _indent  # noqa: PLW0603
    previous = _indent
    _indent = previous + prefix
    try:
        yield
    finally:
        _indent = previous


@contextmanager
def spinner(msg: str, interval: float = 0.08, trail: bool = True):
    """
    Context manager that shows a braille spinner while work runs.

    Usage:
        with spinner("fetching bookings"):
            do_slow_work()

    The spinner line is erased when the block completes and, when ``trail``
    is true, replaced by a dim trail line: "✓ msg" on success, "✗ msg" if the
    block raised. When stdout is not a TTY the spinner is skipped and only
    the trail line is printed. Text is printed as given (no case changes).

    """
    stop = threading.Event()
    text = msg
    prefix = _indent

    if not sys.stdout.isatty():
        try:
            yield
        except BaseException:
            if trail:
                _emit(style(f"\u2717 {text}", DIM))
            raise
        if trail:
            _emit(style(f"\u2713 {text}", DIM))
        return

    def _spin():
        i = 0
        while not stop.is_set():
            frame = _BRAILLE_FRAMES[i % len(_BRAILLE_FRAMES)]
            with _stdout_lock:
                sys.stdout.write(f"\r{prefix}{frame} {text}")
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
        if trail:
            _emit(style(f"{mark} {text}", DIM))


def _emit(line: str) -> None:
    """Print a line to stdout (indented if inside indented()); erase a live spinner frame first."""
    with _stdout_lock:
        if _spinner_active:
            sys.stdout.write("\r\033[2K")
        print(f"{_indent}{line}" if line else line, file=sys.stdout)


def pinfo(msg: str) -> None:
    """
    Print a status message to stdout.

    Convention: prose is lowercase; identifiers keep their case (booking
    numbers are upper-case, station names as SJ writes them).
    """
    _emit(msg)


def pdim(msg: str) -> None:
    """Print a low-emphasis context message (dimmed when colour is enabled)."""
    _emit(style(msg, DIM))


def blank() -> None:
    """Print an empty line (never indented)."""
    _emit("")


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


def day_header(date_str: str, detail: str = "", dim: bool = False) -> str:
    """'tue 15 sep 2026   <detail>' with a bold date; the whole line dimmed when dim."""
    label = _format_date_label(date_str)
    line = f"{style(label, BOLD)}   {detail}" if detail else style(label, BOLD)
    if dim:
        line = style(_ANSI_RE.sub("", line), DIM)
    return line


def print_day_header(date_str: str, route: str) -> None:
    """Start a day card: bold date + route, e.g. 'tue 15 sep 2026   A ⇄ B'."""
    _emit(day_header(date_str, route))


def print_day_note(date_str: str, note: str) -> None:
    """A one-line day card for a day that needs no work: bold date + dim note."""
    _emit(f"{style(_format_date_label(date_str), BOLD)}   {style(note, DIM)}")


# Leg-line columns after the time range, in display order (row keys). A
# row's "note" (why it cannot be booked) is shown dimmed in the flexibility cell.
_LEG_COLUMNS = ("duration", "train", "seat", "comfort_class", "flexibility", "booking_number")


def _cell(row: dict, col: str) -> str:
    """Raw (unstyled) cell text for a leg-line column."""
    if col == "flexibility" and row.get("note"):
        return str(row["note"])
    return str(row.get(col) or "\u2014")


def _has(row: dict, col: str) -> bool:
    return bool(row.get(col)) or (col == "flexibility" and bool(row.get("note")))


def leg_lines(rows: list[dict]) -> list[str]:
    """
    Render legs as aligned lines (no indent): "-> 04:01 - 08:38   4h 37m   X 2000 520   ...".

    Row keys: departure, arrival, route, and optionally duration, train, seat,
    comfort_class, flexibility, note, booking_number, past ("Y"/"N"). Columns
    that are empty on every row are omitted, so the same renderer serves
    bookings (train/seat/number), dry runs (flexibility/note) and cancels.
    The arrow is inferred from the route: a leg whose route is the reverse of
    the first leg's is a return (←); the API reports a standalone return
    booking as OUTBOUND, so the direction field alone is not enough.
    """
    if not rows:
        return []
    cols = [c for c in _LEG_COLUMNS if any(_has(r, c) for r in rows)]
    widths = {c: max(visible_len(_cell(r, c)) for r in rows) for c in cols}
    first_route = rows[0].get("route", "")
    lines = []
    for row in rows:
        is_past = row.get("past") == "Y"
        is_return = bool(first_route) and row.get("route") == _reverse_route(first_route)
        if not first_route and row.get("direction") == "Return":
            is_return = True
        arrow = style("\u2190", MAGENTA) if is_return else style("\u2192", CYAN)
        cells = [f"{row.get('departure', '\u2014')} \u2013 {row.get('arrival', '\u2014')}"]
        for c in cols:
            val = _cell(row, c)
            if c == "flexibility" and row.get("note"):
                val = style(val, DIM)
            cells.append(pad(val, widths[c]))
        number = cells.pop() if "booking_number" in cols else None
        body = "   ".join(cells)
        if is_past:
            arrow = style(_ANSI_RE.sub("", arrow), DIM)
            body = style(_ANSI_RE.sub("", body), DIM)
            if number is not None:
                number = style(_ANSI_RE.sub("", number), DIM)
        elif number is not None:
            number = style(number, BOLD)
        line = f"{arrow} {body}" + (f"   {number}" if number is not None else "")
        lines.append(line.rstrip())
    return lines


def print_leg_lines(rows: list[dict]) -> None:
    """Print leg lines at the current indent (used inside a day card)."""
    for line in leg_lines(rows):
        _emit(line)


def print_bookings_table(bookings: list[dict], pass_name: str | None, summary: bool = True) -> None:
    """
    Print bookings as one card per travel day, legs indented beneath.

    Grouping is by date rather than booking number because with
    `book_partial` each leg is its own booking; the booking number is shown
    on every leg line so it is always visible for cancellation.

    Args:
        bookings: Leg rows (sorted by departure) with keys: date, direction,
                  departure, arrival, duration, comfort_class, route,
                  booking_number, past ("Y"/"N"), and optionally train, seat.
        pass_name: Travel pass name for the title line, or None for no title.
        summary: Print the "N day(s) · N booking(s) · …" footer line.

    """
    # Group legs by date, preserving first-seen order
    groups: dict[str, list[dict]] = {}
    for leg in bookings:
        groups.setdefault(leg.get("date", "\u2014"), []).append(leg)

    # Column widths across all legs so cards line up with each other: pad
    # every row's values to the global width before rendering.
    widths = {
        c: max((visible_len(_cell(leg, c)) for leg in bookings), default=0) for c in _LEG_COLUMNS
    }
    padded_groups: dict[str, list[dict]] = {}
    for leg in bookings:
        padded = {**leg, **{c: pad(str(leg[c]), widths[c]) for c in _LEG_COLUMNS if leg.get(c)}}
        padded_groups.setdefault(leg.get("date", "\u2014"), []).append(padded)

    lines: list[str] = []
    if pass_name:
        lines += ["", style(f"\U0001f3ab {pass_name.lower()}", BOLD)]
    lines.append("")
    past_legs = 0
    for date_str, legs in padded_groups.items():
        all_past = all(leg.get("past") == "Y" for leg in legs)
        header = day_header(date_str, _group_route(legs), dim=all_past)
        if all_past and not color_enabled():
            header += "   past"  # dimming is invisible here, so say it
        lines.append(header)
        past_legs += sum(leg.get("past") == "Y" for leg in legs)
        lines += [f"  {line}" for line in leg_lines(legs)]
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
    for line in lines:
        _emit(line)


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
