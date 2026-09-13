from datetime import date

import httpx
import pytest

from sj_cli.punctuality import (
    Arrival,
    Leg,
    Thresholds,
    clock,
    lookup,
    make_http,
    minutes_between,
    sj_arrival,
    tagradar_worker,
    tagstatistik_detail,
    tagstatistik_summary,
    trafikverket,
    verdict,
)

# An invented route: Brunnsmåla C -> Kvarnhöjden C, train 123.
LEG = Leg(
    train="123",
    date="2026-09-11",
    planned_arrival="09:42",
    dep_uic="740000901",
    arr_uic="740000902",
    arr_short="Kvarnhöjden C",
)
THRESHOLDS = Thresholds()


#
# verdict
#


def v(minutes, *, cancelled=False, exact=True):
    return verdict(Arrival(minutes, cancelled, exact, "t", "09:42", None), THRESHOLDS)


def test_verdict_no_data_when_nobody_answered():
    assert verdict(None, THRESHOLDS).text == "no data"
    assert verdict(None, THRESHOLDS).claim is False


def test_verdict_exact_rows():
    assert v(0).text == "on time"
    assert v(-7).text == "on time"  # early counts as on time
    assert v(5).text == "on time"  # the on_time threshold is inclusive
    assert v(6).text == "6 min late"
    assert v(12) == verdict(Arrival(12, False, True, "t", "09:42", None), THRESHOLDS)
    assert v(12).text == "12 min late"
    assert v(12).claim is False
    assert v(59).text == "59 min late"
    assert v(59).claim is False
    assert v(60).text == "60 min late · claim compensation"  # inclusive
    assert v(60).claim is True
    assert v(64).text == "64 min late · claim compensation"


def test_verdict_final_stop_rows():
    assert v(0, exact=False).text == "final stop on time · likely"
    assert v(-3, exact=False).text == "final stop on time · likely"
    assert v(5, exact=False).text == "final stop on time · likely"
    assert v(6, exact=False).text == "final stop 6 min late · likely"
    assert v(12, exact=False).text == "final stop 12 min late · likely"
    assert v(12, exact=False).claim is False
    assert v(59, exact=False).claim is False
    assert v(60, exact=False).text == "final stop 60 min late · likely, verify"
    assert v(78, exact=False).text == "final stop 78 min late · likely, verify"
    assert v(78, exact=False).claim is True


def test_verdict_cancelled_wins_over_exactness_and_minutes():
    for exact in (True, False):
        cell = v(0, cancelled=True, exact=exact)
        assert cell.text == "train cancelled · claim compensation"
        assert cell.claim is True


def test_verdict_reads_the_configured_thresholds():
    lenient = Thresholds(on_time=15, compensation=120)
    assert verdict(Arrival(12, False, True, "t", "09:42", None), lenient).text == "on time"
    assert verdict(Arrival(64, False, True, "t", "09:42", None), lenient).text == "64 min late"
    assert verdict(Arrival(120, False, True, "t", "09:42", None), lenient).claim is True


#
# time helpers
#


def test_clock_reads_every_shape_the_sources_send():
    assert clock("2026-09-11 09:42") == "09:42"
    assert clock("2026-09-11 09:42:00") == "09:42"
    assert clock("09:42") == "09:42"
    assert clock("2026-09-11T09:42:00.000+02:00") == "09:42"
    assert clock(None) is None
    assert clock("") is None
    assert clock("2026-09-11") is None


def test_minutes_between_signs_and_midnight():
    assert minutes_between("2026-09-11 09:42", "2026-09-11 09:54", "2026-09-11") == 12
    assert minutes_between("09:42", "09:35", "2026-09-11") == -7  # early
    assert minutes_between("23:55", "00:07", "2026-09-11") == 12  # over midnight
    assert minutes_between("2026-09-11T09:42:00+02:00", "2026-09-11T10:46:00+02:00", "x") == 64
    assert minutes_between("nonsense", "09:54", "2026-09-11") is None


#
# the mock transports
#


