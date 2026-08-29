"""--book-journey: the prompts, the pick lists and the write path through the Cart."""

import re
from datetime import timedelta

import pytest

from sj_cli import booking, journey
from sj_cli.booking import booking_date_range
from sj_cli.dates import to_sweden
from sj_cli.stations import StationIndex, parse_stations
from tests.fakes import FakeClient, base_cfg, dep, offers

# A fixed summer noon, not the real clock: wire() freezes both modules to it,
# so every date below (and every wall-clock time asserted on a card) is the
# same whenever the suite runs. Noon, so a run near midnight cannot wrap
# _shift()'s time_leave into the previous day; summer, because the fixtures
# write their timestamps in +02:00 and a date in CET would render an hour off.
NOW = to_sweden("2026-06-15T12:00:00+02:00")
TODAY = NOW.date()
FUTURE = TODAY + timedelta(days=30)
VALID = (TODAY - timedelta(days=10), TODAY + timedelta(days=300))


class Script:
    """
    Answers the wizard's prompts in order.

    Text prompts (ask_optional, confirm) take strings/bools/None; a station
    pick takes "" (default), a query string (first match) or None (abort);
    a departure pick takes "" (default row), a 0-based row index or None
    (the real widget types 1-based numbers; the tests script by index).
    """

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.lists = []  # (prompt, rendered rows, default_index, rejected rows)

    def _next(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def ask_optional(self, text):
        return self._next(text)

    def confirm(self, question):
        return self._next(question)

    def select_filtered(self, prompt, default, search, render, **_kw):
        reply = self._next(prompt)
        if reply is None:
            return None
        if reply == "":
            return default
        hits = search(reply)
        return hits[0] if hits else None

    def select_list(self, prompt, items, render, default_index=0, reject=None, **_kw):
        rows = [render(i) for i in items]
        rejected = [reject(i) for i in items if reject and reject(i)]  # the complaints
        if not 0 <= default_index < len(items):
            default_index = 0  # the widget highlights row 1 for an out-of-range default
        self.lists.append((prompt, rows, default_index, rejected))
        reply = self._next(prompt)
        if reply is None:
            return None
        chosen = items[default_index] if reply == "" else items[reply]
        complaint = reject(chosen) if reject else None
        if complaint:
            # The widget keeps the prompt open on a rejected row, so a script
            # that picks one is testing something that cannot happen.
            pytest.fail(f"picked a rejected row: {complaint}")
        return chosen


def index():
    return StationIndex(parse_stations(FakeClient.STATION_LIST))


# --- _ask_date -----------------------------------------------------------------


def test_ask_date_enter_keeps_the_default(monkeypatch):
    s = Script("")
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == TODAY
    assert s.prompts == [f"date [{TODAY.isoformat()}]: "]


def test_ask_date_reasks_on_garbage_past_and_outside_the_pass(monkeypatch, capsys):
    s = Script(
        "soon",
        (TODAY - timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=400)).isoformat(),
        FUTURE.isoformat(),
    )
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == FUTURE
    out = capsys.readouterr().out
    assert " ! not a date, use YYYY-MM-DD\n" in out
    assert " ! date is in the past\n" in out
    assert f" ! the pass is valid {VALID[0].isoformat()} – {VALID[1].isoformat()}\n" in out


def test_ask_date_uses_the_callers_too_early_text_and_skips_unknown_bounds(monkeypatch, capsys):
    s = Script((FUTURE - timedelta(days=1)).isoformat(), FUTURE.isoformat())
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert (
        journey._ask_date(
            "return date", FUTURE, FUTURE, "return date is before the outbound date", (None, None)
        )
        == FUTURE
    )
    assert " ! return date is before the outbound date\n" in capsys.readouterr().out


def test_ask_date_ctrl_d_is_none(monkeypatch):
    monkeypatch.setattr(journey, "ask_optional", Script(None).ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "x", VALID) is None


def test_ask_date_default_is_clamped_to_the_pass_start(monkeypatch):
    valid = (TODAY + timedelta(days=5), TODAY + timedelta(days=300))
    s = Script("")
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", valid) == valid[0]
    assert s.prompts == [f"date [{valid[0].isoformat()}]: "]


