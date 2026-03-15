"""User-facing output helpers for the SJ API client."""

import logging
import sys
from datetime import datetime

logger = logging.getLogger(__name__)


def pinfo(msg: str) -> None:
    """Print a status message to stdout."""
    print(msg.lower(), file=sys.stdout)


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
                max_width = max(max_width, len(row[i]))
        col_widths.append(max_width + 2)

    total_width = sum(col_widths)
    separator = "\u2500" * total_width

    lines = []
    if title:
        lines.append(title.lower())

    lines.append(separator)
    header_line = "".join(
        h.lower().ljust(w) for h, w in zip(headers, col_widths, strict=False)
    )
    lines.append(header_line)
    lines.append(separator)

    for row in rows:
        cells = []
        for i, w in enumerate(col_widths):
            cell = row[i] if i < len(row) else "\u2014"
            cells.append(cell.lower().ljust(w))
        lines.append("".join(cells))

    lines.append(separator)
    return "\n".join(lines)


def print_dry_run_table(results: list[dict]) -> None:
    """
    Print dry-run results as a formatted table.

    Args:
        results: List of dicts with keys: date, direction, departure,
                 arrival, comfort_class, flexibility, note.

    """
    headers = ["Date", "Direction", "Departure", "Arrival", "Class", "Flexibility"]
    rows = []
    for r in results:
        note = r.get("note", "")
        if note:
            rows.append([
                r.get("date", "\u2014"),
                r.get("direction", "\u2014"),
                "\u2014",
                "\u2014",
                "\u2014",
                f"\u2014 ({note})",
            ])
        else:
            rows.append([
                r.get("date", "\u2014"),
                r.get("direction", "\u2014"),
                r.get("departure", "\u2014"),
                r.get("arrival", "\u2014"),
                r.get("comfort_class", "\u2014"),
                r.get("flexibility", "\u2014"),
            ])

    table = format_table(headers, rows, title="Dry Run Results")
    print(f"\n{table}", file=sys.stdout)


def print_bookings_table(bookings: list[dict], pass_name: str) -> None:
    """
    Print current bookings as a formatted table.

    Args:
        bookings: List of dicts with keys: date, direction, departure,
                  arrival, duration, comfort_class, route.
        pass_name: Name of the travel pass for the title.

    """
    has_past = any("past" in b for b in bookings)
    headers = ["Date", "Direction", "Departure", "Arrival", "Duration", "Class", "Route", "Booking"]
    if has_past:
        headers.append("Past")
    rows = []
    for b in bookings:
        row = [
            b.get("date", "\u2014"),
            b.get("direction", "\u2014"),
            b.get("departure", "\u2014"),
            b.get("arrival", "\u2014"),
            b.get("duration", "\u2014"),
            b.get("comfort_class", "\u2014"),
            b.get("route", "\u2014"),
            b.get("booking_number", "\u2014"),
        ]
        if has_past:
            row.append(b.get("past", "\u2014"))
        rows.append(row)

    title = f"Current Bookings ({pass_name})"
    table = format_table(headers, rows, title=title)
    print(f"\n{table}")
    pinfo(f"{len(bookings)} booking(s) shown")


def _format_tp_date(iso_str: str | None) -> str:
    """Format an ISO datetime string to YYYY-MM-DD."""
    if not iso_str:
        return "\u2014"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return iso_str[:10] if iso_str else "\u2014"


def _days_remaining(end_iso: str | None) -> str:
    """Calculate days remaining from now until end date."""
    if not end_iso:
        return "\u2014"
    try:
        end_dt = datetime.fromisoformat(
            end_iso.replace("Z", "+00:00")
        ).replace(tzinfo=None)
        delta = end_dt - datetime.now()
        days = delta.days
        if days < 0:
            return "expired"
        return str(days)
    except (ValueError, AttributeError):
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
        valid_to = _format_tp_date(tp.get("endTravelValidityDateTime"))
        days_left = _days_remaining(tp.get("endTravelValidityDateTime"))

        # Try to get price from receipt info
        price = "\u2014"
        if receipt_info:
            booking_id = tp.get("travelPassCreationBookingId", "")
            receipt = receipt_info.get(booking_id)
            if receipt:
                price = _extract_price(receipt)

        rows.append([name, card_number, holder, valid_from, valid_to, days_left, price])

    table = format_table(headers, rows, title="Travel Passes")
    print(f"\n{table}")
    pinfo(f"{len(travel_passes)} travel pass(es) shown")


def _format_amount(raw_amount: str | int | float, currency: str = "SEK") -> str:
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
