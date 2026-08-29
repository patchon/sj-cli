"""User-facing output helpers for the SJ API client."""

import logging
import os
import re
import select
import shutil
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta

from sj_cli.dates import parse_api_datetime, sweden_now, to_sweden
from sj_cli.errors import SJError
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


# --- list pickers ---------------------------------------------------------------
#
# select_filtered and select_list draw a small frame under an inline "?"
# prompt and read keystrokes one at a time (cbreak mode; POSIX only, which
# is what the tool targets). Every frame has the same height — the prompt
# line, `height` rows, one footer line — so the cursor arithmetic is
# trivial: draw, move up, done. With `keys` given the widget reads from
# that iterator instead of the terminal (tests) and never touches termios.
#
# Never call a picker inside spinner(): the spinner's single-line clear
# knows nothing about the frame below it, so the two would scribble over
# each other (no deadlock — _stdout_lock is never held across a yield).

_CSI_FINAL = range(0x40, 0x7F)
_CSI_MAX = 16  # bytes: an escape sequence longer than this is not one we know
_ESC_WAIT = 0.1  # seconds: a lone Esc is one that nothing follows within this


def _read_within(fd: int) -> bytes | None:
    """One more byte of an escape sequence; None when none arrives within _ESC_WAIT."""
    ready, _, _ = select.select([fd], [], [], _ESC_WAIT)
    return os.read(fd, 1) if ready else None


def _read_escape(fd: int) -> str:
    """
    The key an Esc byte started, once the rest of its sequence has arrived.

    A sequence is bounded both ways: every byte is awaited for _ESC_WAIT
    only, and at most _CSI_MAX of them are read. Nothing following is a
    lone Esc; a sequence that stalls or overruns is "" (ignored) rather
    than a stray abort, and a stream that ends mid-sequence is "eof".
    """
    second = _read_within(fd)
    if second is None:
        return "esc"  # nothing followed it
    if not second:
        return "eof"
    if second == b"O":  # application cursor mode: ESC O A/B
        third = _read_within(fd)
        if third is None:
            return ""
        if not third:
            return "eof"
        return {b"A": "up", b"B": "down"}.get(third, "")
    if second != b"[":
        return ""
    seq = b""
    while len(seq) < _CSI_MAX:
        ch = _read_within(fd)
        if ch is None:
            return ""
        if not ch:
            return "eof"
        seq += ch
        if ch[0] in _CSI_FINAL:
            return {b"A": "up", b"B": "down", b"Z": "shift-tab"}.get(seq, "")
    return ""


def _read_key(fd: int) -> str:
    """
    One keystroke from fd: a printable character or a key name.

    Names: "enter", "backspace", "up", "down", "tab", "shift-tab", "esc",
    "eof". Multi-byte UTF-8 is assembled from its lead byte (so "ö" arrives
    whole); an unknown escape sequence or control byte is "" (the pickers
    ignore it).
    """
    first = os.read(fd, 1)
    if not first:
        return "eof"
    b = first[0]
    if b == 0x1B:
        return _read_escape(fd)
    if b in (0x0D, 0x0A):
        return "enter"
    if b in (0x7F, 0x08):
        return "backspace"
    if b == 0x09:
        return "tab"
    if b == 0x04:
        return "eof"
    if b < 0x20:
        return ""
    # UTF-8: the lead byte says how many continuation bytes follow. A slow
    # pipe can hand them over in pieces, so read until the character is whole.
    length = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
    raw = first
    while len(raw) < length:
        more = os.read(fd, length - len(raw))
        if not more:
            return ""
        raw += more
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


@contextmanager
def _cbreak(fd: int) -> Iterator[None]:
    """Put the terminal in cbreak mode for the block; always restore it (Ctrl-C included)."""
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _terminal_keys(fd: int) -> Iterator[str]:
    while True:
        yield _read_key(fd)


def _frame_size() -> tuple[int, int]:
    size = shutil.get_terminal_size((80, 24))
    return size.columns, size.lines


def _clip(text: str, width: int) -> str:
    """
    text, or its first width-1 visible characters and an ellipsis.

    Styled text is fine: the width is measured in visible characters, and
    text that fits comes back untouched. Truncating strips the styling —
    a cut mid-sequence would drop the reset and bleed the colour into
    everything printed after the frame.
    """
    if visible_len(text) <= width:
        return text
    return _ANSI_RE.sub("", text)[: max(0, width - 1)] + "…"


def _scroll(first: int, highlight: int, height: int) -> int:
    """The first visible row so that the highlighted one is inside the window."""
    if highlight < first:
        return highlight
    if highlight >= first + height:
        return highlight - height + 1
    return first


def _prompt_prefix() -> str:
    return f"{_MARGIN}{_indent}{style('?', CYAN)} "