def test_ask_date_default_is_clamped_to_the_pass_end(monkeypatch):
    valid = (TODAY - timedelta(days=10), TODAY + timedelta(days=5))
    s = Script("")
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    ahead = TODAY + timedelta(days=30)
    assert journey._ask_date("date", ahead, TODAY, "date is in the past", valid) == valid[1]
    assert s.prompts == [f"date [{valid[1].isoformat()}]: "]


def test_ask_date_whitespace_reply_keeps_the_default(monkeypatch):
    s = Script("   ")
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == TODAY


def test_ask_date_accepts_both_pass_boundary_days(monkeypatch, capsys):
    valid = (TODAY, TODAY + timedelta(days=10))
    s = Script(valid[0].isoformat(), valid[1].isoformat())
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", valid) == valid[0]
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", valid) == valid[1]
    assert " ! " not in capsys.readouterr().out


# --- _ask_station --------------------------------------------------------------


def test_ask_station_passes_the_default_and_refuses_the_other_endpoint(monkeypatch, capsys):
    idx = index()
    gbg, sth = idx.exact("Göteborg Central"), idx.exact("Stockholm Central")
    picks = iter([sth, gbg])
    seen_defaults = []

    def fake_select(prompt, default, search, render, **_kw):
        seen_defaults.append(default)
        if default is not None:
            assert (prompt, render(default), search("upp")[0]["name"]) == (
                "to",
                "Stockholm Central",
                "Uppsala Central",
            )
        return next(picks)

    monkeypatch.setattr(journey, "select_filtered", fake_select)
    assert journey._ask_station("to", sth, idx, other=sth) == gbg
    assert seen_defaults == [sth, None]
    assert " ! from and to are the same station\n" in capsys.readouterr().out


def test_ask_station_abort_is_none(monkeypatch):
    monkeypatch.setattr(journey, "select_filtered", lambda *_a, **_k: None)
    assert journey._ask_station("from", None, index(), other=None) is None


# --- _ask_yes_no and _default_station -------------------------------------------


@pytest.mark.parametrize(
    ("default", "reply", "expected", "shown"),
    [
        (True, "", True, "[Y/n]"),
        (False, "", False, "[y/N]"),
        (False, "Y", True, "[y/N]"),
        (True, "no", False, "[Y/n]"),
        (True, None, None, "[Y/n]"),
    ],
)
def test_ask_yes_no(monkeypatch, default, reply, expected, shown):
    s = Script(reply)
    monkeypatch.setattr(journey, "ask_optional", s.ask_optional)
    assert journey._ask_yes_no("return?", default) is expected
    assert s.prompts == [f"return? {shown}: "]


def test_ask_yes_no_whitespace_reply_keeps_the_default(monkeypatch):
    monkeypatch.setattr(journey, "ask_optional", Script("   ").ask_optional)
    assert journey._ask_yes_no("return?", True) is True


def test_default_station_prefers_the_live_list_then_the_map():
    idx = index()
    assert journey._default_station(idx, FakeClient(), "göteborg central")["code"] == "740000002"
    assert journey._default_station(idx, FakeClient(), "Linköping Central") == {
        "name": "Linköping Central",
        "code": "Linköping Central",
        "synonyms": [],
    }


# --- the flow -------------------------------------------------------------------

D = FUTURE.isoformat()
D2 = (FUTURE + timedelta(days=1)).isoformat()
OUT = [
    dep("o-early", D, "06:30", "08:10"),
    dep("o-best", D, "06:59", "11:36"),
    dep("o-late", D, "07:30", "09:10"),
]
IN = [
    dep("i-early", D, "16:50", "18:30"),
    dep("i-best", D, "17:22", "21:53"),
    dep("i-late", D, "18:00", "19:40"),
]
NO_CLASS = dep("o-bus", D, "07:00", "09:30", props=())
NO_OFFER = offers(calm_price=295, second_price=195)
PASS = {
    "name": "Pass",
    "travelPassId": "TP",
    "startTravelValidityDateTime": (NOW - timedelta(days=10)).isoformat(),
    "endTravelValidityDateTime": (NOW + timedelta(days=300)).isoformat(),
}
WRITES = {"create", "add", "seats", "customer", "checkout"}


