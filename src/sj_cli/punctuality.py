"""
How late a train actually was: the verdict rules and the sources that answer.

SJ's booking data never records an arrival, so a past leg's punctuality has
to be looked up upstream, live, on every run — nothing is stored between
runs. Five sources answer, first answer wins (``lookup``):

1. SJ's own traffic-info service (passed in as a callable, so this module
   never talks to the SJ client), exact, reaches the travel day and the
   day after
2. Trafikverket's open API, exact, ~4 days, needs a key — unverified
3. the Tågradar worker, exact, ~4 days
4. Tågstatistik's per-train detail, exact, ~4 days
5. Tågstatistik's yearly summary, the train's *final* stop, ~1 year

The first four answer for our own stop; the fifth answers for the train's
final stop, which is our stop only when its planned time matches — otherwise
its figure is an indication, and ``verdict`` words it as one.

Every external call is best effort: any exception or unexpected shape logs
at DEBUG and passes to the next source, so a listing is never failed by a
third party being down. The verdict half is pure.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, NamedTuple

import httpx

from sj_cli.dates import SWEDEN, parse_api_datetime
from sj_cli.logger import log_request, log_response
from sj_cli.stations import fold

logger = logging.getLogger(__name__)

# A source is one of five and the user is watching a listing: wait a good
# deal less on it than on an SJ booking call (the retry policy replays a GET
# up to three times, so this is the per-attempt patience).
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# How far back the exact sources (1-4) still answer. Beyond this the cascade
# goes straight to the yearly summary rather than spending four requests
# learning that nobody remembers the day.
EXACT_REACH_DAYS = 5

# A day on the rails, used to fold a midnight-crossing arrival back into
# range when a source gives times without their date.
_DAY = 24 * 60
_HALF_DAY = _DAY // 2

# The external sources' endpoints. Sources 3-5 are third-party services that
# want to see where the request comes from; neither needs a key.
URL_TRAFIKVERKET = "https://api.trafikinfo.trafikverket.se/v2/data.json"
URL_TAGRADAR = "https://train-detail.etfnordic.workers.dev/train"
URL_TAGSTATISTIK_DETAIL = "https://prod.tydalsystems.se/api/tfor.php"
URL_TAGSTATISTIK_SUMMARY = "https://prod.tydalsystems.se/api/ts_tagperiod.php"
ORIGIN_TAGRADAR = "https://tagradar.nu"
REFERER_TAGSTATISTIK = "https://statistik.xn--tgexperterna-tcb.nu/"

# The same browser identity the SJ client sends. None of these services are
# SJ's, so the string is our own rather than imported from the client.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Source names, as carried on an Arrival and logged.
SOURCE_SJ = "sj"
SOURCE_TRAFIKVERKET = "trafikverket"
SOURCE_TAGRADAR = "tagradar"
SOURCE_TAGSTATISTIK = "tagstatistik"
SOURCE_TAGSTATISTIK_SUMMARY = "tagstatistik-summary"

# What a verdict names as its source: the site the figure came from, in the
# form a user can go and check it at. Both Tågstatistik services are one
# site to the reader, so they share a label.
SOURCE_LABELS = {
    SOURCE_SJ: "sj.se",
    SOURCE_TRAFIKVERKET: "trafikverket.se",
    SOURCE_TAGRADAR: "tagradar.nu",
    SOURCE_TAGSTATISTIK: "tågexperterna.nu",
    SOURCE_TAGSTATISTIK_SUMMARY: "tågexperterna.nu",
    # Tågstatistik lives at statistik.tågexperterna.nu; its backend host (tydalsystems.se)
    # is not a name a reader knows, so the label is the site's apex domain like the others.
}

_TIME = re.compile(r"(?<!\d)([01]\d|2[0-3]):([0-5]\d)(?!\d)")


@dataclass(frozen=True)
class Leg:
    """One booked leg to look up: the train, the day, and where it should have arrived."""

    train: str
    date: str  # YYYY-MM-DD, the leg's Swedish departure day — what every source is keyed by
    planned_arrival: str  # HH:MM, Swedish wall clock
    dep_uic: str
    arr_uic: str
    arr_short: str  # short station name, e.g. "Linköping C" — where a source keys by name
    # The Swedish date the train is due to arrive — the day after `date` for
    # a night train leaving 23:30. `_delay_leg` always fills it in; the None
    # default is for a Leg built by hand, and reads as "the departure day".
    arrival_date: str | None = None

    @property
    def arrival_day(self) -> str:
        """
        The Swedish date of the planned arrival — the departure day unless it crossed midnight.

        Everything about the *arrival* is read against this: which day a
        source's stop must be dated, and which day a bare ``HH:MM`` belongs
        to. The request itself still goes out on ``date``, which is how the
        sources index a run.
        """
        return self.arrival_date or self.date


@dataclass(frozen=True)
class Arrival:
    """
    What a source says about the arrival.

    ``minutes_late`` is actual minus planned, so it may be negative (early);
    it is 0 for a cancelled train, which never arrived at all. ``exact`` is
    false when the figure is the train's final stop rather than our own.
    """

    minutes_late: int
    cancelled: bool
    exact: bool
    source: str
    planned: str
    actual: str | None


class Thresholds(NamedTuple):
    """The minute threshold a verdict is read against ([delays] in the config)."""

    compensation: int = 60


# How the cell should read to the eye: on time, late, worth claiming for, or
# nothing known. The renderer colours by this and never parses the text.
Tone = Literal["good", "late", "claim", "none"]


@dataclass(frozen=True)
class Verdict:
    """The cell shown on a past leg: its text, whether it is worth claiming for, how it reads."""

    text: str
    claim: bool
    tone: Tone = "none"
    source_label: str = ""


def verdict(arrival: Arrival | None, thresholds: Thresholds) -> Verdict:
    """
    The punctuality cell for one leg.

    There is no tolerance to hide behind: a train is on time when it arrived
    on the planned minute, and otherwise the cell shows the signed
    difference — ``+11 min``, and ``-1 min`` for a train that was early,
    which is just as true. From ``compensation`` minutes the cell adds
    ``· claim compensation``. No source answering at all is ``no data``,
    which is not the same as a train that ran on time.

    A final-stop figure (``exact`` false) keeps its hedge: it says which stop
    it speaks for and only asks the user to verify once it is over the
    threshold, a cancellation included — the summary's ``inst`` speaks for
    the train's final stop, not necessarily for our own.

    The cell per condition, exact source / final-stop source:

    * cancelled — ``train cancelled · claim compensation`` /
      ``final stop cancelled · likely, verify``
    * arrived on the minute — ``on time`` / ``final stop on time · likely``
    * early or late — ``-1 min`` / ``+11 min`` /
      ``final stop +20 min · likely``
    * late >= compensation — ``+64 min · claim compensation`` /
      ``final stop +78 min · likely, verify``
    * no source answered — ``no data`` (there is no final-stop form)

    """
    if arrival is None:
        return Verdict("no data", False, "none", "")
    where = SOURCE_LABELS.get(arrival.source, arrival.source)
    if arrival.cancelled:
        if arrival.exact:
            return Verdict("train cancelled · claim compensation", True, "claim", where)
        return Verdict("final stop cancelled · likely, verify", True, "claim", where)
    late = arrival.minutes_late
    claimable = late >= thresholds.compensation
    if arrival.exact:
        if late == 0:
            return Verdict("on time", False, "good", where)
        if claimable:
            return Verdict(f"{late:+d} min · claim compensation", True, "claim", where)
        return Verdict(f"{late:+d} min", False, "late", where)
    if late == 0:
        return Verdict("final stop on time · likely", False, "good", where)
    if claimable:
        return Verdict(f"final stop {late:+d} min · likely, verify", True, "claim", where)
    return Verdict(f"final stop {late:+d} min · likely", False, "late", where)


#
# TIME AND STOP MATCHING
#


def clock(value: Any) -> str | None:
    """
    The ``HH:MM`` in a source's timestamp, whatever shape it came in.

    The sources write the same instant as ``YYYY-MM-DD HH:MM``,
    ``YYYY-MM-DD HH:MM:SS``, a bare ``HH:MM`` or ISO 8601 with an offset;
    the first time-looking token is the wall clock in all four.
    """
    if not isinstance(value, str):
        return None
    match = _TIME.search(value)
    return match.group(0) if match else None


def _as_datetime(value: str, day: str) -> tuple[datetime, bool] | None:
    """
    Parse a source timestamp; a bare time is placed on ``day``.

    Returns the aware datetime and whether the value carried its own date
    (a time-only value may have crossed midnight, which the caller folds).
    """
    text = value.strip()
    dated = True
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", text):
        text = f"{day} {text}"
        dated = False
    try:
        return parse_api_datetime(text), dated
    except ValueError:
        return None


def minutes_between(planned: str, actual: str, day: str) -> int | None:
    """
    Whole minutes from ``planned`` to ``actual``, negative when early.

    Both sides are read at minute resolution — seconds are dropped, never
    rounded — because that is what every official figure does: a train
    planned 19:02 that arrives 20:06:33 is 64 minutes late, the same as
    Tågstatistik reports and the worker's own ``actual`` of 20:06 says.
    Rounding would make 59:31 a claimable 60.

    ``day`` (the leg's travel date) dates a value given as a bare time; when
    either side was undated the difference is folded into ±12 h, so a train
    planned at 23:55 and arriving 00:07 is 12 minutes late, not a day early.
    That fold is also a cap: an undated delay of more than 12 h is not
    representable, and no train we look up is that late.
    """
    left = _as_datetime(planned, day)
    right = _as_datetime(actual, day)
    if left is None or right is None:
        return None
    planned_minute = left[0].replace(second=0, microsecond=0)
    actual_minute = right[0].replace(second=0, microsecond=0)
    minutes = int((actual_minute - planned_minute).total_seconds() // 60)
    if not (left[1] and right[1]):
        # Undated times cannot say which day they are on, so the fold caps
        # such a delay at 12 h by design: past that it reads as early instead.
        minutes = (minutes + _HALF_DAY) % _DAY - _HALF_DAY
    return int(minutes)


def _pick_stop(
    leg: Leg,
    stops: Any,
    planned_of: Callable[[dict[str, Any]], Any],
    name_of: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any] | None:
    """
    Our stop among a source's list: the one planned at the leg's arrival minute.

    Sources that also name the station allow a short-name match as a
    fallback, for the day a timetable change moved the planned minute.
    """
    rows = [s for s in stops or [] if isinstance(s, dict)]
    for stop in rows:
        if clock(planned_of(stop)) == leg.planned_arrival:
            return stop
    if name_of is not None:
        for stop in rows:
            name = name_of(stop)
            if isinstance(name, str) and fold(name) == fold(leg.arr_short):
                return stop
    return None


#
# HTTP PLUMBING
#


def make_http() -> httpx.Client:
    """
    An HTTP client for the external sources, built like the SJ one.

    Same retry policy (GETs replayed on 502/503, timeouts and connection
    errors), but no SJ headers and no auth: these are public third-party
    endpoints. The timeout is tighter than the SJ client's — a listing waits
    on these, and a booking POST's 30 s allowance retried three times would
    stall a whole run on one source being slow.

    The retry transport is imported here rather than at module level so this
    module (and everything that reads a verdict from it) carries no
    dependency on the SJ HTTP client.
    """
    from sj_cli.client import RetryTransport

    return httpx.Client(
        transport=RetryTransport(httpx.HTTPTransport()),
        http2=False,
        follow_redirects=True,
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        event_hooks={"request": [log_request], "response": [log_response]},
    )


def _fetch_json(
    http: httpx.Client,
    method: str,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> Any:
    """Fetch and decode a source's body, or None — a source is never allowed to raise."""
    try:
        response = http.request(method, url, params=params, headers=headers, content=content)
        response.raise_for_status()
        return response.json()
    except Exception as e:  # any failure means this source passes
        logger.debug(f"punctuality: {method} {url} failed: {type(e).__name__}: {e}")
        return None


def _cached(memo: dict[Any, Any] | None, key: tuple[str, ...], fetch: Callable[[], Any]) -> Any:
    """
    One fetch per (source, train, date) in a run — two legs on a train share it.

    A failure caches as None too — including a fetch that raises (the SJ
    client raises by contract): a bad key or a 503 would otherwise cost one
    request per leg instead of one per train and day. The exception still
    propagates, so the caller's own handling is unchanged.
    """
    if memo is None:
        return fetch()
    if key not in memo:
        try:
            memo[key] = fetch()
        except BaseException:
            memo[key] = None
            raise
    return memo[key]


#
# SOURCE 1: SJ TRAFFIC-INFO (the response is fetched by the caller)
#


def sj_arrival(leg: Leg, body: Any) -> Arrival | None:
    """
    Read our stop out of an SJ traffic-info segments response.

    The fetching is the SJ client's job (``get_traffic_segments``); this
    parses what it returned. A segment that says ``missingData``, names a
    reason why traffic information is unavailable, or carries no stations
    tells us nothing, and a stop the train has not ``arrived`` at yet has no
    actual time — both pass to the next source.

    A station's ``name`` is verified live to carry the short form
    ("Linköping C"), which is what the leg's ``arr_short`` holds, so the
    name fallback in ``_pick_stop`` really does catch a moved minute here.
    """
    if not isinstance(body, dict):
        return None
    for segment in body.get("segments") or []:
        if not isinstance(segment, dict) or segment.get("missingData"):
            continue
        if segment.get("trafficInformationUnavailableReason"):
            continue
        stop = _pick_stop(
            leg,
            segment.get("stations"),
            lambda s: (s.get("arrival") or {}).get("originalTime"),
            lambda s: s.get("name"),
        )
        if stop is None:
            continue
        arrival = stop.get("arrival") or {}
        planned = clock(arrival.get("originalTime")) or leg.planned_arrival
        if arrival.get("cancelled") or stop.get("cancelled"):
            return Arrival(0, True, True, SOURCE_SJ, planned, None)
        current = arrival.get("currentTime")
        if not stop.get("arrived") or not isinstance(current, str):
            continue
        minutes = minutes_between(arrival.get("originalTime") or "", current, leg.arrival_day)
        if minutes is None:
            continue
        return Arrival(minutes, False, True, SOURCE_SJ, planned, clock(current))
    return None


#
# SOURCE 2: TRAFIKVERKET
#


def _xml_attr(value: str) -> str:
    """Escape a value for an XML attribute (the query is built, never parsed)."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def trafikverket_query(leg: Leg, key: str) -> str:
    """The TrainAnnouncement query for one leg's arrivals, as Trafikverket's XML."""
    return (
        f'<REQUEST><LOGIN authenticationkey="{_xml_attr(key)}"/>'
        f'<QUERY objecttype="TrainAnnouncement" schemaversion="1.9"><FILTER><AND>'
        f'<EQ name="AdvertisedTrainIdent" value="{_xml_attr(leg.train)}"/>'
        f'<EQ name="ActivityType" value="Ankomst"/>'
        f'<EQ name="ScheduledDepartureDateTime" value="{_xml_attr(leg.date)}"/>'
        f"</AND></FILTER>"
        f"<INCLUDE>AdvertisedTimeAtLocation</INCLUDE><INCLUDE>TimeAtLocation</INCLUDE>"
        f"<INCLUDE>Canceled</INCLUDE><INCLUDE>LocationSignature</INCLUDE>"
        f"</QUERY></REQUEST>"
    )


def trafikverket(
    leg: Leg, http: httpx.Client, key: str, memo: dict[Any, Any] | None = None
) -> Arrival | None:
    """
    Trafikverket's open API: the exact arrival at our stop, about four days back.

    **Unverified.** Nobody has run this against the live service yet — it
    needs a personal API key (``[delays].trafikverket_key``, free from
    data.trafikverket.se) and there is none to test with. The request and
    the response shaping follow the published schema (TrainAnnouncement,
    schemaversion 1.9) and are unit-tested against a canned body; treat the
    first live run as the verification.
    """
    try:
        body = _cached(
            memo,
            (SOURCE_TRAFIKVERKET, leg.train, leg.date),
            lambda: _fetch_json(
                http,
                "POST",
                URL_TRAFIKVERKET,
                headers={"Content-Type": "text/xml"},
                content=trafikverket_query(leg, key).encode("utf-8"),
            ),
        )
        if not isinstance(body, dict):
            return None
        announcements: list[Any] = []
        for result in (body.get("RESPONSE") or {}).get("RESULT") or []:
            if isinstance(result, dict):
                announcements.extend(result.get("TrainAnnouncement") or [])
        stop = _pick_stop(leg, announcements, lambda s: s.get("AdvertisedTimeAtLocation"))
        if stop is None:
            return None
        advertised = stop.get("AdvertisedTimeAtLocation") or ""
        planned = clock(advertised) or leg.planned_arrival
        if stop.get("Canceled"):
            return Arrival(0, True, True, SOURCE_TRAFIKVERKET, planned, None)
        actual = stop.get("TimeAtLocation")
        if not isinstance(actual, str):
            return None
        minutes = minutes_between(advertised, actual, leg.arrival_day)
        if minutes is None:
            return None
        return Arrival(minutes, False, True, SOURCE_TRAFIKVERKET, planned, clock(actual))
    except Exception as e:  # an unexpected shape passes to the next source
        logger.debug(f"punctuality: trafikverket failed for {leg.train}/{leg.date}: {e}")
        return None


#
# SOURCE 3: THE TÅGRADAR WORKER
#


def tagradar_worker(
    leg: Leg, http: httpx.Client, memo: dict[Any, Any] | None = None
) -> Arrival | None:
    """
    The Tågradar worker: the exact arrival at our stop, about four days back.

    Two guards. A train it has no announcements for answers
    ``NO_TRAIN_ANNOUNCEMENTS`` rather than an error, and it has been seen
    serving the *next* day's run for a date it no longer holds — so the
    matched stop's own scheduled date has to be the day the leg was due to
    arrive (``arrival_day``, the next day for a night train) before its
    figure is believed.
    """
    try:
        body = _cached(
            memo,
            (SOURCE_TAGRADAR, leg.train, leg.date),
            lambda: _fetch_json(
                http,
                "GET",
                URL_TAGRADAR,
                params={
                    "nr": leg.train,
                    "date": leg.date,
                    "jdate": leg.date,
                    "view": "advertised",
                },
                headers={"Origin": ORIGIN_TAGRADAR},
            ),
        )
        if not isinstance(body, dict) or body.get("code") == "NO_TRAIN_ANNOUNCEMENTS":
            return None
        stop = _pick_stop(
            leg, body.get("stops"), lambda s: (s.get("arrival") or {}).get("advertised")
        )
        if stop is None:
            return None
        arrival = stop.get("arrival") or {}
        scheduled = arrival.get("scheduledISO")
        if not isinstance(scheduled, str) or _sweden_date(scheduled) != leg.arrival_day:
            logger.debug(f"punctuality: tagradar served another day for {leg.train}/{leg.date}")
            return None
        planned = clock(arrival.get("advertised")) or clock(scheduled) or leg.planned_arrival
        if arrival.get("canceled") or stop.get("canceled"):
            return Arrival(0, True, True, SOURCE_TAGRADAR, planned, None)
        if isinstance(arrival.get("actualISO"), str):
            actual = arrival["actualISO"]
            minutes = minutes_between(scheduled, actual, leg.arrival_day)
        elif isinstance(arrival.get("actual"), str):
            actual = arrival["actual"]
            minutes = minutes_between(arrival.get("advertised") or "", actual, leg.arrival_day)
        else:
            return None
        if minutes is None:
            return None
        return Arrival(minutes, False, True, SOURCE_TAGRADAR, planned, clock(actual))
    except Exception as e:  # an unexpected shape passes to the next source
        logger.debug(f"punctuality: tagradar failed for {leg.train}/{leg.date}: {e}")
        return None


def _sweden_date(value: str) -> str | None:
    """The Swedish calendar date of an ISO timestamp, as YYYY-MM-DD."""
    try:
        return parse_api_datetime(value).astimezone(SWEDEN).date().isoformat()
    except ValueError:
        return None


#
# SOURCES 4 AND 5: TÅGSTATISTIK
#


def tagstatistik_detail(
    leg: Leg, http: httpx.Client, memo: dict[Any, Any] | None = None
) -> Arrival | None:
    """
    Tågstatistik's per-train detail: the exact arrival at our stop, about four days back.

    One row per stop (``tpl`` names it), ``anktid`` planned and ``verkAnk``
    actual. ``statusAnk`` is the deviation in minutes with the **opposite**
    sign to ours: negative means late.
    """
    try:
        body = _cached(
            memo,
            (SOURCE_TAGSTATISTIK, leg.train, leg.date),
            lambda: _fetch_json(
                http,
                "GET",
                URL_TAGSTATISTIK_DETAIL,
                params={"tagNr": leg.train, "datum": leg.date},
                headers={"Referer": REFERER_TAGSTATISTIK},
            ),
        )
        if not isinstance(body, dict) or body.get("error"):
            return None
        stop = _pick_stop(
            leg, body.get("result"), lambda s: s.get("anktid"), lambda s: s.get("tpl")
        )
        if stop is None:
            return None
        actual = stop.get("verkAnk")
        if not isinstance(actual, str) or not actual:
            return None
        planned = clock(stop.get("anktid")) or leg.planned_arrival
        status = stop.get("statusAnk")
        if status is None or status == "":
            minutes = minutes_between(stop.get("anktid") or "", actual, leg.arrival_day)
        else:
            minutes = -int(status)
        if minutes is None:
            return None
        return Arrival(minutes, False, True, SOURCE_TAGSTATISTIK, planned, clock(actual))
    except Exception as e:  # an unexpected shape passes to the next source
        logger.debug(f"punctuality: tagstatistik detail failed for {leg.train}/{leg.date}: {e}")
        return None


def tagstatistik_summary(
    leg: Leg, http: httpx.Client, memo: dict[Any, Any] | None = None
) -> Arrival | None:
    """
    Tågstatistik's yearly summary: the train's final stop, about a year back.

    One request per train number returns every day it ran, so the memo is
    keyed by the train alone. The figure is the *final* stop's, which is our
    stop only when its planned time is the leg's planned arrival — otherwise
    the arrival comes back inexact and the verdict words it as an
    indication. ``ankdiff`` carries the same inverted sign as the detail
    source, and a filled-in ``inst`` means the run was cancelled — no such
    row has been seen live, so it is read for truthiness rather than trusting
    a shape nobody has observed.
    """
    try:
        body = _cached(
            memo,
            (SOURCE_TAGSTATISTIK_SUMMARY, leg.train),
            lambda: _fetch_json(
                http,
                "GET",
                URL_TAGSTATISTIK_SUMMARY,
                params={"tagNr": leg.train, "from": "", "to": ""},
                headers={"Referer": REFERER_TAGSTATISTIK},
            ),
        )
        if not isinstance(body, dict):
            return None
        row = next(
            (
                r
                for r in body.get("data") or []
                if isinstance(r, dict) and r.get("datum") == leg.date
            ),
            None,
        )
        if row is None:
            return None
        # A row whose final stop we cannot read is never our stop: the
        # figure stays an indication rather than passing as exact.
        final_stop = clock(row.get("anktid"))
        exact = final_stop is not None and final_stop == leg.planned_arrival
        planned = final_stop or leg.planned_arrival
        if row.get("inst"):
            return Arrival(0, True, exact, SOURCE_TAGSTATISTIK_SUMMARY, planned, None)
        diff = row.get("ankdiff")
        if diff is None or diff == "":
            return None
        return Arrival(-int(diff), False, exact, SOURCE_TAGSTATISTIK_SUMMARY, planned, None)
    except Exception as e:  # an unexpected shape passes to the next source
        logger.debug(f"punctuality: tagstatistik summary failed for {leg.train}/{leg.date}: {e}")
        return None


#
# THE CASCADE
#


def lookup(
    leg: Leg,
    *,
    sj_segments: Callable[[Leg], dict[str, Any] | None] | None,
    http: httpx.Client,
    trafikverket_key: str | None,
    today: date,
    memo: dict[Any, Any],
) -> Arrival | None:
    """
    Ask each source in turn for the leg's actual arrival; the first answer wins.

    A leg older than ``EXACT_REACH_DAYS`` goes straight to the yearly
    summary: the four exact sources have forgotten it, and asking them would
    cost four requests per leg to learn that. ``sj_segments`` is the SJ
    traffic-info fetch (None skips the source, e.g. with no token at hand)
    and ``trafikverket_key`` skips its source when absent. ``memo`` is
    shared across the whole run, so two legs on one train fetch once.

    Returns None when no source answered — the caller renders that as
    ``no data``, which is not the same thing as ``on time``.
    """
    try:
        leg_date = date.fromisoformat(leg.date)
    except ValueError:
        logger.debug(f"punctuality: leg date '{leg.date}' is not a date")
        return None

    steps: list[tuple[str, Callable[[], Arrival | None]]] = []
    if today - leg_date <= timedelta(days=EXACT_REACH_DAYS):
        if sj_segments is not None:
            fetch_sj = sj_segments
            steps.append(
                (
                    SOURCE_SJ,
                    lambda: sj_arrival(
                        leg,
                        _cached(
                            memo,
                            (SOURCE_SJ, leg.train, leg.date, leg.dep_uic, leg.arr_uic),
                            lambda: fetch_sj(leg),
                        ),
                    ),
                )
            )
        if trafikverket_key:
            key = trafikverket_key
            steps.append((SOURCE_TRAFIKVERKET, lambda: trafikverket(leg, http, key, memo)))
        steps.append((SOURCE_TAGRADAR, lambda: tagradar_worker(leg, http, memo)))
        steps.append((SOURCE_TAGSTATISTIK, lambda: tagstatistik_detail(leg, http, memo)))
    steps.append((SOURCE_TAGSTATISTIK_SUMMARY, lambda: tagstatistik_summary(leg, http, memo)))

    for name, run in steps:
        try:
            arrival = run()
        except Exception as e:  # one source failing is not the run failing
            logger.debug(f"punctuality: {name} failed for {leg.train}/{leg.date}: {e}")
            continue
        if arrival is not None:
            logger.debug(
                f"punctuality: {name} says {leg.train}/{leg.date} arrived "
                f"{arrival.minutes_late} min late (exact={arrival.exact})"
            )
            return arrival
    return None
