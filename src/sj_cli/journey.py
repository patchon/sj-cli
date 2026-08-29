"""The interactive journey mode (--book-journey): questions, pick lists, then the Cart."""

import logging
from datetime import date

from sj_cli.client import SJClient
from sj_cli.output import ask_optional, pwarn, select_filtered
from sj_cli.stations import Station, StationIndex

logger = logging.getLogger(__name__)


# --- the questions ---------------------------------------------------------------


def _ask_date(
    label: str,
    default: date,
    earliest: date,
    too_early: str,
    valid: tuple[date | None, date | None],
) -> date | None:
    """
    Ask for a date; Enter keeps `default`, Ctrl-D returns None.

    Re-asks until the answer is a YYYY-MM-DD date not before `earliest`
    (`too_early` is the complaint) and inside the pass validity `valid`
    (an unknown bound is not checked).
    """
    first, last = valid
    while True:
        answer = ask_optional(f"{label} [{default.isoformat()}]: ")
        if answer is None:
            return None
        answer = answer.strip()
        if not answer:
            chosen = default
        else:
            try:
                chosen = date.fromisoformat(answer)
            except ValueError:
                pwarn("not a date, use YYYY-MM-DD")
                continue
        if chosen < earliest:
            pwarn(too_early)
            continue
        if (first is not None and chosen < first) or (last is not None and chosen > last):
            pwarn(f"the pass is valid {first or '?'} – {last or '?'}")
            continue
        return chosen


def _ask_station(
    label: str, default: Station | None, index: StationIndex, other: Station | None
) -> Station | None:
    """Pick a station by typing; Enter keeps `default`; None on Esc/Ctrl-D. Refuses `other`."""
    while True:
        chosen = select_filtered(label, default, index.match, lambda s: s["name"])
        if chosen is None:
            return None
        if other is not None and chosen["code"] == other["code"]:
            pwarn("from and to are the same station")
            continue
        return chosen


def _ask_yes_no(question: str, default: bool) -> bool | None:
    """A [Y/n]/[y/N] question: Enter is the default, Ctrl-D is None."""
    answer = ask_optional(f"{question} [{'Y/n' if default else 'y/N'}]: ")
    if answer is None:
        return None
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _default_station(index: StationIndex, client: SJClient, name: str) -> Station:
    """The config station as a live-list entry, or a client-resolved stand-in when it's missing."""
    station = index.exact(name)
    if station is not None:
        return station
    return {"name": name, "code": client.resolve_station(name), "synonyms": []}