def wire(monkeypatch, script, tty=True):
    for name in ("ask_optional", "confirm", "select_filtered", "select_list"):
        monkeypatch.setattr(journey, name, getattr(script, name))
    monkeypatch.setattr(journey, "sweden_now", lambda: NOW)
    monkeypatch.setattr(booking, "sweden_now", lambda: NOW)  # booking_date_range reads it
    monkeypatch.setattr(journey.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(journey.sys.stdout, "isatty", lambda: tty)


def run(client, script, cfg=None, dry_run=False):
    return journey.handle_book_journey(
        client, "tok", cfg or base_cfg(), PASS, "TP", "TOK", dry_run=dry_run
    )


def test_roundtrip_with_every_default_reproduces_the_commute(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    s = Script(
        "", "", "", "", "", "", "", True
    )  # date, from, to, return?, return date, outbound, return, book?
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert c.calls == [
        ("stations",),
        ("search", "740000002", "740000001", TODAY.isoformat(), TODAY.isoformat()),
        ("results", "OUT"),
        ("results", "IN"),
        ("bookings", *booking_date_range(PASS)),
        ("offers", "o-best"),
        ("offers", "i-best"),
        ("create", "OFF-calm"),
        ("add", "UUID-1", "OFF-calm"),
        ("customer", "UUID-1"),
        ("checkout", "UUID-1"),
    ]
    assert s.prompts == [
        f"date [{TODAY.isoformat()}]: ",
        "from",
        "to",
        "return? [Y/n]: ",
        f"return date [{TODAY.isoformat()}]: ",
        "outbound",
        "return",
        "book? [y/N]: ",
    ]
    assert s.lists[0][2] == 1 and s.lists[1][2] == 1  # closest to 06:59 / 17:22
    out = capsys.readouterr().out
    assert "✓ checking offers for outbound at 06:59" in out
    assert "✓ creating booking with outbound at 06:59" in out
    assert "✓ adding return leg at 17:22" in out
    assert "NUM1" in out
    assert out.rstrip().endswith(" ● booked NUM1")


def test_one_way_on_another_route_and_date(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT})
    s = Script(D, "", "upp", "n", 0, True)
    wire(monkeypatch, s)
    assert run(c, s, base_cfg(roundtrip=False)) is True
    assert c.calls[1] == ("search", "740000002", "740000005", D, None)
    assert [call[0] for call in c.calls[2:]] == [
        "results",
        "bookings",
        "offers",
        "create",
        "customer",
        "checkout",
    ]
    assert s.prompts[3] == "return? [y/N]: "
    # once on the summary card, once on the booked card
    assert capsys.readouterr().out.count("Göteborg Central → Uppsala Central") >= 2


def test_lists_show_class_fallback_and_no_seats_and_reject_the_latter(monkeypatch):
    c = FakeClient(
        {"OUT": [OUT[0], NO_CLASS, dep("o-second", D, "08:00", "09:40", props=("COMFORT-B",))]}
    )
    s = Script("", "", "", "n", 2, True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    _, rows, default_index, rejected = s.lists[0]
    assert rows[0].endswith("2 class calm")
    assert "—" in rows[1] and "no seats" in rows[1]
    assert re.search(r"2 class\s+fallback$", rows[2])
    assert rejected == ["no seats at 07:00 · pick another"]
    assert default_index == 0  # 06:30 is the closest enabled row to 06:59 (07:00 has no seats)


def test_offerless_pick_reopens_the_list_with_that_row_disabled(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT}, {"o-best": NO_OFFER})
    s = Script("", "", "", "n", "", 0, True)  # default row (o-best) has no offer → pick row 0
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert [call for call in c.calls if call[0] == "offers"] == [
        ("offers", "o-best"),
        ("offers", "o-early"),
    ]
    assert len(s.lists) == 2
    assert s.lists[1][2] == 0  # the re-opened list highlights the closest still-enabled row
    assert any("no 0-price offer at 06:59" in r for r in s.lists[1][3])
    assert " ! no 0-price offer at 06:59 · pick another\n" in capsys.readouterr().out


def test_class_fallback_on_the_offer_is_said(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT}, {"o-best": offers(calm_price=295, second_price=0)})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert " ! outbound class fallback: 2 class calm → 2 class\n" in capsys.readouterr().out


