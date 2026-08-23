"""
Swedish calendar and date helpers.

- Red days ("röda dagar") and weekends, with no external dependency: every
  Swedish red day is either a fixed date or derived from Easter Sunday
  (anonymous Gregorian algorithm, Meeus/Jones/Butcher).
- Timezone helpers: the SJ API returns timestamps with an explicit offset
  (e.g. 2026-09-01T06:59:00+02:00). Train times and pass validity are
  Swedish wall-clock times, config times are too, and "now" comparisons must
  not depend on the machine's timezone. All API timestamp handling goes
  through parse_api_datetime() / to_sweden() so those rules live in one place.
- The date-selection grammar (parse_date_selection): dates, ISO weeks and
  start..end ranges, shared by the config's dates key and --cancel-date.
"""

import re
from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

from sj_api_client.errors import SJConfigError

SWEDEN = ZoneInfo("Europe/Stockholm")


def parse_api_datetime(value: str) -> datetime:
    """
    Parse an SJ API timestamp into an aware datetime.

    Accepts ISO 8601 with an offset or 'Z'. A naive value (no offset) is
    assumed to be Swedish local time.

    Raises:
        ValueError: If the string is not an ISO 8601 datetime.

    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SWEDEN)
    return dt


def to_sweden(value: str) -> datetime:
    """Parse an SJ API timestamp and express it in Swedish local time (aware)."""
    return parse_api_datetime(value).astimezone(SWEDEN)


def sweden_now() -> datetime:
    """Current time in Sweden (aware)."""
    return datetime.now(SWEDEN)


def easter_sunday(year: int) -> date:
    """Return Easter Sunday for the given year (Gregorian calendar)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _saturday_in_window(year: int, month: int, first_day: int) -> date:
    """Return the Saturday that falls in [first_day, first_day + 6] of the month."""
    d = date(year, month, first_day)
    return d + timedelta(days=(5 - d.weekday()) % 7)


@cache
def swedish_holidays(year: int) -> dict[date, str]:
    """
    Return all Swedish red days for a year, mapped to their Swedish names.

    Includes the official public holidays plus the three eves (Midsommarafton,
    Julafton, Nyårsafton) which are legally treated as Sundays and are de facto
    non-working days.
    """
    easter = easter_sunday(year)
    midsummer_day = _saturday_in_window(year, 6, 20)
    return {
        date(year, 1, 1): "Nyårsdagen",
        date(year, 1, 6): "Trettondedag jul",
        easter - timedelta(days=2): "Långfredagen",
        easter: "Påskdagen",
        easter + timedelta(days=1): "Annandag påsk",
        date(year, 5, 1): "Första maj",
        easter + timedelta(days=39): "Kristi himmelsfärdsdag",
        easter + timedelta(days=49): "Pingstdagen",
        date(year, 6, 6): "Nationaldagen",
        midsummer_day - timedelta(days=1): "Midsommarafton",
        midsummer_day: "Midsommardagen",
        _saturday_in_window(year, 10, 31): "Alla helgons dag",
        date(year, 12, 24): "Julafton",
        date(year, 12, 25): "Juldagen",
        date(year, 12, 26): "Annandag jul",
        date(year, 12, 31): "Nyårsafton",
    }


def skip_reason(d: date, skip_weekends: bool, skip_holidays: bool) -> str | None:
    """
    Return why a date should be skipped, or None if it should be processed.

    Returns "weekend" for Saturday/Sunday when skip_weekends is set, otherwise
    the holiday name when skip_holidays is set and the date is a red day.
    """
    if skip_weekends and d.weekday() >= 5:
        return "weekend"
    if skip_holidays:
        return swedish_holidays(d.year).get(d)
    return None


# --- date selection grammar ---------------------------------------------------
#
# Shared by the config's `dates` key and --cancel-date: a comma-separated list
# of units and inclusive start..end ranges. A unit is a date (2026-09-14) or an
# ISO week (W43 = this ISO year, 2027-W02). The end of a week range may omit
# the year and the W (W43..46, 2027-W02..03); a date range needs two full dates.

_DATE_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}")
_WEEK_SHAPE = re.compile(r"(?:(\d{4})-)?W(\d{1,2})", re.IGNORECASE)
_WEEK_END_SHAPE = re.compile(r"(?:(\d{4})-)?W?(\d{1,2})", re.IGNORECASE)  # year and W optional
_NOT_A_UNIT = "'{}' is not a date (YYYY-MM-DD) or a week (W43, 2027-W02)"