def _prompt_line(text: str, width: int) -> str:
    """The inline '? ' prefix plus text, clipped so the whole line fits width."""
    prefix = _prompt_prefix()
    return prefix + _clip(text, width - visible_len(prefix))


def _draw_frame(prompt_line: str, lines: list[str], height: int) -> None:
    """
    Redraw the picker: the prompt line, then exactly height+1 lines, cursor back on the prompt.

    Every line is cleared to its end so a shorter frame leaves nothing of a
    taller one; the cursor ends at the end of the prompt line.
    """
    frame = lines[: height + 1] + [""] * (height + 1 - len(lines))
    out = ["\r" + prompt_line + "\x1b[K"]
    out += ["\n" + line + "\x1b[K" for line in frame]
    out.append(f"\x1b[{height + 1}A\r\x1b[{visible_len(prompt_line) + 1}G")
    with _stdout_lock:
        sys.stdout.write("".join(out))
        sys.stdout.flush()


def _close_frame(final_line: str) -> None:
    """Wipe the prompt line and the frame under it; leave final_line as the one line behind."""
    with _stdout_lock:
        sys.stdout.write("\r\x1b[J" + final_line + "\n")
        sys.stdout.flush()


def _rows(items: Sequence[str], first: int, highlight: int, height: int, width: int) -> list[str]:
    rows = []
    for i, text in enumerate(items[first : first + height], start=first):
        clipped = _clip(text, width - 4)
        rows.append(f"  {style('›', CYAN)} {clipped}" if i == highlight else f"    {clipped}")
    return rows


def _window_text(first: int, shown: int, total: int) -> str:
    return f"{first + 1}–{first + shown} of {total}"


def _keys_or_terminal(keys: Iterator[str] | None) -> tuple[Iterator[str], int | None]:
    """The key source: the given iterator, or the terminal (which needs a TTY on both ends)."""
    if keys is not None:
        return keys, None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SJError("a list can only be picked from at a terminal")
    fd = sys.stdin.fileno()
    return _terminal_keys(fd), fd


def _picked[T](fd: int | None, run: Callable[[], T], abort: str) -> T:
    """
    Run a picker's loop with the terminal put back and the frame wiped on the way out.

    Ctrl-C must not leave the frame on screen with the shell prompt landing
    in the middle of it: the frame is wiped and `abort` — the bare prompt
    line — left as the record of where the run stopped, before the
    exception carries on up to main().
    """
    with _cbreak(fd) if fd is not None else nullcontext():
        try:
            return run()
        except BaseException:
            _close_frame(abort)
            raise


def select_filtered[T](
    prompt: str,
    default: T | None,
    search: Callable[[str], list[T]],
    render: Callable[[T], str],
    height: int = 8,
    keys: Iterator[str] | None = None,
) -> T | None:
    """
    Pick an item by typing: every edit calls search(query) and redraws the shortlist.

    ↑/↓ (Tab/Shift-Tab) move the highlight, wrapping; Enter takes the
    highlighted item, or `default` when the query is empty; Backspace edits;
    Esc/Ctrl-D return None. Leaves "? prompt: <rendered choice>" behind.
    """
    source, fd = _keys_or_terminal(keys)
    return _picked(
        fd,
        lambda: _run_filtered(prompt, default, search, render, height, source),
        f"{_prompt_prefix()}{prompt}: ",
    )


def _run_filtered[T](
    prompt: str,
    default: T | None,
    search: Callable[[str], list[T]],
    render: Callable[[T], str],
    height: int,
    keys: Iterator[str],
) -> T | None:
    default_label = render(default) if default is not None else ""
    label = f"{prompt} [{default_label}]: " if default is not None else f"{prompt}: "
    # A frame needs height + 2 rows (prompt, rows, footer); on a short
    # terminal an unclamped height scrolls the prompt off and the cursor
    # arithmetic smears the frame across the screen.
    height = max(1, min(height, _frame_size()[1] - 2))
    query = ""
    results: list[T] = []
    highlight = 0
    first = 0

    def draw() -> None:
        nonlocal first
        columns, _ = _frame_size()
        width = columns - 1
        first = _scroll(first, highlight, height)
        rows = _rows([render(r) for r in results], first, highlight, height, width)
        if not query:
            footer = (
                f"type to search · Enter keeps {default_label}"
                if default is not None
                else "type to search"
            )
        elif not results:
            footer = "no match"
        else:
            footer = (
                f"{_window_text(first, len(rows), len(results))} · ↑↓ move "
                "· Enter picks · Esc aborts"
            )
        rows.append(f"  {style(footer, DIM)}")
        _draw_frame(_prompt_line(f"{label}{query}", width), rows, height)

    draw()
    for key in keys:
        if key == "enter":
            if results:
                chosen = results[highlight]
            elif not query and default is not None:
                chosen = default
            else:
                continue
            _close_frame(f"{_prompt_prefix()}{prompt}: {style(render(chosen), DIM)}")
            return chosen
        if key in ("esc", "eof"):
            _close_frame(f"{_prompt_prefix()}{prompt}: {query}")
            return None
        if key in ("down", "tab"):
            if results:
                highlight = (highlight + 1) % len(results)
        elif key in ("up", "shift-tab"):
            if results:
                highlight = (highlight - 1) % len(results)
        elif key == "backspace":
            if not query:
                continue
            query = query[:-1]
            results, highlight, first = search(query) if query else [], 0, 0
        elif len(key) == 1 and key.isprintable():
            query += key
            results, highlight, first = search(query), 0, 0
        else:
            continue
        draw()
    return None