def held(
    number,
    day,
    status="CONFIRMED",
    dep_time="06:59",
    arr_time="11:36",
    arr_day=None,
    origin="Göteborg Central",
    dest="Stockholm Central",
):
    """A bookings-list entry: one segment leaving `day` at dep_time (arr_day = an overnight)."""
    return {
        "bookingId": f"U-{number}",
        "booking": {
            "bookingNumber": number,
            "bookingStatus": status,
            "journeys": [
                {
                    "segments": [
                        {
                            "departureDateTime": f"{day}T{dep_time}:00+02:00",
                            "arrivalDateTime": f"{arr_day or day}T{arr_time}:00+02:00",
                            "departureStation": {"name": origin},
                            "arrivalStation": {"name": dest},
                        }
                    ]
                }
            ],
        },
    }


def test_held_booking_on_the_date_is_pointed_out(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT})
    c.bookings_list = [held("HELD1", D)]
    s = Script(D, "", "", "n", "", False)
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert (
        f" ! you hold booking HELD1 on {D} (Göteborg Central → Stockholm Central 06:59)"
        " · departures overlapping it show no seats\n"
    ) in capsys.readouterr().out


def test_return_on_another_date_prints_two_cards(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT, "IN": [dep("i-best", D2, "17:22", "21:53")]})
    s = Script(D, "", "", "y", D2, "", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert c.calls[1] == ("search", "740000002", "740000001", D, D2)
    out = capsys.readouterr().out
    assert out.count("Göteborg Central → Stockholm Central") >= 1
    assert out.count("Stockholm Central → Göteborg Central") >= 1


def test_decline_and_dry_run_write_nothing(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT, "IN": IN})
    s = Script("", "", "", "", "", "", "", False)
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert capsys.readouterr().out.rstrip().endswith(" ● booking aborted, nothing was booked")

    c = FakeClient({"OUT": OUT, "IN": IN})
    s = Script("", "", "", "", "", "", "")
    wire(monkeypatch, s)
    assert run(c, s, dry_run=True) is True
    assert not any(call[0] in WRITES for call in c.calls)
    assert "book?" not in s.prompts
    assert capsys.readouterr().out.rstrip().endswith(" ● dry run · nothing booked")


@pytest.mark.parametrize("at", range(7))
def test_abort_at_any_prompt_writes_nothing(monkeypatch, capsys, at):
    c = FakeClient({"OUT": OUT, "IN": IN})
    replies = ["", "", "", "", "", "", ""]
    replies[at] = None
    s = Script(*replies[: at + 1])
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert capsys.readouterr().out.rstrip().endswith(" ● booking aborted, nothing was booked")


def test_not_a_terminal_is_refused_before_any_request(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT})
    s = Script()
    wire(monkeypatch, s, tty=False)
    assert run(c, s, dry_run=True) is False
    assert c.calls == []
    assert " ● not a terminal · --book-journey asks questions" in capsys.readouterr().out


def test_station_list_failure_ends_the_run(monkeypatch, capsys):
    class NoStations(FakeClient):
        def get_stations(self):
            raise RuntimeError("boom")

    s = Script()
    wire(monkeypatch, s)
    assert run(NoStations(), s) is False
    assert s.prompts == []
    assert " ● could not fetch the station list: boom" in capsys.readouterr().out


def test_no_departures_ends_the_run(monkeypatch, capsys):
    c = FakeClient({"OUT": []})
    s = Script(D, "", "", "n")
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] == "offers" for call in c.calls)
    assert (
        f" ● no departures found for Göteborg Central → Stockholm Central on {D}"
        in capsys.readouterr().out
    )


