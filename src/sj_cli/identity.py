"""The Swedish personal identity number (personnummer): parsing, checking, masking. Pure."""

import re
from datetime import date

from sj_cli.dates import sweden_now

_FORM = re.compile(r"^(\d{6}|\d{8})([-+ ]?)(\d{4})$")


def _luhn_ok(ten_digits: str) -> bool:
    """The Luhn check over the ten-digit form, YYMMDDNNNC."""
    total = 0
    for i, ch in enumerate(ten_digits):
        n = int(ch)
        if i % 2 == 0:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def normalise_personal_number(text: str, today: date | None = None) -> str:
    """
    A personal identity number in the twelve-digit dashed form, `YYYYMMDD-NNNN`.

    Accepts ten or twelve digits with an optional `-`, `+` or space between
    the date and the last four (surrounding whitespace ignored). A
    ten-digit form takes the most recent century that puts the birthday in
    the past; `+` as the separator means a person past a hundred, as on
    Swedish forms. Coordination numbers (day + 60) pass as well. The date
    must be real and not in the future, and the last digit must satisfy
    the Luhn check.

    Raises:
        ValueError: With the reason, worded for the prompt that re-asks.

    """
    today = today or sweden_now().date()
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("empty")
    m = _FORM.match(cleaned)
    if not m:
        raise ValueError("expected 10 or 12 digits, like 19850315-0008")
    date_part, sep, last = m.groups()
    if len(date_part) == 8:
        year, month, day = int(date_part[:4]), int(date_part[4:6]), int(date_part[6:])
    else:
        yy, month, day = int(date_part[:2]), int(date_part[2:4]), int(date_part[4:])
        # The most recent century that keeps the birthday in the past.
        century = today.year - today.year % 100
        year = century + yy
        if (year, month, day % 60 if day > 60 else day) > (today.year, today.month, today.day):
            year -= 100
        if sep == "+":
            year -= 100
    real_day = day - 60 if day > 60 else day
    try:
        born = date(year, month, real_day)
    except ValueError:
        raise ValueError("not a date") from None
    if born > today:
        raise ValueError("the birth date is in the future")
    if not _luhn_ok(f"{year % 100:02d}{month:02d}{day:02d}{last}"):
        raise ValueError("the check digit does not match")
    return f"{year:04d}{month:02d}{day:02d}-{last}"


def mask_personal_number(normalised: str) -> str:
    """The birth date with the last four digits starred, for a summary line."""
    return f"{normalised[:8]}-****"