def transport(routes):
    """routes: {path: body or (status, body) or callable(request) -> body}; records requests."""
    seen = []

    def handler(request):
        seen.append(request)
        route = routes.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"missing": request.url.path}, request=request)
        if callable(route):
            route = route(request)
        status, body = route if isinstance(route, tuple) else (200, route)
        return httpx.Response(status, json=body, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, seen


WORKER = "/train"
DETAIL = "/api/tfor.php"
SUMMARY = "/api/ts_tagperiod.php"
TRV = "/v2/data.json"


def worker_body(**over):
    arrival = {
        "advertised": "09:42",
        "scheduledISO": "2026-09-11T09:42:00+02:00",
        "actual": "09:54",
        "actualISO": "2026-09-11T09:54:00+02:00",
        "estimated": None,
        "canceled": False,
        "passed": True,
    }
    arrival.update(over)
    return {
        "stops": [
            {
                "locationSignature": "Bma",
                "arrival": {"advertised": "07:15", "scheduledISO": "2026-09-11T07:15:00+02:00"},
                "canceled": False,
            },
            {"locationSignature": "Kvh", "arrival": arrival, "canceled": False},
        ]
    }


def detail_body(**over):
    row = {
        "tpl": "Kvarnhöjden C",
        "avgtid": None,
        "verkAvg": None,
        "statusAvg": None,
        "anktid": "09:42",
        "verkAnk": "09:54",
        "statusAnk": "-12",
    }
    row.update(over)
    return {
        "traindata": {"tagNr": "123"},
        "result": [
            {"tpl": "Brunnsmåla C", "anktid": None, "verkAnk": None, "statusAnk": None},
            row,
        ],
    }


def summary_body(**over):
    row = {
        "datum": "2026-09-11",
        "avgtid": "2026-09-11 07:15:00",
        "anktid": "2026-09-11 09:42:00",
        "avgdiff": "0",
        "ankdiff": "-12",
        "inst": None,
    }
    row.update(over)
    return {"from": "", "to": "", "data": [{"datum": "2026-09-10", "ankdiff": "0"}, row]}


#
# source 1: SJ traffic-info (parsing only — the fetch belongs to the client)
#


def sj_body(**over):
    station = {
        "name": "Kvarnhöjden C",
        "locationCode": "740000902",
        "arrived": True,
        "departed": False,
        "cancelled": False,
        "arrival": {
            "originalTime": "2026-09-11 09:42",
            "currentTime": "2026-09-11 09:54",
            "cancelled": False,
            "delayed": True,
        },
    }
    station.update(over)
    return {
        "segments": [
            {
                "missingData": False,
                "trafficInformationUnavailableReason": None,
                "stations": [
                    {
                        "name": "Brunnsmåla C",
                        "arrived": True,
                        "arrival": {"originalTime": "2026-09-11 07:15"},
                    },
                    station,
                ],
            }
        ]
    }


def test_sj_arrival_happy_path():
    got = sj_arrival(LEG, sj_body())
    assert got == Arrival(12, False, True, "sj", "09:42", "09:54")


def test_sj_arrival_matches_by_short_name_when_the_minute_moved():
    body = sj_body(arrival={"originalTime": "2026-09-11 09:41", "currentTime": "2026-09-11 09:53"})
    assert sj_arrival(LEG, body).minutes_late == 12


def test_sj_arrival_passes_on_missing_data_and_null_stations():
    assert sj_arrival(LEG, {"segments": [{"missingData": True, "stations": None}]}) is None
    assert sj_arrival(LEG, {"segments": [{"stations": None}]}) is None
    assert (
        sj_arrival(
            LEG,
            {"segments": [{"trafficInformationUnavailableReason": "NO_DATA", "stations": []}]},
        )
        is None
    )
    assert sj_arrival(LEG, {"segments": []}) is None
    assert sj_arrival(LEG, None) is None


def test_sj_arrival_passes_when_the_train_has_not_arrived_yet():
    assert sj_arrival(LEG, sj_body(arrived=False)) is None
    body = sj_body(arrival={"originalTime": "2026-09-11 09:42", "currentTime": None})
    assert sj_arrival(LEG, body) is None


def test_sj_arrival_reports_a_cancelled_stop():
    body = sj_body(
        arrival={"originalTime": "2026-09-11 09:42", "currentTime": None, "cancelled": True}
    )
    got = sj_arrival(LEG, body)
    assert got.cancelled is True
    assert got.minutes_late == 0
    assert got.exact is True


#
# source 2: trafikverket (unverified against the live service)
#


def trv_body(**over):
    announcement = {
        "AdvertisedTimeAtLocation": "2026-09-11T09:42:00.000+02:00",
        "TimeAtLocation": "2026-09-11T09:54:00.000+02:00",
        "Canceled": False,
        "LocationSignature": "Kvh",
    }
    announcement.update(over)
    return {
        "RESPONSE": {
            "RESULT": [
                {
                    "TrainAnnouncement": [
                        {
                            "AdvertisedTimeAtLocation": "2026-09-11T07:15:00.000+02:00",
                            "TimeAtLocation": "2026-09-11T07:15:00.000+02:00",
                            "LocationSignature": "Bma",
                        },
                        announcement,
                    ]
                }
            ]
        }
    }


def test_trafikverket_happy_path_and_request_shape():
    http, seen = transport({TRV: trv_body()})
    got = trafikverket(LEG, http, "KEY-123")
    assert got == Arrival(12, False, True, "trafikverket", "09:42", "09:54")
    request = seen[0]
    assert request.method == "POST"
    assert request.headers["content-type"] == "text/xml"
    body = request.content.decode()
    assert '<LOGIN authenticationkey="KEY-123"/>' in body
    assert 'objecttype="TrainAnnouncement" schemaversion="1.9"' in body
    assert '<EQ name="AdvertisedTrainIdent" value="123"/>' in body
    assert '<EQ name="ActivityType" value="Ankomst"/>' in body
    assert '<EQ name="ScheduledDepartureDateTime" value="2026-09-11"/>' in body
    assert "<INCLUDE>TimeAtLocation</INCLUDE>" in body


def test_trafikverket_passes_without_an_actual_time_and_reports_cancelled():
    http, _ = transport({TRV: trv_body(TimeAtLocation=None)})
    assert trafikverket(LEG, http, "K") is None
    http, _ = transport({TRV: trv_body(TimeAtLocation=None, Canceled=True)})
    assert trafikverket(LEG, http, "K").cancelled is True
    http, _ = transport({TRV: {"RESPONSE": {"RESULT": []}}})
    assert trafikverket(LEG, http, "K") is None
    http, _ = transport({TRV: (500, {"nope": 1})})
    assert trafikverket(LEG, http, "K") is None


#
# source 3: the tagradar worker
#


def test_tagradar_happy_path_and_request_shape():
    http, seen = transport({WORKER: worker_body()})
    got = tagradar_worker(LEG, http)
    assert got == Arrival(12, False, True, "tagradar", "09:42", "09:54")
    request = seen[0]
    assert dict(request.url.params) == {
        "nr": "123",
        "date": "2026-09-11",
        "jdate": "2026-09-11",
        "view": "advertised",
    }
    assert request.headers["origin"] == "https://tagradar.nu"


def test_tagradar_passes_on_no_announcements():
    http, _ = transport({WORKER: {"code": "NO_TRAIN_ANNOUNCEMENTS", "message": "none"}})
    assert tagradar_worker(LEG, http) is None


def test_tagradar_refuses_another_days_data():
    # the worker has been seen serving the next day's run for a date it no
    # longer holds: the stop's own scheduled date has to be the leg's date
    http, _ = transport({WORKER: worker_body(scheduledISO="2026-09-12T09:42:00+02:00")})
    assert tagradar_worker(LEG, http) is None


def test_tagradar_passes_without_an_actual_and_reports_cancelled():
    http, _ = transport({WORKER: worker_body(actual=None, actualISO=None)})
    assert tagradar_worker(LEG, http) is None
    http, _ = transport({WORKER: worker_body(actual=None, actualISO=None, canceled=True)})
    got = tagradar_worker(LEG, http)
    assert (got.cancelled, got.minutes_late, got.exact) == (True, 0, True)
    http, _ = transport({WORKER: {"stops": []}})
    assert tagradar_worker(LEG, http) is None


def test_tagradar_falls_back_to_the_wall_clock_pair():
    http, _ = transport({WORKER: worker_body(actualISO=None)})
    assert tagradar_worker(LEG, http).minutes_late == 12


#
# source 4: tagstatistik detail
#


def test_tagstatistik_detail_happy_path_and_sign_convention():
    http, seen = transport({DETAIL: detail_body()})
    got = tagstatistik_detail(LEG, http)
    # statusAnk is minutes with the opposite sign: -12 means 12 late
    assert got == Arrival(12, False, True, "tagstatistik", "09:42", "09:54")
    assert dict(seen[0].url.params) == {"tagNr": "123", "datum": "2026-09-11"}
    assert seen[0].headers["referer"] == "https://statistik.xn--tgexperterna-tcb.nu/"


def test_tagstatistik_detail_early_train_is_a_positive_status():
    http, _ = transport({DETAIL: detail_body(statusAnk="3", verkAnk="09:39")})
    assert tagstatistik_detail(LEG, http).minutes_late == -3


def test_tagstatistik_detail_matches_by_tpl_when_the_minute_moved():
    http, _ = transport({DETAIL: detail_body(anktid="09:41")})
    assert tagstatistik_detail(LEG, http).minutes_late == 12


def test_tagstatistik_detail_pass_conditions():
    http, _ = transport({DETAIL: {"result": None}})
    assert tagstatistik_detail(LEG, http) is None
    http, _ = transport({DETAIL: {"error": 1}})
    assert tagstatistik_detail(LEG, http) is None
    http, _ = transport({DETAIL: detail_body(verkAnk=None)})
    assert tagstatistik_detail(LEG, http) is None


#
# source 5: tagstatistik summary
#


def test_tagstatistik_summary_is_exact_only_when_the_final_stop_is_ours():
    http, seen = transport({SUMMARY: summary_body()})
    got = tagstatistik_summary(LEG, http)
    assert got == Arrival(12, False, True, "tagstatistik-summary", "09:42", None)
    assert dict(seen[0].url.params) == {"tagNr": "123", "from": "", "to": ""}

    http, _ = transport({SUMMARY: summary_body(anktid="2026-09-11 11:08:00")})
    got = tagstatistik_summary(LEG, http)
    assert got.exact is False
    assert got.minutes_late == 12
    assert verdict(got, THRESHOLDS).text == "final stop 12 min late · likely"


def test_tagstatistik_summary_is_never_exact_without_a_readable_final_stop():
    # an unreadable anktid cannot be our stop, so the figure stays an
    # indication rather than printing with the exact wording
    for anktid in (None, "", "n/a"):
        http, _ = transport({SUMMARY: summary_body(anktid=anktid)})
        got = tagstatistik_summary(LEG, http)
        assert got.exact is False
        assert got.planned == LEG.planned_arrival
        assert verdict(got, THRESHOLDS).text == "final stop 12 min late · likely"


def test_tagstatistik_summary_only_a_filled_in_inst_means_cancelled():
    # no non-null inst has ever been seen live, so its shape is unknown: a
    # falsy value is read as "not cancelled", not as a cancellation
    for inst in (0, "", None):
        http, _ = transport({SUMMARY: summary_body(inst=inst)})
        assert tagstatistik_summary(LEG, http).cancelled is False


def test_tagstatistik_summary_cancelled_and_missing_rows():
    http, _ = transport({SUMMARY: summary_body(inst="X")})
    got = tagstatistik_summary(LEG, http)
    assert (got.cancelled, got.minutes_late) == (True, 0)
    http, _ = transport({SUMMARY: {"data": [{"datum": "2026-09-10", "ankdiff": "0"}]}})
    assert tagstatistik_summary(LEG, http) is None
    http, _ = transport({SUMMARY: summary_body(ankdiff=None)})
    assert tagstatistik_summary(LEG, http) is None


def test_tagstatistik_summary_is_memoised_per_train():
    memo = {}
    http, seen = transport({SUMMARY: summary_body()})
    other = Leg("123", "2026-09-11", "09:42", "740000901", "740000902", "Kvarnhöjden C")
    assert tagstatistik_summary(LEG, http, memo).minutes_late == 12
    assert tagstatistik_summary(other, http, memo).minutes_late == 12
    assert len(seen) == 1  # one call for the whole year, shared by both legs


#
# the client the external sources share
#


def test_make_http_is_retried_patient_and_anonymous():
    from sj_cli.client import RetryTransport

    client = make_http()
    try:
        # tighter than the SJ client's 30 s: a listing waits on these, and the
        # retry policy replays a GET three times over
        assert client.timeout.read == 15.0
        assert client.timeout.connect == 5.0
        assert isinstance(client._transport, RetryTransport)
        assert "Mozilla/5.0" in client.headers["user-agent"]
        assert "authorization" not in client.headers  # public third parties
    finally:
        client.close()


#
# the cascade
#


TODAY = date(2026, 9, 13)


def paths(seen):
    return [r.url.path for r in seen]


def cascade(routes, *, sj=None, key=None, today=TODAY, memo=None, leg=LEG):
    http, seen = transport(routes)
    got = lookup(
        leg,
        sj_segments=sj,
        http=http,
        trafikverket_key=key,
        today=today,
        memo={} if memo is None else memo,
    )
    return got, seen


def test_cascade_stops_at_the_first_answer():
    got, seen = cascade({}, sj=lambda _leg: sj_body())
    assert got.source == "sj"
    assert paths(seen) == []  # nothing external was asked


def test_cascade_order_when_every_source_passes():
    empty = {WORKER: {"stops": []}, DETAIL: {"result": None}, SUMMARY: {"data": []}, TRV: {}}
    got, seen = cascade(empty, sj=lambda _leg: {"segments": [{"missingData": True}]}, key="K")
    assert got is None
    assert paths(seen) == [TRV, WORKER, DETAIL, SUMMARY]


def test_cascade_skips_the_sources_it_has_no_way_into():
    empty = {WORKER: {"stops": []}, DETAIL: {"result": None}, SUMMARY: {"data": []}}
    got, seen = cascade(empty, sj=None, key=None)
    assert got is None
    assert paths(seen) == [WORKER, DETAIL, SUMMARY]  # no SJ call, no trafikverket call


def test_cascade_falls_through_to_each_next_source():
    routes = {WORKER: {"code": "NO_TRAIN_ANNOUNCEMENTS"}, DETAIL: detail_body()}
    got, seen = cascade(routes, sj=lambda _leg: None)
    assert got.source == "tagstatistik"
    assert paths(seen) == [WORKER, DETAIL]


def test_cascade_caches_a_raising_source_across_two_legs():
    # SJClient.get_traffic_segments raises by contract (a bad key, a 503):
    # without caching the failure, a bad day would cost one POST per leg
    memo = {}
    calls = []

    def sj(leg):
        calls.append(leg.planned_arrival)
        raise httpx.ConnectError("traffic info is unreachable")

    empty = {WORKER: {"stops": []}, DETAIL: {"result": None}, SUMMARY: {"data": []}}
    http, _seen = transport(empty)
    other = Leg("123", "2026-09-11", "07:15", "740000901", "740000902", "Brunnsmåla C")
    for leg in (LEG, other):
        assert (
            lookup(leg, sj_segments=sj, http=http, trafikverket_key=None, today=TODAY, memo=memo)
            is None
        )
    assert calls == ["09:42"]  # asked once for the train and day, not once per leg


def test_cascade_skips_a_source_that_raises():
    def boom(_leg):
        raise RuntimeError("traffic info is down")

    got, seen = cascade({WORKER: worker_body()}, sj=boom)
    assert got.source == "tagradar"
    assert paths(seen) == [WORKER]


def test_cascade_goes_straight_to_the_summary_for_an_old_leg():
    routes = {WORKER: worker_body(), DETAIL: detail_body(), SUMMARY: summary_body()}
    got, seen = cascade(routes, sj=lambda _leg: sj_body(), key="K", today=date(2026, 9, 17))
    assert got.source == "tagstatistik-summary"
    assert paths(seen) == [SUMMARY]

    # five days back is still within reach of the exact sources
    _, seen = cascade(routes, sj=lambda _leg: None, today=date(2026, 9, 16))
    assert paths(seen) == [WORKER]


def test_cascade_memoises_a_response_across_two_legs_on_one_train():
    memo = {}
    routes = {WORKER: worker_body(), SUMMARY: summary_body()}
    calls = []

    def sj(leg):
        calls.append(leg.planned_arrival)
        return {"segments": [{"missingData": True}]}

    http, seen = transport(routes)
    other = Leg("123", "2026-09-11", "07:15", "740000901", "740000902", "Brunnsmåla C")
    for leg in (LEG, other):
        lookup(leg, sj_segments=sj, http=http, trafikverket_key=None, today=TODAY, memo=memo)
    assert calls == ["09:42"]  # the second leg read the memoised SJ response
    assert paths(seen).count(WORKER) == 1  # and the memoised worker response


def test_cascade_survives_a_leg_date_that_is_not_a_date():
    broken = Leg("123", "not-a-date", "09:42", "1", "2", "Kvarnhöjden C")
    got, seen = cascade({SUMMARY: summary_body()}, leg=broken)
    assert got is None
    assert paths(seen) == []  # no source can be asked about a day we cannot name


@pytest.mark.parametrize("status", [500, 503, 404])
def test_cascade_survives_every_source_erroring(status):
    routes = {p: (status, {"boom": 1}) for p in (TRV, WORKER, DETAIL, SUMMARY)}
    got, _ = cascade(routes, sj=lambda _leg: None, key="K")
    assert got is None
    assert verdict(got, THRESHOLDS).text == "no data"