def test_failed_return_add_books_the_outbound_alone(monkeypatch, capsys):
    class NoAdd(FakeClient):
        def add_offer_to_booking(self, token, booking_id, offer_id, passenger_token):
            self.calls.append(("add", booking_id, offer_id))
            raise RuntimeError("add exploded")

    c = NoAdd({"OUT": OUT, "IN": IN})
    s = Script("", "", "", "", "", "", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert [call[0] for call in c.calls[-3:]] == ["add", "customer", "checkout"]
    out = capsys.readouterr().out
    assert " ! return leg failed (add exploded), booking outbound only\n" in out
    assert out.rstrip().endswith(" ● booked NUM1")


def test_failed_checkout_is_red(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT}, checkout_ok=False)
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is False
    out = capsys.readouterr().out
    assert " ! checkout failed: checkout exploded\n" in out
    assert "● booking NUM1 not checked out" in out
    assert out.rstrip().endswith("provisional left, SJ releases it or cancel it on sj.se")


def test_seat_preference_is_applied_on_the_cart(monkeypatch):
    c = FakeClient({"OUT": OUT})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s, base_cfg(roundtrip=False, seat_preference=["window"])) is True
    assert [call[0] for call in c.calls[-5:]] == [
        "create",
        "seatmap",
        "seats",
        "customer",
        "checkout",
    ]


def test_a_cancelled_booking_and_another_day_are_not_named(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT})
    c.bookings_list = [held("GONE1", D, status="CANCELLED"), held("OTHER1", D2)]
    s = Script(D, "", "", "n", "", False)
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert "you hold booking" not in capsys.readouterr().out


def test_a_failed_bookings_fetch_is_only_a_note(monkeypatch, capsys):
    class NoBookings(FakeClient):
        def get_bookings(self, token, start_date, end_date, page=0):
            self.calls.append(("bookings", start_date, end_date))
            raise RuntimeError("bookings exploded")

    c = NoBookings({"OUT": OUT})
    s = Script(D, "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert " ! could not check existing bookings: bookings exploded\n" in capsys.readouterr().out
    assert len(s.lists) == 1  # the wizard went on to the pick list


def test_no_return_departures_ends_the_run(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT, "IN": []})
    s = Script(D, "", "", "y", "")
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] == "offers" for call in c.calls)
    assert (
        f" ● no departures found for Stockholm Central → Göteborg Central on {D}"
        in capsys.readouterr().out
    )


def test_abort_on_the_reopened_list_writes_nothing(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT}, {"o-best": NO_OFFER})
    s = Script("", "", "", "n", "", None)  # the default row has no offer, then Esc
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert capsys.readouterr().out.rstrip().endswith(" ● booking aborted, nothing was booked")


def test_a_failed_first_add_ends_the_run(monkeypatch, capsys):
    class NoCreate(FakeClient):
        def create_provisional_booking(self, token, offer_id, passenger_token):
            self.calls.append(("create", offer_id))
            raise RuntimeError("create exploded")

    c = NoCreate({"OUT": OUT})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is False
    assert not any(call[0] in ("customer", "checkout") for call in c.calls)
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(" ● could not create the booking (create exploded) · nothing was booked")
    )