def _anniversary(d: date) -> date:
    """The same calendar date next year (29 Feb → 1 Mar)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return date(d.year + 1, 3, 1)


def _parse_date(token: str, errors: list[str]) -> date | None:
    """A date-shaped token → date, or None with an error appended."""
    try:
        return datetime.strptime(token, "%Y-%m-%d").date()
    except ValueError:
        errors.append(f"'{token}' is not a real calendar date")
        return None


def _week_span(year: int, week: int, errors: list[str]) -> tuple[date, date] | None:
    """Monday and Sunday of an ISO week, or None with an error appended."""
    if not 1 <= week <= 53:
        errors.append(f"week {week} is out of range 1..53")
        return None
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError:
        errors.append(f"{year} has no week {week}")
        return None
    return monday, monday + timedelta(days=6)


def _parse_unit(token: str, errors: list[str], year: int) -> tuple[str, date, date] | None:
    """One unit → (kind, first day, last day); kind is "date" or "week"."""
    if _DATE_SHAPE.fullmatch(token):
        d = _parse_date(token, errors)
        return ("date", d, d) if d else None
    m = _WEEK_SHAPE.fullmatch(token)
    if m:
        span = _week_span(int(m.group(1) or year), int(m.group(2)), errors)
        return ("week", span[0], span[1]) if span else None
    errors.append(_NOT_A_UNIT.format(token))
    return None


def _parse_range(token: str, errors: list[str], year: int) -> tuple[date, date] | None:
    """A start..end range → (first day, last day), or None with errors appended."""
    parts = token.split("..")
    if len(parts) != 2:
        errors.append(f"'{token}' must be a single range start..end")
        return None
    start = _parse_unit(parts[0], errors, year)
    if start is None:
        return None
    kind, first, _ = start
    end_token = parts[1]
    if kind == "week":
        if _DATE_SHAPE.fullmatch(end_token):
            errors.append(f"range '{token}' mixes a date and a week")
            return None
        m = _WEEK_END_SHAPE.fullmatch(end_token)
        if not m:
            errors.append(_NOT_A_UNIT.format(end_token))
            return None
        span = _week_span(int(m.group(1) or first.isocalendar().year), int(m.group(2)), errors)
        if span is None:
            return None
        last = span[1]
    else:
        if _WEEK_SHAPE.fullmatch(end_token):
            errors.append(f"range '{token}' mixes a date and a week")
            return None
        end = _parse_unit(end_token, errors, year)
        if end is None:
            return None
        last = end[2]
    if first > last:
        errors.append(f"range '{token}' must run forwards (start before end)")
        return None
    if last >= _anniversary(first):  # a calendar year at most, leap day included
        errors.append(f"range '{token}' spans more than a year")
        return None
    return first, last


def parse_date_selection(text: str, *, today: date | None = None) -> tuple[list[date], list[str]]:
    """
    Parse a date selection into individual dates.

    Comma-separated units and inclusive start..end ranges, mixed freely:
    dates (2026-09-14), ISO weeks (W43 = week 43 of today's ISO year,
    2027-W02), week ranges whose end may omit year and W (W43..46). A
    week expands to its Monday..Sunday. Duplicates collapse.

    Validate-first contract: every term is checked and ALL problems are
    collected; the dates are only meaningful when errors is empty.

    Args:
        text: The selection as written in the config or on the command line.
        today: The Swedish date that decides the ISO year of a bare week
            (defaults to now).

    Returns:
        (sorted unique dates, errors).

    """
    year = (today or sweden_now().date()).isocalendar().year
    selected: set[date] = set()
    errors: list[str] = []
    for raw in text.split(","):
        token = raw.strip()
        if not token:
            errors.append("empty entry in the date list")
            continue
        if ".." in token:
            span = _parse_range(token, errors, year)
        else:
            unit = _parse_unit(token, errors, year)
            span = (unit[1], unit[2]) if unit else None
        if span:
            first, last = span
            selected.update(first + timedelta(days=i) for i in range((last - first).days + 1))
    if errors:
        return [], errors
    return sorted(selected), []


def normalise_date_selection(text: str) -> str:
    """The selection as written: terms trimmed, `w` upper-cased, joined by ", "."""
    return ", ".join(t.strip().upper() for t in text.split(","))


def selected_dates(params: dict, today: date | None = None) -> list[date]:
    """
    Every date the config's `dates` selection names, sorted.

    The selection was validated at startup; an error here is a programming
    error and raises SJConfigError rather than booking the wrong days.
    """
    dates, errors = parse_date_selection(params["dates"], today=today)
    if errors:
        raise SJConfigError("dates: " + "; ".join(errors), errors=errors)
    return dates


def booking_dates(params: dict, today: date | None = None) -> list[date]:
    """The dates a --book run walks: the selection from today (Swedish date) on."""
    today = today or sweden_now().date()
    return [d for d in selected_dates(params, today) if d >= today]
