"""User-facing output helpers for the SJ API client."""

import logging
import os
import re
import sys
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta

from sj_cli.dates import parse_api_datetime, sweden_now, to_sweden
from sj_cli.seats import Seat, number_key, seat_words

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
RED = "91"
GREEN = "92"
YELLOW = "93"
MAGENTA = "95"
CYAN = "96"


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
                _emit(_trail_line(False, text))
            raise
        if trail:
            _emit(_trail_line(True, text))
        return

    def _spin():
        i = 0
        while not stop.is_set():
            frame = _BRAILLE_FRAMES[i % len(_BRAILLE_FRAMES)]
            with _stdout_lock:
                sys.stdout.write(f"\r{_MARGIN}{prefix}{frame} {text}")
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
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        stop.set()
        t.join()
        _spinner_active = False
        if trail:
            _emit(_trail_line(ok, text))


def _trail_line(ok: bool, text: str) -> str:
    """Trail line for a finished step: green ✓ / red ✗ mark, dim text."""
    mark = style("✓", GREEN) if ok else style("✗", RED)
    return f"{mark} {style(text, DIM)}"


_MARGIN = " "  # global left margin: every printed line starts off the terminal edge


def _emit(line: str) -> None:
    """Print a line to stdout (indented if inside indented()); erase a live spinner frame first."""
    with _stdout_lock:
        if _spinner_active:
            sys.stdout.write("\r\033[2K")
        print(f"{_MARGIN}{_indent}{line}" if line else line, file=sys.stdout)


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


def _status_line(ok: bool | None, msg: str) -> str:
    """Operation status line: ● coloured by outcome, dim summary text."""
    mark = style("●", DIM if ok is None else GREEN if ok else RED)
    return f"{mark} {style(msg, DIM)}"


def pstatus(ok: bool | None, msg: str) -> None:
    """
    Print the status line that closes an operation.

    Args:
        ok: True when the run changed something (green ●), None when it ran
            fine but changed nothing — a listing, a dry run (dim ●), False on
            a failure, an abort, or nothing found (red ●).
        msg: The summary text, printed dim.

    """
    _emit(_status_line(ok, msg))


def pwarn(msg: str) -> None:
    """Warning line: yellow '!' mark, dim text — a deviation worth noticing."""
    _emit(f"{style('!', YELLOW)} {style(msg, DIM)}")


def blank() -> None:
    """Print an empty line (never indented)."""
    _emit("")


def prompt(text: str) -> None:
    """Inline input prompt: cyan '?' marker + text, no trailing newline."""
    with _stdout_lock:
        sys.stdout.write(f"{_MARGIN}{_indent}{style('?', CYAN)} {text}")
        sys.stdout.flush()


def ask(text: str) -> str:
    """Show an inline '?' prompt and read one line of input ('' on EOF)."""
    prompt(text)
    try:
        reply = input()
        if not sys.stdin.isatty():
            print()  # close the prompt line when input wasn't echoed
    except EOFError:
        print()
        reply = ""
    return reply


def ask_optional(text: str) -> str | None:
    """Like ask(), but None on EOF — the caller can tell Ctrl-D from an empty line."""
    prompt(text)
    try:
        reply = input()
    except EOFError:
        print()
        return None
    if not sys.stdin.isatty():
        print()  # close the prompt line when input wasn't echoed
    return reply


def confirm(question: str) -> bool:
    """Ask a yes/no question on stdin; 'y' and 'yes' (any case) mean yes."""
    return ask(question).strip().lower() in ("y", "yes")


def print_header_box(rows: list[tuple[str, str]]) -> None:
    """
    Rounded header banner opening every pass-scoped mode.

    Dim borders and labels, plain values; the first row's value (the
    operation) is bold. Width fits the longest row.
    """
    label_w = max(visible_len(label) for label, _ in rows)
    body = []
    for i, (label, value) in enumerate(rows):
        val = style(value, BOLD) if i == 0 else value
        body.append(f"{style(pad(label, label_w), DIM)}   {val}")
    inner = max(visible_len(line) for line in body)
    bar = style("│", DIM)
    _emit(style(f"╭{'─' * (inner + 4)}╮", DIM))
    for line in body:
        _emit(f"{bar}  {pad(line, inner)}  {bar}")
    _emit(style(f"╰{'─' * (inner + 4)}╯", DIM))


def print_status_card(
    ok: bool,
    verdict: str,
    facts: Sequence[tuple[str, str]] = (),
    lines: Sequence[str] = (),
) -> None:
    """
    Status card, the result block of card-first modes and failures.

    Coloured dot + bold verdict, blank line, then dim-labelled facts and/or
    plain indented lines (for detail without natural labels, e.g. config
    errors), trailing blank.
    """
    dot = style("●", GREEN if ok else RED)
    _emit(f"{dot} {style(verdict, BOLD)}")
    if facts or lines:
        blank()
    for label, value in facts:
        _emit(_fact_line(label, value))
    for line in lines:
        _emit(f"  {line}")
    blank()


def _fact_line(label: str, value: str) -> str:
    """One card fact: dim 7-char label, plain value (shared card grammar)."""
    return f"  {style(pad(label, 7), DIM)}   {value}"


def print_fact(label: str, value: str) -> None:
    """Print one card fact at top level (run headers use the same grammar)."""
    _emit(_fact_line(label, value))


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


