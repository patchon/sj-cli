"""The booking core: search parsing, polling, departure facts, offers and the Cart."""

from sj_cli.booking import describe_departure, poll_departures, search
from tests.fakes import FakeClient, dep

D = "2026-09-01"


# --- search() -----------------------------------------------------------------


def test_search_roundtrip_returns_both_ids_and_the_passenger_token():
    c = FakeClient()
    found = search(
        c,
        "tok",
        "Göteborg Central",
        "Stockholm Central",
        D,
        D,
        tp_product_id="TP",
        tp_token_id="TOK",
        service_types=None,
    )
    assert found == {"out_id": "OUT", "in_id": "IN", "passenger_token": "PT"}
    assert c.calls == [("search", "Göteborg Central", "Stockholm Central", D, D)]
    assert c.search_tp_ids == ["TP"]


def test_search_one_way_has_no_return_id():
    c = FakeClient()
    found = search(
        c,
        "tok",
        "Stockholm Central",
        "Göteborg Central",
        D,
        None,
        tp_product_id="TP",
        tp_token_id="TOK",
        service_types=None,
    )
    # "IN" is a FakeClient artefact (it names the search id by direction); what
    # matters here is that return_date=None reaches the client and no in_id comes back.
    assert found == {"out_id": "IN", "in_id": None, "passenger_token": "PT"}
    assert c.calls == [("search", "Stockholm Central", "Göteborg Central", D, None)]


def test_search_falls_back_to_the_pass_token_and_drops_the_all_filter():
    class Bare(FakeClient):
        def search_journey(
            self, token, origin, dest, date, return_date=None, tp_id=None, service_types=None
        ):
            self.calls.append(("search", service_types))
            return {"departureSearchId": "S"}

    c = Bare()
    found = search(
        c, "tok", "A", "B", D, None, tp_product_id="TP", tp_token_id="TOK", service_types=["ALL"]
    )
    assert found == {"out_id": "S", "in_id": None, "passenger_token": "TOK"}
    assert c.calls == [("search", None)]


def test_search_passes_a_real_service_filter_and_a_missing_pass_through():
    class Bare(FakeClient):
        def search_journey(
            self, token, origin, dest, date, return_date=None, tp_id=None, service_types=None
        ):
            self.calls.append(("search", tp_id, service_types))
            return {}

    c = Bare()
    found = search(
        c, "tok", "A", "B", D, None, tp_product_id=None, tp_token_id="", service_types=["SJ_HIGH"]
    )
    assert found == {"out_id": None, "in_id": None, "passenger_token": ""}
    assert c.calls == [("search", None, ["SJ_HIGH"])]


# --- poll_departures() --------------------------------------------------------


def test_poll_departures_retries_until_departures_appear():
    class Late(FakeClient):
        def __init__(self):
            super().__init__()
            self.tries = 0

        def get_search_results(self, token, search_id):
            self.tries += 1
            self.calls.append(("results", search_id))
            found = [dep("d", D, "06:00", "07:00")] if self.tries == 3 else []
            return {"travels": [{"departures": found}]}

    c = Late()
    assert [d["departureId"] for d in poll_departures(c, "tok", "S")] == ["d"]
    assert c.calls == [("results", "S")] * 3


def test_poll_departures_gives_up_after_five_tries():
    c = FakeClient({"S": []})
    assert poll_departures(c, "tok", "S") == []
    assert c.calls == [("results", "S")] * 5


# --- describe_departure() -----------------------------------------------------


def test_describe_departure_reads_times_duration_and_train():
    d = dep("o-best", D, "06:59", "11:36")
    assert describe_departure(d, "A → B") == {
        "departure": "06:59",
        "arrival": "11:36",
        "duration": "4h 37m",
        "train": "X 2000 O-BEST",
        "route": "A → B",
    }


def test_describe_departure_reads_an_api_null_time_and_the_service_name_fallback():
    facts = describe_departure(
        {"departureDateTime": None, "legs": [{"serviceName": "520"}]}, "A → B"
    )
    assert facts["departure"] == "—"
    assert facts["train"] == "520"


def test_describe_departure_survives_an_empty_departure():
    assert describe_departure({}, "A → B") == {
        "departure": "—",
        "arrival": "—",
        "duration": "—",
        "train": "",
        "route": "A → B",
    }
