"""Test doubles and builders shared by the suites (no network, no sleeping)."""


def dep(dep_id: str, date: str, dep_time: str, arr_time: str, props=("COMFORT-B", "COMFORT-CALM")):
    """Build a minimal departure dict as returned by the search-results API."""
    return {
        "departureId": dep_id,
        "departureDateTime": f"{date}T{dep_time}:00+02:00",
        "arrivalDateTime": f"{date}T{arr_time}:00+02:00",
        "duration": "PT4H37M",
        "legs": [
            {
                "serviceProperties": [{"code": c} for c in props],
                "serviceBrandNameDescription": "X 2000",
                "publicServiceName": dep_id.upper(),
            }
        ],
    }


def offers(*, calm_price=0, second_price=0, first_price=None, flex="FULLFLEX", available=True):
    """Build a minimal offers response. Prices None = class absent."""

    def entry(offer_id, price):
        return {
            "flexibilities": {
                flex: {
                    "available": available,
                    "offerId": offer_id,
                    "journeyPrices": {"price": {"amount": price}},
                }
            }
        }

    out = {}
    if second_price is not None:
        out["SECOND"] = entry("OFF-second", second_price)
    if calm_price is not None:
        out["SECOND_CALM"] = entry("OFF-calm", calm_price)
    if first_price is not None:
        out["FIRST"] = entry("OFF-first", first_price)
    return {"seatOffers": {"offers": out}}


class FakeClient:
    """
    Scripted stand-in for SJClient covering the booking flow.

    departures: {search_id: [departure dicts]}
    offers_by_dep: {departure_id: offers response} (default: 0-price calm+second)
    Records every call in .calls as (method, *key args). Booking responses use
    the real API shape: {"bookingId": ..., "booking": {"bookingNumber": ...,
    "journeys": [{"segments": [...]}]}}, with one segment per booked leg built
    from the departure whose offer was used.
    """

    STATIONS = {"Linköping Central": "740000009", "Stockholm Central": "740000001"}

    def __init__(self, departures=None, offers_by_dep=None, *, checkout_ok=True, search_ids=None):
        self.departures = departures or {}
        self.offers_by_dep = offers_by_dep or {}
        self.checkout_ok = checkout_ok
        self.search_ids = search_ids or {
            "departureSearchId": "OUT",
            "returnDepartureSearchId": "IN",
        }
        self.calls: list[tuple] = []
        self.customer_updates: list[tuple] = []
        self._booking_counter = 0
        self._last_dep: dict | None = None
        self._bookings: dict[str, dict] = {}

    def resolve_station(self, name):
        return self.STATIONS.get(name, name)

    def search_journey(
        self, token, origin, dest, date, return_date=None, tp_id=None, service_types=None
    ):
        self.calls.append(("search", origin, dest, date, return_date))
        if return_date:
            return {"passengerListId": "PT", **self.search_ids}
        # one-way: the flow reads departureSearchId regardless of direction
        sid = "OUT" if origin == "Linköping Central" else "IN"
        return {"passengerListId": "PT", "departureSearchId": sid}

    def get_search_results(self, token, search_id):
        self.calls.append(("results", search_id))
        return {"travels": [{"departures": self.departures.get(search_id, [])}]}

    def get_offers(self, token, dep_id, passenger_token):
        self.calls.append(("offers", dep_id))
        self._last_dep = next(
            (d for deps in self.departures.values() for d in deps if d["departureId"] == dep_id),
            None,
        )
        return self.offers_by_dep.get(dep_id, offers())

    def _segment(self, direction):
        dep = self._last_dep or {}
        a, b = ("Linköping Central", "Stockholm Central")
        if direction == "INBOUND":
            a, b = b, a
        return {
            "direction": direction,
            "departureDateTime": dep.get("departureDateTime", "2026-09-01T00:00:00+02:00"),
            "arrivalDateTime": dep.get("arrivalDateTime", "2026-09-01T00:00:00+02:00"),
            "duration": dep.get("duration", "PT4H37M"),
            "departureStation": {"name": a, "uicStationCode": self.STATIONS[a]},
            "arrivalStation": {"name": b, "uicStationCode": self.STATIONS[b]},
            "productFamily": {"name": "2 klass Lugn, Kan återbetalas"},
            "serviceBrandNameDescription": "X 2000",
            "publicServiceName": "520" if direction == "OUTBOUND" else "543",
            "requiredProducts": [{"seat": {"carriageNumber": "3", "number": "17"}}],
        }

    def _response(self, booking_id):
        return {"bookingId": booking_id, "booking": self._bookings[booking_id]}

    def create_provisional_booking(self, token, offer_id, passenger_token):
        self._booking_counter += 1
        self.calls.append(("create", offer_id))
        booking_id = f"UUID-{self._booking_counter}"
        self._bookings[booking_id] = {
            "bookingNumber": f"NUM{self._booking_counter}",
            "bookingStatus": "NEW",
            "customer": {"email": "a@b.se", "phoneNumber": "+46701112233"},
            "journeys": [{"segments": [self._segment("OUTBOUND")]}],
        }
        return self._response(booking_id)

    def add_offer_to_booking(self, token, booking_id, offer_id, passenger_token):
        self.calls.append(("add", booking_id, offer_id))
        self._bookings[booking_id]["journeys"].append({"segments": [self._segment("INBOUND")]})
        return self._response(booking_id)

    def update_booking_customer(self, token, booking_id, email, phone=None):
        self.calls.append(("customer", booking_id))
        self.customer_updates.append((booking_id, email, phone))
        return {}

    def checkout_booking(self, token, booking_id):
        self.calls.append(("checkout", booking_id))
        if not self.checkout_ok:
            raise RuntimeError("checkout exploded")
        return {}


class FakeTokenManager:
    token = {"access_token": "tok", "refresh_token": "r"}

    def is_valid(self):
        return True

    def refresh_token_needs_renewal(self):
        return False


def base_cfg(**overrides):
    params = {
        "date_start": "2026-09-01",
        "date_end": "2026-09-01",
        "time_leave": "06:59",
        "time_return": "17:22",
        "station_from": "Linköping Central",
        "station_to": "Stockholm Central",
        "comfort_class": "2 class calm",
        "flexibility": "FULLFLEX",
        "roundtrip": True,
        "select_closest_ticket_available": True,
    }
    params.update(overrides)
    return {"auth": {"email": "a@b.se", "password": "x"}, "search_parameters": params}


def future_cfg(**overrides):
    """base_cfg with a date window that validates whatever today is (today+30 .. today+60)."""
    from datetime import date, timedelta

    today = date.today()
    dates = {
        "date_start": (today + timedelta(days=30)).isoformat(),
        "date_end": (today + timedelta(days=60)).isoformat(),
    }
    return base_cfg(**{**dates, **overrides})