def select_list[T](
    prompt: str,
    items: list[T],
    render: Callable[[T], str],
    default_index: int = 0,
    reject: Callable[[T], str | None] | None = None,
    height: int | None = None,
    keys: Iterator[str] | None = None,
) -> T | None:
    """
    Pick one of `items` with ↑/↓ (wrapping) or by typing its row number.

    The bracketed number on the prompt line is the row Enter will take.
    reject(item) may return why an item cannot be picked; that text shows in
    the footer and the prompt stays. Esc/Ctrl-D return None; an empty list
    is None at once. Leaves "? prompt: <rendered choice>" behind.
    """
    if not items:
        return None
    source, fd = _keys_or_terminal(keys)
    return _picked(
        fd,
        lambda: _run_list(prompt, items, render, default_index, reject, height, source),
        f"{_prompt_prefix()}{prompt}: ",
    )


def _run_list[T](
    prompt: str,
    items: list[T],
    render: Callable[[T], str],
    default_index: int,
    reject: Callable[[T], str | None] | None,
    height: int | None,
    keys: Iterator[str],
) -> T | None:
    lines = _frame_size()[1]
    rows_text = [render(item) for item in items]
    window = height if height is not None else min(len(items), max(4, lines - 8))
    window = max(1, min(window, lines - 2))  # a frame needs window + 2 rows
    highlight = default_index if 0 <= default_index < len(items) else 0
    first = 0
    typed = ""
    note = ""

    def draw() -> None:
        nonlocal first
        width = _frame_size()[0] - 1
        first = _scroll(first, highlight, window)
        rows = _rows(rows_text, first, highlight, window, width)
        footer = note or (
            f"{_window_text(first, len(rows), len(items))} · ↑↓ move "
            "· digits jump · Enter picks · Esc aborts"
        )
        rows.append(f"  {style(footer, DIM)}")
        _draw_frame(_prompt_line(f"{prompt} [{highlight + 1}]: {typed}", width), rows, window)

    draw()
    for key in keys:
        if key == "enter":
            if typed and not 1 <= int(typed) <= len(items):
                note, typed = f"no row {typed}", ""
                draw()
                continue
            item = items[highlight]
            complaint = reject(item) if reject is not None else None
            if complaint:
                note, typed = complaint, ""
                draw()
                continue
            _close_frame(f"{_prompt_prefix()}{prompt}: {style(render(item), DIM)}")
            return item
        if key in ("esc", "eof"):
            _close_frame(f"{_prompt_prefix()}{prompt} [{highlight + 1}]: {typed}")
            return None
        if key in ("down", "tab"):
            highlight, typed = (highlight + 1) % len(items), ""
        elif key in ("up", "shift-tab"):
            highlight, typed = (highlight - 1) % len(items), ""
        elif key == "backspace":
            typed = typed[:-1]
            if typed and 1 <= int(typed) <= len(items):
                highlight = int(typed) - 1
        elif len(key) == 1 and key in "0123456789" and len(typed) < 3:
            typed += key
            if 1 <= int(typed) <= len(items):
                highlight = int(typed) - 1
        else:
            continue
        note = ""  # only where the screen is about to change: note matches it
        draw()
    return None


def departure_choice_lines(rows: list[dict[str, str]]) -> list[str]:
    """
    Pick-list text per departure row, columns aligned across the rows.

    Row keys: departure, arrival, duration, train, comfort_class and note
    ("fallback" / "no seats", shown dim after the class). No arrow and no
    indent — the picker adds its own marker.
    """
    cols = ("duration", "train", "comfort_class")
    widths = {c: max(visible_len(str(r.get(c) or "—")) for r in rows) for c in cols}
    lines = []
    for row in rows:
        cells = [f"{row.get('departure', '—')} → {row.get('arrival', '—')}"]
        cells += [pad(str(row.get(c) or "—"), widths[c]) for c in cols]
        line = "   ".join(cells)
        if row.get("note"):
            line += f"   {style(str(row['note']), DIM)}"
        lines.append(line.rstrip())
    return lines
