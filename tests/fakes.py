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


def seatmap(
    free=(("70", ["TABLE", "WINDOW"], True),),
    assigned=("3", "39"),
    assigned_codes=None,
    can_change=True,
    carriage="3",
    comforts=("SECOND_CALM",),
    extra=(),
):
    """
    Seat map fixture: free is [(seat number, property codes, reversed)].

    Each entry may carry two more elements — (row number, ypos) — for tests
    that need real carriage geometry: seats.py's single-seat heuristic (the
    widest gap between a carriage's distinct ypos values is the aisle) needs
    at least two distinct ypos values to mean anything, and geometry is
    omitted by default. A 2+1 carriage (one single-seat side, one paired
    side) needs three ypos values, e.g. row 1: ("11", [...], False, 1, 5),
    ("13", [...], True, 1, 108), ("14", [...], False, 1, 144) — seat 11
    alone at ypos 5 is a single, 13/14 paired at 108/144 are not.

    extra adds further carriages as (carriage number, free, comforts) tuples —
    seat numbers repeat across carriages on a real map, so tests that care
    about the pair need more than one. assigned_codes, when given, puts
    carriageSeatProperties on the assigned (passengerSeats[0]) entry — for
    --seat-details' fallback path, which reads that seat's own codes rather
    than joining it against the carriage layout the way free_seats() (and
    the primary, layout-lookup path) does.
    """

    def _seat(spec):
        n, codes, rev, *geometry = spec
        row_number = geometry[0] if len(geometry) > 0 else None
        ypos = geometry[1] if len(geometry) > 1 else None
        seat = {
            "seatNumber": n,
            "reversed": rev,
            "carriageSeatProperties": [{"code": c} for c in codes],
        }
        if row_number is not None:
            seat["rowNumber"] = row_number
        if ypos is not None:
            seat["ypos"] = ypos
        return seat

    def _carriage(number, seats, carriage_comforts):
        return {
            "carriageNumber": number,
            "reversed": True,
            "carriageComforts": list(carriage_comforts),
            "seats": [_seat(spec) for spec in seats],
        }

    carriages = [(carriage, free, comforts), *extra]
    assigned_seat = {
        "carriageNumber": assigned[0],
        "seatNumber": assigned[1],
        "inventoryClass": "SECOND_CALM",
    }
    if assigned_codes is not None:
        assigned_seat["carriageSeatProperties"] = [{"code": c} for c in assigned_codes]
    return {
        "carriages": [_carriage(*c) for c in carriages],
        "seatsPossibleToSelect": {c: [spec[0] for spec in seats] for c, seats, _ in carriages},
        "passengerSeats": [assigned_seat],
        "canChangeSeat": can_change,
        "hasDeparted": False,
    }