def test_a_card_that_cannot_be_rendered_still_reports_the_booking(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)

    def boom(*_a, **_k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(journey, "booked_rows", boom)
    assert run(c, s) is True
    out = capsys.readouterr().out
    assert " ! booked as NUM1, but the legs could not be shown (render exploded)\n" in out
    assert out.rstrip().endswith(" ● booked NUM1")


def test_ctrl_c_after_the_first_add_names_the_held_provisional(monkeypatch, capsys):
    class Interrupted(FakeClient):
        def checkout_booking(self, token, booking_id):
            self.calls.append(("checkout", booking_id))
            raise KeyboardInterrupt

    c = Interrupted({"OUT": OUT})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    with pytest.raises(KeyboardInterrupt):
        run(c, s)
    assert (
        " ! booking NUM1 left as a provisional, SJ releases it or cancel it on sj.se\n"
        in capsys.readouterr().out
    )


def test_ctrl_c_before_anything_is_held_names_nothing(monkeypatch, capsys):
    class Interrupted(FakeClient):
        def create_provisional_booking(self, token, offer_id, passenger_token):
            self.calls.append(("create", offer_id))
            raise KeyboardInterrupt

    c = Interrupted({"OUT": OUT})
    s = Script("", "", "", "n", "", True)
    wire(monkeypatch, s)
    with pytest.raises(KeyboardInterrupt):
        run(c, s)
    assert "left as a provisional" not in capsys.readouterr().out


# --- departed trains ---------------------------------------------------------------

T = TODAY.isoformat()


def _shift(minutes):
    return (NOW + timedelta(minutes=minutes)).strftime("%H:%M")


def _dep_at(dep_id, minutes, duration=277):  # 277 min = dep()'s "PT4H37M"
    """dep() at NOW + minutes, timestamped in NOW's own offset (not dep()'s fixed +02:00)."""
    start = NOW + timedelta(minutes=minutes)
    end = start + timedelta(minutes=duration)
    d = dep(dep_id, start.date().isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"))
    d["departureDateTime"] = start.isoformat(timespec="seconds")
    d["arrivalDateTime"] = end.isoformat(timespec="seconds")
    return d


def test_departed_trains_are_dropped_and_counted(monkeypatch, capsys):
    deps = [
        _dep_at("gone-1", -120),
        _dep_at("gone-2", -10),
        _dep_at("next", 15),
        _dep_at("later", 75),
    ]
    c = FakeClient({"OUT": deps})
    s = Script(T, "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s, base_cfg(roundtrip=False, time_leave=_shift(-10))) is True
    _, rows, default_index, _ = s.lists[0]
    assert len(rows) == 2 and rows[0].startswith(_shift(15))
    assert default_index == 0  # the closest *upcoming* one
    out = capsys.readouterr().out
    assert "  2 already departed\n" in out
    assert [call for call in c.calls if call[0] == "offers"] == [("offers", "next")]


def test_all_departed_ends_the_run(monkeypatch, capsys):
    c = FakeClient({"OUT": [_dep_at("gone", -60)]})
    s = Script(T, "", "", "n")
    wire(monkeypatch, s)
    assert run(c, s, base_cfg(roundtrip=False)) is False
    assert not any(call[0] == "offers" for call in c.calls)
    assert (
        " ● no departures left for Göteborg Central → Stockholm Central today"
        in capsys.readouterr().out
    )


def test_the_clock_is_read_after_the_questions(monkeypatch, capsys):
    # A user who takes minutes over the prompts must not be shown a train
    # that left while they answered: the filter reads the clock afterwards.
    c = FakeClient({"OUT": [_dep_at("gone-while-asking", 10), _dep_at("next", 45)]})
    s = Script(T, "", "", "n", "", True)
    wire(monkeypatch, s)
    clock = iter([NOW])
    monkeypatch.setattr(journey, "sweden_now", lambda: next(clock, NOW + timedelta(minutes=30)))
    assert run(c, s, base_cfg(roundtrip=False)) is True
    _, rows, _, _ = s.lists[0]
    assert len(rows) == 1 and rows[0].startswith(_shift(45))
    assert "  1 already departed\n" in capsys.readouterr().out
    assert [call for call in c.calls if call[0] == "offers"] == [("offers", "next")]


# --- overlaps ------------------------------------------------------------------------


def _held(number, dep_time, arr_time, day=None, arr_day=None):
    day = day or D
    return journey._Held(
        number=number,
        day=day,
        origin="Göteborg Central",
        dest="Stockholm Central",
        dep=to_sweden(f"{day}T{dep_time}:00+02:00"),
        arr=to_sweden(f"{arr_day or day}T{arr_time}:00+02:00") if arr_time else None,
    )


def test_overlap_needs_an_intersection_on_the_same_day_or_across_midnight():
    row = dep("x", D, "06:59", "11:36")
    assert journey._overlap(row, [_held("A", "07:30", "09:10")]).number == "A"
    assert journey._overlap(row, [_held("B", "11:36", "13:00")]) is None  # touching edge
    assert journey._overlap(row, [_held("C", "07:00", "08:00", day=D2)]) is None
    assert journey._overlap(row, [_held("D", "07:00", None)]).number == "D"  # instant inside
    assert journey._overlap(row, [_held("E", "11:36", None)]) is None
    assert journey._overlap(row, [_held("F", "06:59", None)]).number == "F"  # same instant
    assert journey._overlap({"departureDateTime": "soon"}, [_held("A", "07:30", "09:10")]) is None
    # a held night train leaving D at 23:50 runs into the D2 morning
    night = _held("N", "23:50", "06:00", arr_day=D2)
    assert journey._overlap(dep("x", D2, "00:10", "06:30"), [night]).number == "N"
    # and the reverse: a candidate over midnight meets a segment held on D2
    overnight = dep("y", D, "23:00", "06:00")
    overnight["arrivalDateTime"] = f"{D2}T06:00:00+02:00"
    assert journey._overlap(overnight, [_held("M", "01:00", "02:00", day=D2)]).number == "M"


def test_overlapping_rows_say_which_booking(monkeypatch):
    c = FakeClient({"OUT": [OUT[0], NO_CLASS, OUT[2]]})  # 06:30 ok, 07:00 no class, 07:30 ok
    c.bookings_list = [held("HELD1", D, dep_time="06:59", arr_time="11:36")]
    s = Script(D, "", "", "n", 0, True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    _, rows, _, rejected = s.lists[0]
    assert re.search(r"2 class calm\s+overlaps HELD1 · 06:59–11:36$", rows[0])
    assert re.search(r"—\s+overlaps HELD1 · 06:59–11:36$", rows[1])  # not "no seats"
    assert re.search(r"2 class calm\s+overlaps HELD1 · 06:59–11:36$", rows[2])
    assert rejected == ["overlaps booking HELD1 · pick another"]


def test_sj_conflict_at_offer_time_is_said(monkeypatch, capsys):
    c = FakeClient({"OUT": OUT}, {"o-best": offers(conflicts=["HELD1", "HELD2"])})
    s = Script(D, "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True  # the offer exists, the pick stands
    assert (
        " ! outbound: SJ reports a conflict with booking HELD1, HELD2\n" in capsys.readouterr().out
    )


def test_a_night_train_from_the_evening_before_overlaps_the_morning(monkeypatch, capsys):
    prev = (FUTURE - timedelta(days=1)).isoformat()
    c = FakeClient({"OUT": [dep("early", D, "05:00", "07:00")]})
    c.bookings_list = [held("NIGHT", prev, dep_time="23:50", arr_time="06:00", arr_day=D)]
    s = Script(D, "", "", "n", "", True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    _, rows, _, _ = s.lists[0]
    assert re.search(r"2 class calm\s+overlaps NIGHT · 23:50–06:00$", rows[0])
    # the summary names tickets on the chosen dates; this one is on the day before
    assert "you hold booking NIGHT" not in capsys.readouterr().out


def test_the_note_names_the_held_route_when_it_differs():
    row = dep("x", D, "06:59", "11:36")
    other = journey._Held(
        number="HELD1",
        day=D,
        origin="Uppsala Central",
        dest="Stockholm Central",
        dep=to_sweden(f"{D}T06:59:00+02:00"),
        arr=to_sweden(f"{D}T11:36:00+02:00"),
    )
    rows = journey._departure_rows(
        [row], "Göteborg Central → Stockholm Central", base_cfg()["search_parameters"], [other]
    )
    assert rows[0]["note"] == "overlaps HELD1 · Uppsala Central → Stockholm Central 06:59–11:36"


def test_the_return_list_gets_its_own_overlaps(monkeypatch):
    in_deps = [dep("i-early", D2, "16:50", "18:30"), dep("i-best", D2, "17:22", "21:53")]
    c = FakeClient({"OUT": OUT, "IN": in_deps})
    c.bookings_list = [
        held("H1", D),
        held(
            "H2",
            D2,
            dep_time="17:22",
            arr_time="21:53",
            origin="Stockholm Central",
            dest="Göteborg Central",
        ),
    ]
    s = Script(D, "", "", "y", D2, 0, 0, True)
    wire(monkeypatch, s)
    assert run(c, s) is True
    assert re.search(r"2 class calm\s+overlaps H1 · 06:59–11:36$", s.lists[0][1][0])
    assert re.search(r"2 class calm\s+overlaps H2 · 17:22–21:53$", s.lists[1][1][1])
