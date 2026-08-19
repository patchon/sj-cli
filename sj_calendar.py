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
"""

from datetime import date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

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