class FakeClient:
    """
    Scripted stand-in for SJClient covering the booking flow.

    departures: {search_id: [departure dicts]}
    offers_by_dep: {departure_id: offers response} (default: 0-price calm+second)
    Records every call in .calls as (method, *key args). Booking responses use
    the real API shape: {"bookingId": ..., "booking": {"bookingNumber": ...,
    "journeys": [{"segments": [...]}]}}, with one segment per booked leg built
    from the departure whose offer was used: get_offers queues each departure
    under every 0-price offer id it hands out (the only ids a flow can book),
    and create/add pop the queue, so a flow that resolves both legs before
    writing (--book-journey) gets each leg's own times, not the last one
    searched. The segments' stations come from the first search's
    origin/dest — a name or a UIC code, as the caller passed it.
    """

    STATIONS = {"Göteborg Central": "740000002", "Stockholm Central": "740000001"}
    STATION_LIST = [
        {"name": "Stockholm Central", "uicStationCode": "740000001", "synonyms": ["1"]},
        {"name": "Göteborg Central", "uicStationCode": "740000002", "synonyms": ["2"]},
        {"name": "Uppsala Central", "uicStationCode": "740000005", "synonyms": []},
    ]

    def __init__(self, departures=None, offers_by_dep=None, *, checkout_ok=True, search_ids=None):
        self.departures = departures or {}
        self.offers_by_dep = offers_by_dep or {}
        self.checkout_ok = checkout_ok
        self.search_ids = search_ids or {
            "departureSearchId": "OUT",
            "returnDepartureSearchId": "IN",
        }
        self.calls: list[tuple] = []
        # (name, code) of the first search, so a booked segment says where
        # the journey actually went; departures queued per 0-price offer id.
        self._route: tuple[str, str] | None = None
        self._offer_deps: dict[str, list[dict]] = {}
        self.customer_updates: list[tuple] = []
        self._booking_counter = 0
        self._last_dep: dict | None = None
        self._bookings: dict[str, dict] = {}
        self.seatmaps: dict[str, dict] = {}  # seatMapSearchId -> seat map
        self.seat_updates: list[tuple] = []  # (booking_id, updates, provisional)
        self.seatmap_error: Exception | None = None
        self.seat_update_error: Exception | None = None
        self.bookings_list: list[dict] = []
        self.cancel_payloads: list[tuple] = []  # (booking_id, payload) per PATCH
        self.cancel_error: Exception | None = None
        self.finalize_error: Exception | None = None
        # One entry per search_journey call, in order — the upgrade-class probe's
        # whole point is that this must be None (a pass-free search); tests assert
        # against it directly rather than digging through .calls' plain tuples.
        self.search_tp_ids: list[str | None] = []

    def resolve_station(self, name):
        return self.STATIONS.get(name, name)

    def get_stations(self):
        self.calls.append(("stations",))
        return [{**s, "synonyms": list(s["synonyms"])} for s in self.STATION_LIST]

    def _station(self, value):
        """(display name, UIC code) for a search endpoint given as either."""
        for entry in self.STATION_LIST:
            if value in (entry["name"], entry["uicStationCode"]):
                return entry["name"], entry["uicStationCode"]
        return value, self.STATIONS.get(value, value)

    def search_journey(
        self, token, origin, dest, date, return_date=None, tp_id=None, service_types=None
    ):
        self.calls.append(("search", origin, dest, date, return_date))
        if self._route is None:
            self._route = (origin, dest)
        self.search_tp_ids.append(tp_id)
        if return_date:
            return {"passengerListId": "PT", **self.search_ids}
        if tp_id is None:
            # The upgrade-class probe: a deterministic id per route+date so a
            # test can serve distinct departures for distinct days without
            # colliding with the booking flow's static OUT/IN ids below.
            sid = f"{origin}->{dest}@{date}"
            return {"passengerListId": "PT", "departureSearchId": sid}
        # one-way with the pass: the flow reads departureSearchId regardless of direction
        sid = "OUT" if origin in ("Göteborg Central", "740000002") else "IN"
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
        response = self.offers_by_dep.get(dep_id, offers())
        self._queue_offers(response)
        return response

    def _queue_offers(self, response):
        """Remember this departure under every 0-price offer id in the response."""
        if self._last_dep is None:
            return
        for offer in ((response.get("seatOffers") or {}).get("offers") or {}).values():
            for flex in (offer.get("flexibilities") or {}).values():
                price = ((flex.get("journeyPrices") or {}).get("price") or {}).get("amount")
                if flex.get("available") and price == 0 and flex.get("offerId"):
                    self._offer_deps.setdefault(flex["offerId"], []).append(self._last_dep)

    def _dep_for(self, offer_id):
        """The departure whose offer this is (the last one searched as a fallback)."""
        queued = self._offer_deps.get(offer_id) or []
        return queued.pop(0) if queued else (self._last_dep or {})

    def _segment(self, direction, dep=None):
        dep = dep if dep is not None else (self._last_dep or {})
        raw = self._route or ("Göteborg Central", "Stockholm Central")
        a, b = (self._station(raw[0]), self._station(raw[1]))
        if direction == "INBOUND":
            a, b = b, a
        return {
            "direction": direction,
            "departureDateTime": dep.get("departureDateTime", "2026-09-01T00:00:00+02:00"),
            "arrivalDateTime": dep.get("arrivalDateTime", "2026-09-01T00:00:00+02:00"),
            "duration": dep.get("duration", "PT4H37M"),
            "departureStation": {"name": a[0], "uicStationCode": a[1]},
            "arrivalStation": {"name": b[0], "uicStationCode": b[1]},
            "productFamily": {
                "name": "2 klass Lugn, Kan återbetalas",
                "salesCategoryComfort": "SECOND_CALM",
                "salesCategoryFlexibility": "FULLFLEX",
            },
            "serviceBrandNameDescription": "X 2000",
            "publicServiceName": "520" if direction == "OUTBOUND" else "543",
            "requiredProducts": [{"seat": {"carriageNumber": "3", "number": "17"}}],
            "seatMapAvailable": True,
            "seatMapSearchId": f"SM-{direction}",
            "serviceIdentifier": f"SI-{direction}",
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
            "journeys": [{"segments": [self._segment("OUTBOUND", self._dep_for(offer_id))]}],
        }
        return self._response(booking_id)

    def add_offer_to_booking(self, token, booking_id, offer_id, passenger_token):
        self.calls.append(("add", booking_id, offer_id))
        self._bookings[booking_id]["journeys"].append(
            {"segments": [self._segment("INBOUND", self._dep_for(offer_id))]}
        )
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

    def cancel_booking_with_patch(self, token, booking_id, segments_and_passengers):
        # Recorded before the error hook fires: the request is what the test
        # is judging, and a failing PATCH still had a payload.
        self.calls.append(("cancel", booking_id))
        self.cancel_payloads.append((booking_id, segments_and_passengers))
        if self.cancel_error:
            raise self.cancel_error

    def finalize_cancellation(self, token, booking_id):
        self.calls.append(("finalize", booking_id))
        if self.finalize_error:
            raise self.finalize_error

    def get_bookings(self, token, start_date, end_date, page=0):
        self.calls.append(("bookings", start_date, end_date))
        # fetch_all_bookings reads "bookings" and paginates on "nextPage";
        # omitting nextPage ends the loop after one page.
        return {"bookings": self.bookings_list}

    def get_seatmap(self, token, booking_id, seatmap_search_id):
        self.calls.append(("seatmap", booking_id, seatmap_search_id))
        if self.seatmap_error:
            raise self.seatmap_error
        if seatmap_search_id in self.seatmaps:
            return self.seatmaps[seatmap_search_id]
        # Only the ids this fake's own segments carry get the default map: an
        # unknown id (None included) means the caller skipped the
        # seatMapAvailable/seatMapSearchId gate, and must not be handed a map.
        if seatmap_search_id in ("SM-OUTBOUND", "SM-INBOUND"):
            return seatmap()
        raise AssertionError(f"no seat map exists for {seatmap_search_id!r}")

    def _listed_booking(self, booking_id):
        """A booking from bookings_list by id — the change-seat modes' targets."""
        for item in self.bookings_list:
            booking = item.get("booking") or {}
            if booking_id in (item.get("bookingId"), booking.get("bookingId")):
                return booking
        return {}

    def update_seats(self, token, booking_id, updates, provisional=True):
        self.calls.append(("seats", booking_id, provisional))
        self.seat_updates.append((booking_id, updates, provisional))
        if self.seat_update_error:
            raise self.seat_update_error
        booking = self._bookings.get(booking_id) or self._listed_booking(booking_id)
        for update in updates:
            for journey in booking.get("journeys") or []:
                for seg in journey.get("segments") or []:
                    # Direction alone is not a segment id: a booking can hold
                    # the same direction on several days.
                    if seg.get("direction") != update["direction"]:
                        continue
                    if seg.get("serviceIdentifier") != update["serviceIdentifier"]:
                        continue
                    for product in seg.get("requiredProducts") or []:
                        product["seat"] = {
                            "number": update["seatNumber"],
                            "carriageNumber": update["carriageNumber"],
                        }
        return {"bookingId": booking_id, "booking": booking}


class FakeTokenManager:
    token = {"access_token": "tok", "refresh_token": "r"}

    def is_valid(self):
        return True

    def refresh_token_needs_renewal(self):
        return False


def base_cfg(**overrides):
    params = {
        "dates": "2026-09-01",
        "time_leave": "06:59",
        "time_return": "17:22",
        "station_from": "Göteborg Central",
        "station_to": "Stockholm Central",
        "comfort_class": "2 class calm",
        "flexibility": "FULLFLEX",
        "roundtrip": True,
        "select_closest_ticket_available": True,
    }
    params.update(overrides)
    return {"auth": {"email": "a@b.se", "password": "x"}, "search_parameters": params}


def future_cfg(**overrides):
    """base_cfg with a dates selection that validates whatever today is (today+30 .. today+60)."""
    from datetime import date, timedelta

    today = date.today()
    window = (
        f"{(today + timedelta(days=30)).isoformat()}..{(today + timedelta(days=60)).isoformat()}"
    )
    return base_cfg(**{"dates": window, **overrides})