def group_route(legs: list[dict]) -> str:
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
    # Reference route: the first leg that is not an explicit return, so a
    # subset that starts with the return (cancel choices, the selection
    # echo) keeps outbound → / return ←.
    first_route = next(
        (r.get("route", "") for r in rows if r.get("direction") != "Return"),
        "",
    )
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


def print_seat_choices(seats: list[Seat], comforts: dict[str, str] | None = None) -> None:
    """
    List the free seats to choose from: a dim header per carriage, then a grid.

    Grouped by carriage (lowest first), each seat as 'number words' in the
    config vocabulary, so what the user types at the prompt is what is shown
    here. `comforts` maps a carriage number to its display comfort, as
    `seats.carriage_comfort` reads it; a carriage the map says nothing
    about simply omits it.
    """
    by_carriage: dict[str, list[Seat]] = {}
    for seat in seats:
        by_carriage.setdefault(seat["carriage"], []).append(seat)

    for carriage, group in sorted(by_carriage.items(), key=lambda kv: number_key(kv[0])):
        parts = [f"{s['number']} {', '.join(seat_words(s))}" for s in group]
        header = f"free in carriage {carriage}"
        comfort = (comforts or {}).get(carriage, "")
        if comfort:
            header += f" · {comfort}"
        pdim(f"{header} · {len(group)} seat{'' if len(group) == 1 else 's'}")
        # Columns sized to the widest entry (a seat can carry every property),
        # three at most and fewer when three would not fit an 80-column line.
        width = max(visible_len(p) for p in parts)
        columns = max(1, min(3, 78 // (width + 3)))
        for i in range(0, len(parts), columns):
            pdim("  " + "   ".join(pad(p, width) for p in parts[i : i + columns]).rstrip())


def print_bookings_table(bookings: list[dict], summary: bool = True) -> None:
    """
    Print bookings as one card per travel day, legs indented beneath.

    Card-first: no title and no leading blank — the first day header is the
    headline. Grouping is by date rather than booking number because with
    `book_partial` each leg is its own booking; the booking number is shown
    on every leg line so it is always visible for cancellation.

    Args:
        bookings: Leg rows (sorted by departure) with keys: date, direction,
                  departure, arrival, duration, comfort_class, route,
                  booking_number, past ("Y"/"N"), and optionally train, seat.
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
    past_legs = 0
    for i, (date_str, legs) in enumerate(padded_groups.items()):
        if i:
            lines.append("")
        all_past = all(leg.get("past") == "Y" for leg in legs)
        header = day_header(date_str, group_route(legs), dim=all_past)
        if all_past and not color_enabled():
            header += "   past"  # dimming is invisible here, so say it
        lines.append(header)
        past_legs += sum(leg.get("past") == "Y" for leg in legs)
        lines += [f"  {line}" for line in leg_lines(legs)]

    if summary:
        n_bookings = len({leg.get("booking_number") for leg in bookings})
        footer = f"{len(groups)} day(s) \u00b7 {n_bookings} booking(s)"
        if past_legs:
            footer += f" \u00b7 {past_legs} in the past"
        lines.append("")
        lines.append(_status_line(None, footer))
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


def print_travelpasses(
    travel_passes: list[dict], receipt_info: dict[str, dict] | None = None
) -> None:
    """
    Print travel passes as cards in the shared card grammar.

    One card per pass: bold name + card number header, then dim-labelled
    facts (holder, validity with days left, price from the receipt).
    Unknown facts are omitted rather than shown as placeholders.

    Args:
        travel_passes: List of travel pass dicts from the API.
        receipt_info: Optional dict mapping booking ID to receipt data.

    """
    if not travel_passes:
        pstatus(False, "no travel passes found")
        return

    for i, tp in enumerate(travel_passes):
        if i:
            blank()
        _emit(f"{style(tp.get('name', '\u2014'), BOLD)}   {tp.get('code', '\u2014')}")

        holder_data = tp.get("holder") or {}
        name = " ".join(p for p in (holder_data.get("firstName"), holder_data.get("lastName")) if p)
        if name:
            email = holder_data.get("email")
            _emit(_fact_line("holder", f"{name} ({email})" if email else name))

        valid_from = _format_tp_date(tp.get("startTravelValidityDateTime"))
        valid_to = _format_tp_date(tp.get("endTravelValidityDateTime"), exclusive=True)
        if valid_from != "\u2014" or valid_to != "\u2014":
            valid = f"{valid_from} \u2013 {valid_to}"
            days_left = _days_remaining(tp.get("endTravelValidityDateTime"))
            if days_left == "expired":
                valid += " (expired)"
            elif days_left != "\u2014":
                valid += f" ({days_left} days left)"
            _emit(_fact_line("valid", valid))

        if receipt_info:
            receipt = receipt_info.get(tp.get("travelPassCreationBookingId", ""))
            if receipt:
                price = _extract_price(receipt)
                if price != "\u2014":
                    _emit(_fact_line("price", price))

    blank()
    pstatus(None, f"{len(travel_passes)} travel pass(es)")


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
                amt = val.get("amount")
                if amt is None:
                    amt = val.get("value")
                cur = val.get("currency") or val.get("currencyCode") or "SEK"
                if amt is not None:  # 0 is a price (a free renewal), not "unknown"
                    return _format_amount(amt, cur)
            elif isinstance(val, (int, float)) or (isinstance(val, str) and val):
                return _format_amount(val)

    logger.debug(f"could not extract price from receipt: {list(receipt_data.keys())}")
    return "\u2014"
