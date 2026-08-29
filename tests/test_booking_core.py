"""The booking core: search parsing, polling, departure facts, offers and the Cart."""

import pytest

from sj_cli.booking import Cart, describe_departure, poll_departures, resolve_offer, search
from sj_cli.errors import SJAPIError, SJError
from tests.fakes import FakeClient, base_cfg, dep, offers, seatmap

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


def test_describe_departure_never_leads_with_the_changes_separator():
    assert describe_departure({"numberOfChanges": 1}, "A → B")["train"] == "1 change"


def test_describe_departure_survives_an_empty_departure():
    assert describe_departure({}, "A → B") == {
        "departure": "—",
        "arrival": "—",
        "duration": "—",
        "train": "",
        "route": "A → B",
    }


def test_describe_departure_falls_back_to_the_service_type_name_and_counts_changes():
    d = dep("262", D, "05:29", "07:25")
    d["legs"][0]["serviceBrandNameDescription"] = None
    d["legs"][0]["serviceType"] = {"code": "SJIC", "name": "SJ InterCity"}
    assert describe_departure(d, "A → B")["train"] == "SJ InterCity 262"
    d["numberOfChanges"] = 1
    assert describe_departure(d, "A → B")["train"] == "SJ InterCity 262 · 1 change"
    d["numberOfChanges"] = 2
    assert describe_departure(d, "A → B")["train"] == "SJ InterCity 262 · 2 changes"
    d["numberOfChanges"] = None
    d["legs"].append({})
    assert describe_departure(d, "A → B")["train"] == "SJ InterCity 262 · 1 change"


# --- resolve_offer() ----------------------------------------------------------


def test_resolve_offer_returns_a_leg_with_the_zero_price_offer(capsys):
    c = FakeClient({"OUT": [dep("o-best", D, "06:59", "11:36")]})
    params = base_cfg()["search_parameters"]
    leg = resolve_offer(
        c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class calm", "outbound"
    )
    assert leg == {
        "departure": "06:59",
        "arrival": "11:36",
        "duration": "4h 37m",
        "train": "X 2000 O-BEST",
        "route": "A → B",
        "comfort_class": "2 class calm",
        "offer_id": "OFF-calm",
        "alternative": False,
    }
    assert c.calls == [("offers", "o-best")]
    assert capsys.readouterr().out == " ✓ checking offers for outbound at 06:59\n"


def test_resolve_offer_falls_through_the_class_chain_silently():
    # The caller decides whether a fallback is worth a line; the core only reports it.
    c = FakeClient(
        {"OUT": [dep("o", D, "06:59", "11:36")]}, {"o": offers(calm_price=295, second_price=0)}
    )
    params = base_cfg()["search_parameters"]
    leg = resolve_offer(
        c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class calm", "outbound"
    )
    assert leg is not None
    assert (leg["comfort_class"], leg["offer_id"]) == ("2 class", "OFF-second")


def test_resolve_offer_starts_from_the_given_class_not_the_configured_one():
    # The picked row's class wins: --book-journey passes the class the chosen
    # departure carries, which need not be the config's. Both are 0-price
    # here, so only the argument decides which offer comes back.
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]}, {"o": offers()})
    params = base_cfg()["search_parameters"]
    assert params["comfort_class"] == "2 class calm"
    leg = resolve_offer(
        c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class", "outbound"
    )
    assert leg is not None
    assert (leg["comfort_class"], leg["offer_id"]) == ("2 class", "OFF-second")


def test_resolve_offer_is_none_without_a_zero_price_offer(capsys):
    c = FakeClient(
        {"OUT": [dep("o", D, "06:59", "11:36")]}, {"o": offers(calm_price=295, second_price=195)}
    )
    params = base_cfg()["search_parameters"]
    assert (
        resolve_offer(
            c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class calm", "return"
        )
        is None
    )
    assert "checking offers for return at 06:59" in capsys.readouterr().out


def test_resolve_offer_honours_no_fallback_and_flexibility():
    # allow_class_fallback=False: the 0-price 2 class offer below is not taken.
    c = FakeClient(
        {"OUT": [dep("o", D, "06:59", "11:36")]}, {"o": offers(calm_price=295, second_price=0)}
    )
    params = base_cfg(allow_class_fallback=False)["search_parameters"]
    assert (
        resolve_offer(c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class calm", "x")
        is None
    )
    # Right class, wrong flexibility: the config asks for FULLFLEX.
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]}, {"o": offers(flex="SEMIFLEX")})
    params = base_cfg()["search_parameters"]
    assert (
        resolve_offer(c, "tok", params, "PT", c.departures["OUT"][0], "A → B", "2 class calm", "x")
        is None
    )


def test_resolve_offer_never_asks_for_offers_on_an_id_less_departure():
    c = FakeClient()
    params = base_cfg()["search_parameters"]
    assert resolve_offer(c, "tok", params, "PT", {}, "A → B", "2 class calm", "x") is None
    assert c.calls == []


# --- Cart ---------------------------------------------------------------------


def _leg(offer_id="OFF-calm", time="06:59", alternative=False):
    return {
        "departure": time,
        "arrival": "11:36",
        "duration": "4h 37m",
        "train": "X 2000 520",
        "route": "A → B",
        "comfort_class": "2 class calm",
        "offer_id": offer_id,
        "alternative": alternative,
    }


