"""
Station lookup for --book-journey: fold, rank and match SJ's station list.

Pure — no HTTP, no printing.
"""

import unicodedata
from typing import Any, TypedDict


class Station(TypedDict):
    """One entry of SJ's station list: display name, UIC code, search synonyms."""

    name: str
    code: str
    synonyms: list[str]


def parse_stations(payload: Any) -> list[Station]:
    """
    Stations from the config/stations response (a list, or {"stations": [...]}).

    Entries without a name or a UIC code are dropped; synonyms default to none.
    """
    items = payload if isinstance(payload, list) else (payload or {}).get("stations") or []
    stations: list[Station] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        code = item.get("uicStationCode")
        if not (name and code):
            continue
        synonyms = [str(s) for s in item.get("synonyms") or [] if s]
        stations.append({"name": str(name), "code": str(code), "synonyms": synonyms})
    return stations


def fold(text: str) -> str:
    """Matching form of a name: casefolded, diacritics stripped, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


_WORD_BREAKS = str.maketrans(dict.fromkeys("-/()", " "))


def _words(folded: str) -> list[str]:
    return folded.translate(_WORD_BREAKS).split()


def _code_order(station: Station) -> int:
    """Sort key: the big stations have the lowest UIC codes (740000001 Stockholm Central)."""
    code = station["code"]
    return int(code) if code.isdigit() else 10**12


def _rank(wanted: str, folded: str, synonyms: list[str]) -> int | None:
    if folded == wanted or wanted in synonyms:
        return 0
    if folded.startswith(wanted):
        return 1
    if any(word.startswith(wanted) for word in _words(folded)):
        return 2
    if wanted in folded:
        return 3
    if any(wanted in s for s in synonyms):
        return 4
    return None


class StationIndex:
    """The station list folded once, ranked per query (see match)."""

    def __init__(self, stations: list[Station]) -> None:
        self._entries = [
            (station, fold(station["name"]), [fold(s) for s in station["synonyms"]])
            for station in stations
        ]

    def exact(self, name: str) -> Station | None:
        """The station whose name or synonym equals `name` (folded); the major one on a tie."""
        wanted = fold(name)
        if not wanted:
            return None
        hits = [s for s, folded, syns in self._entries if folded == wanted or wanted in syns]
        return min(hits, key=_code_order) if hits else None

    def match(self, query: str) -> list[Station]:
        """
        Stations matching `query`, best first; [] for an empty query.

        Rank 0: name or synonym equals the query; 1: the name starts with it;
        2: a word of the name (split on space, -, /, parentheses) starts with
        it; 3: it is inside the name; 4: it is inside a synonym. Ties by UIC
        code, lowest first.
        """
        wanted = fold(query)
        if not wanted:
            return []
        ranked = []
        for station, folded, synonyms in self._entries:
            rank = _rank(wanted, folded, synonyms)
            if rank is not None:
                ranked.append((rank, _code_order(station), station))
        ranked.sort(key=lambda entry: (entry[0], entry[1]))
        return [station for _, _, station in ranked]