def test_cart_creates_on_the_first_add_and_adds_on_the_next(capsys):
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]})
    cart = Cart(c, "tok", base_cfg(), "PT")
    assert cart.held is False
    cart.add(_leg(), "outbound")
    assert cart.held is True
    assert (cart.booking_id, cart.booking_number) == ("UUID-1", "NUM1")
    cart.add(_leg("OFF-second", "17:22", alternative=True), "return")
    assert cart.legs == ["outbound", "return"]
    assert len(cart.booking["journeys"]) == 2
    assert c.calls == [("create", "OFF-calm"), ("add", "UUID-1", "OFF-second")]
    out = capsys.readouterr().out
    assert " ✓ creating booking with outbound at 06:59\n" in out
    assert " ✓ adding alternative return leg at 17:22\n" in out


def test_cart_finish_sends_the_customer_back_and_checks_out():
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]})
    cart = Cart(c, "tok", base_cfg(), "PT")
    cart.add(_leg(), "outbound")
    result = cart.finish()
    assert {k: v for k, v in result.items() if k != "booking"} == {
        "booking_id": "UUID-1",
        "booking_number": "NUM1",
        "legs": ["outbound"],
        "checked_out": True,
    }
    assert result["booking"]["bookingNumber"] == "NUM1"
    assert c.calls == [("create", "OFF-calm"), ("customer", "UUID-1"), ("checkout", "UUID-1")]
    assert c.customer_updates == [("UUID-1", "a@b.se", "+46701112233")]


def test_cart_finish_chooses_seats_before_checkout_when_preferred():
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]})
    c.seatmaps["SM-OUTBOUND"] = seatmap()
    cart = Cart(c, "tok", base_cfg(seat_preference=["window"]), "PT")
    cart.add(_leg(), "outbound")
    cart.finish()
    assert [call[0] for call in c.calls] == ["create", "seatmap", "seats", "customer", "checkout"]


def test_cart_finish_reports_a_failed_checkout_instead_of_raising(capsys):
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]}, checkout_ok=False)
    cart = Cart(c, "tok", base_cfg(), "PT")
    cart.add(_leg(), "outbound")
    result = cart.finish()
    assert result["checked_out"] is False
    assert result["booking_id"] == "UUID-1"
    assert " ! checkout failed: checkout exploded\n" in capsys.readouterr().out


def test_cart_refuses_a_provisional_without_an_id_and_an_empty_finish(capsys):
    class NoId(FakeClient):
        def create_provisional_booking(self, token, offer_id, passenger_token):
            self.calls.append(("create", offer_id))
            return {"booking": {}}

    cart = Cart(NoId(), "tok", base_cfg(), "PT")
    with pytest.raises(SJAPIError):
        cart.add(_leg(), "outbound")
    # Nothing of the half-made booking is kept, and the trail says the step failed.
    assert cart.held is False
    assert cart.booking_number is None
    assert cart.booking == {}
    assert cart.legs == []
    assert " ✗ creating booking with outbound at 06:59\n" in capsys.readouterr().out
    with pytest.raises(SJError):
        cart.finish()


def test_cart_keeps_the_booking_when_an_added_leg_comes_back_without_journeys():
    class NoJourneys(FakeClient):
        def add_offer_to_booking(self, token, booking_id, offer_id, passenger_token):
            self.calls.append(("add", booking_id, offer_id))
            return {"bookingId": booking_id, "booking": {}}

    cart = Cart(NoJourneys(), "tok", base_cfg(), "PT")
    cart.add(_leg(), "outbound")
    cart.add(_leg("OFF-second", "17:22"), "return")
    # The thin response must not wipe the journeys the card is rendered from.
    assert len(cart.booking["journeys"]) == 1
    assert cart.legs == ["outbound", "return"]


def test_cart_refuses_a_leg_without_an_offer():
    c = FakeClient()
    cart = Cart(c, "tok", base_cfg(), "PT")
    with pytest.raises(SJError):
        cart.add({**_leg(), "offer_id": None}, "outbound")
    assert c.calls == []
    assert cart.held is False


def test_cart_refuses_to_be_used_after_it_is_checked_out():
    c = FakeClient({"OUT": [dep("o", D, "06:59", "11:36")]})
    cart = Cart(c, "tok", base_cfg(), "PT")
    cart.add(_leg(), "outbound")
    cart.finish()
    spent = list(c.calls)
    with pytest.raises(SJError):
        cart.finish()
    with pytest.raises(SJError):
        cart.add(_leg("OFF-second", "17:22"), "return")
    assert c.calls == spent


def test_cart_names_a_failed_customer_patch_and_never_checks_out(capsys):
    class NoCustomer(FakeClient):
        def update_booking_customer(self, token, booking_id, email, phone=None):
            self.calls.append(("customer", booking_id))
            raise RuntimeError("customer exploded")

    c = NoCustomer({"OUT": [dep("o", D, "06:59", "11:36")]})
    cart = Cart(c, "tok", base_cfg(), "PT")
    cart.add(_leg(), "outbound")
    result = cart.finish()
    assert result["checked_out"] is False
    assert ("checkout", "UUID-1") not in c.calls
    assert " ! customer details failed: customer exploded\n" in capsys.readouterr().out
