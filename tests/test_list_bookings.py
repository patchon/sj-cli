"""Tests for handle_list_bookings, including the --seat-details and --since modifiers."""

from datetime import date, timedelta

from sj_cli import output
from sj_cli.booking import handle_list_bookings
from sj_cli.dates import sweden_now
from tests.fakes import FakeClient, TtyOut, seatmap

# Computed from the real clock at test-run time so these dates are never
# accidentally in the past (a past leg is deliberately excluded from the
# seat-detail fetch — see test_a_departed_segment_is_not_fetched).
FUTURE_DATE = (sweden_now() + timedelta(days=30)).date().isoformat()
FUTURE_DATE_2 = (sweden_now() + timedelta(days=31)).date().isoformat()
PAST_DATE = (sweden_now() - timedelta(days=2)).date().isoformat()


def _segment(date, tag, dep_time="06:00", seat_map=True):
    return {
        "direction": "OUTBOUND",
        "serviceIdentifier": f"SI-{tag}",
        "seatMapAvailable": seat_map,
        "seatMapSearchId": f"SM-{tag}",
        "departureDateTime": f"{date}T{dep_time}:00+02:00",
        "arrivalDateTime": f"{date}T{dep_time[:2]}:59:00+02:00",
        "departureStation": {"name": "Göteborg Central", "uicStationCode": "740000002"},
        "arrivalStation": {"name": "Stockholm Central", "uicStationCode": "740000001"},
        "requiredProducts": [{"seat": {"number": "39", "carriageNumber": "3"}}],
    }


def _booking_item(number, segments):
    return {
        "bookingId": f"ID-{number}",
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED",
            "journeys": [{"segments": segments}],
        },
    }


def _cancelled_booking_item(number, segments):
    """A fully cancelled booking: no journeys, its legs in cancelledJourneys instead."""
    return {
        "bookingId": f"ID-{number}",
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED_CANCELLED",
            "journeys": [],
            "cancelledJourneys": [{"segments": segments}],
        },
    }


def test_seat_details_appends_words_and_fetches_the_seat_map(capsys):
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(assigned=("3", "39"), assigned_codes=["IDR", "TABLE", "WINDOW"])
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert c.calls.count(("seatmap", "ID-NUM1", "SM-D1")) == 1
    assert "carriage 3 seat 39 · table, window, forward" in capsys.readouterr().out


def test_without_the_flag_get_seatmap_is_never_called(capsys):
    c = FakeClient()
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=False)

    assert not any(call[0] == "seatmap" for call in c.calls)
    assert "carriage 3 seat 39" in capsys.readouterr().out  # the plain listing is unaffected


def test_a_raising_seatmap_leaves_the_plain_seat_and_warns_once(capsys):
    c = FakeClient()
    c.seatmap_error = RuntimeError("map exploded")
    c.bookings_list = [
        _booking_item(
            "NUM1",
            [_segment(FUTURE_DATE, "D1"), _segment(FUTURE_DATE_2, "D2", dep_time="09:00")],
        )
    ]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert c.calls.count(("seatmap", "ID-NUM1", "SM-D1")) == 1
    assert c.calls.count(("seatmap", "ID-NUM1", "SM-D2")) == 1
    out = capsys.readouterr().out
    # Both cards still rendered, plain seat cell kept (no " · " suffix added)
    assert out.count("carriage 3 seat 39") == 2
    assert "carriage 3 seat 39 ·" not in out
    # Exactly one aggregated warning, not one per leg
    assert out.count("seat details unavailable") == 1
    assert "! seat details unavailable for 2 leg(s)" in out


def test_a_departed_segment_is_not_fetched(capsys):
    c = FakeClient()
    c.bookings_list = [_booking_item("NUM1", [_segment(PAST_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert not any(call[0] == "seatmap" for call in c.calls)
    assert "seat details unavailable" not in capsys.readouterr().out


def test_seat_details_reports_single_for_a_single_seat(capsys):
    c = FakeClient()
    # Row 1 of a 2+1 carriage: seat 39 sits alone on one side of the aisle
    # (the widest ypos gap), seats 41/42 are the paired seats on the other.
    c.seatmaps["SM-D1"] = seatmap(
        free=(
            ("39", ["WINDOW"], True, 1, 5),
            ("41", ["AISLE"], True, 1, 108),
            ("42", ["WINDOW"], True, 1, 144),
        ),
        assigned=("3", "39"),
    )
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert "carriage 3 seat 39 · single, window, forward" in capsys.readouterr().out


def test_seat_details_does_not_report_single_for_a_paired_seat(capsys):
    c = FakeClient()
    # Seat 39 is paired with seat 40 on one side of the aisle; seat 41 is
    # the carriage's actual single, alone on the other side.
    c.seatmaps["SM-D1"] = seatmap(
        free=(
            ("39", ["AISLE"], True, 1, 108),
            ("40", ["WINDOW"], True, 1, 144),
            ("41", ["WINDOW"], True, 1, 5),
        ),
        assigned=("3", "39"),
    )
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, forward" in out
    assert "single" not in out


def test_seat_details_falls_back_to_property_codes_when_layout_lookup_fails(capsys):
    # Seat 39 is assigned but is not present in this map's carriage detail
    # (the default `free` only has seat 70) — the carriage-layout lookup
    # must fail cleanly and --seat-details must still render from the
    # assigned seat's own codes (never "single": codes alone cannot say).
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(assigned=("3", "39"), assigned_codes=["IDR", "AISLE"])
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert "carriage 3 seat 39 · aisle, forward" in capsys.readouterr().out


def test_the_same_map_is_fetched_once_when_two_legs_share_it():
    c = FakeClient()
    c.seatmaps["SM-SHARED"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    segments = [
        _segment(FUTURE_DATE, "SHARED", dep_time="06:00"),
        _segment(FUTURE_DATE_2, "SHARED", dep_time="09:00"),
    ]
    c.bookings_list = [_booking_item("NUM1", segments)]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert c.calls.count(("seatmap", "ID-NUM1", "SM-SHARED")) == 1


def test_seat_details_counts_the_legs_in_the_spinner(monkeypatch):
    out = TtyOut()
    monkeypatch.setattr(output.sys, "stdout", out)
    c = FakeClient()
    for tag in ("D1", "D2", "D3"):
        c.seatmaps[f"SM-{tag}"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    segments = [
        _segment(FUTURE_DATE, "D1"),
        _segment(FUTURE_DATE, "D2", dep_time="09:00"),
        _segment(FUTURE_DATE_2, "D3"),
    ]
    c.bookings_list = [_booking_item("NUM1", segments)]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    text = out.getvalue()
    assert "fetching seat details · 1 of 3" in text
    assert "fetching seat details · 2 of 3" in text
    assert "fetching seat details · 0 of 3" not in text  # nothing to report before the first leg
    assert "fetching seat details · 3 of 3" not in text  # the last leg ends the step instead
    assert sum(1 for call in c.calls if call[0] == "seatmap") == 3  # fetch count unchanged


def test_seat_details_counts_legs_not_fetches(monkeypatch):
    out = TtyOut()
    monkeypatch.setattr(output.sys, "stdout", out)
    c = FakeClient()
    c.seatmaps["SM-SHARED"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    c.seatmaps["SM-D3"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    segments = [
        _segment(FUTURE_DATE, "SHARED"),
        _segment(FUTURE_DATE, "SHARED", dep_time="09:00"),  # same map: served from the cache
        _segment(FUTURE_DATE_2, "D3"),
    ]
    c.bookings_list = [_booking_item("NUM1", segments)]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    # the cached leg still ticks: the count follows the legs, not the requests
    assert "fetching seat details · 2 of 3" in out.getvalue()
    assert c.calls.count(("seatmap", "ID-NUM1", "SM-SHARED")) == 1


def test_seat_details_for_a_single_leg_shows_no_count(monkeypatch):
    out = TtyOut()
    monkeypatch.setattr(output.sys, "stdout", out)
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    assert "fetching seat details ·" not in out.getvalue()


# --- the "could take N" hint (seat_preference ranked lists only) -----------

# Row 1 of a 2+1 carriage: seat 47 sits alone on one side of the aisle
# (single, window); the assigned seat 39 (aisle) and seat 40 are the paired
# seats on the other side, all in carriage 3 to match _segment()'s default
# requiredProducts seat.
_HINT_MAP_SEATS: tuple[tuple[str, list[str], bool, int, int], ...] = (
    ("47", ["WINDOW"], True, 1, 5),
    ("39", ["AISLE"], False, 1, 108),
    ("40", [], True, 1, 144),
)


def test_seat_preference_hint_names_a_strictly_better_free_seat(capsys):
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(free=_HINT_MAP_SEATS, assigned=("3", "39"))
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=["single", "window"])

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward · could take 47 · single, window" in out


def test_seat_preference_hint_is_silent_when_current_seat_is_already_best(capsys):
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(
        free=(("39", ["AISLE"], True), ("47", ["WINDOW"], True)),
        assigned=("3", "39"),
    )
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=["aisle"])

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, forward" in out
    assert "could take" not in out


def test_seat_preference_hint_is_silent_when_the_only_free_seat_ranks_worse(capsys):
    """
    The anti-regression case: the seat the passenger already holds is not
    itself offered as free (a real map never offers the passenger's own
    seat back to them), so the only free candidate is a plain aisle seat —
    worse than the window seat already assigned. A naive
    "best_seat(...) != current" identity check would flag seat 47 as an
    improvement simply because it differs from seat 39; ranking by
    seats.rank must correctly say it is not.
    """
    c = FakeClient()
    m = seatmap(
        free=(("39", ["WINDOW"], True), ("47", ["AISLE"], True)),
        assigned=("3", "39"),
    )
    m["seatsPossibleToSelect"]["3"] = ["47"]  # only 47 is actually offered
    c.seatmaps["SM-D1"] = m
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=["window"])

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · window, forward" in out
    assert "could take" not in out


def test_seat_preference_ask_never_shows_a_hint(capsys):
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(free=_HINT_MAP_SEATS, assigned=("3", "39"))
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference="ask")

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward" in out
    assert "could take" not in out


def test_no_seat_preference_never_shows_a_hint_and_does_not_crash(capsys):
    # seat_preference omitted entirely, as when [search_parameters] is
    # absent from an otherwise --list-bookings-valid config: must not crash
    # and must not hint, even though a strictly better seat exists on this map.
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(free=_HINT_MAP_SEATS, assigned=("3", "39"))
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward" in out
    assert "could take" not in out


# --- "· nothing free": the map offers no seat at all ------------------------

_WISHES = ["avoid table", "avoid easy access", "single", "window"]


def _full_map(assigned_in_layout=True):
    """A map whose only layout seat is the assigned one and whose selectable lists are empty."""
    layout = (("39", ["AISLE"], False),) if assigned_in_layout else ()
    sm = seatmap(
        free=layout,
        assigned=("3", "39"),
        assigned_codes=["AISLE", "ODR"],
        extra=(("5", (), ("SECOND",)),),
    )
    sm["seatsPossibleToSelect"] = {"3": [], "5": []}
    return sm


def test_nothing_free_is_said_when_the_map_offers_no_seat(capsys):
    c = FakeClient()
    c.seatmaps["SM-D1"] = _full_map()
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=_WISHES)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward · nothing free in this class" in out
    assert "seat details unavailable" not in out  # an empty map is a reported state, not a failure


def test_nothing_free_stays_silent_for_an_unlocatable_seat(capsys):
    # The assigned seat is missing from the layout: the words come from the
    # fallback path and the map is not understood well enough to judge it.
    c = FakeClient()
    c.seatmaps["SM-D1"] = _full_map(assigned_in_layout=False)
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=_WISHES)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward" in out
    assert "nothing free" not in out


def test_nothing_free_needs_a_ranked_preference(capsys):
    for preference in ("ask", None):
        c = FakeClient()
        c.seatmaps["SM-D1"] = _full_map()
        c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

        handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=preference)

        out = capsys.readouterr().out
        assert "carriage 3 seat 39 · aisle, backward" in out
        assert "nothing free" not in out


def test_free_seats_that_do_not_outrank_stay_silent(capsys):
    # A free seat exists but is no better: neither a suggestion nor "nothing free".
    # (A real map never offers the passenger their own seat back, so seat 39
    # stays in the layout — assigned_seat() must still find it — but only
    # seat 40 is actually offered.)
    c = FakeClient()
    m = seatmap(free=(("39", ["AISLE"], False), ("40", ["AISLE"], True)), assigned=("3", "39"))
    m["seatsPossibleToSelect"]["3"] = ["40"]
    c.seatmaps["SM-D1"] = m
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=_WISHES)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward" in out
    assert "could take" not in out
    assert "nothing free" not in out


def test_a_locked_map_says_it_cannot_be_changed(capsys):
    # canChangeSeat: false — SJ refuses a re-seat, so neither a suggestion nor
    # "nothing free" would name the cause (the write paths refuse it too).
    c = FakeClient()
    sm = _full_map()
    sm["canChangeSeat"] = False
    c.seatmaps["SM-D1"] = sm
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=_WISHES)

    out = capsys.readouterr().out
    assert "carriage 3 seat 39 · aisle, backward · cannot be changed" in out
    assert "nothing free" not in out


def test_a_departed_segment_is_not_fetched_even_with_seat_preference(capsys):
    c = FakeClient()
    c.bookings_list = [_booking_item("NUM1", [_segment(PAST_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=["window"])

    assert not any(call[0] == "seatmap" for call in c.calls)
    out = capsys.readouterr().out
    assert "seat details unavailable" not in out
    assert "could take" not in out


def test_a_raising_seatmap_leaves_the_plain_seat_with_seat_preference_too(capsys):
    c = FakeClient()
    c.seatmap_error = RuntimeError("map exploded")
    c.bookings_list = [_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, seat_preference=["window"])

    out = capsys.readouterr().out
    assert "carriage 3 seat 39" in out
    assert "carriage 3 seat 39 ·" not in out
    assert "could take" not in out
    assert "seat details unavailable for 1 leg(s)" in out


def test_no_hint_when_the_free_seat_meets_exactly_the_same_wishes():
    """
    Seat 19 and seat 15 both single+window+forward: moving 19 -> 15 gains the
    traveller nothing, so the hint must stay silent. `rank` prefers 15 on its
    seat-number tie-break, which is why `_seat_hint` compares `wish_rank`.
    """
    from sj_cli.booking import _seat_hint
    from sj_cli.seats import assigned_seat
    from tests.fakes import seatmap

    # a 2+1 carriage: singles at ypos 5, pairs at 108/144
    layout = (
        ("15", ["WINDOW"], True, 1, 5),
        ("16", ["AISLE"], True, 1, 108),
        ("17", ["WINDOW"], True, 1, 144),
        ("19", ["WINDOW"], True, 2, 5),
        ("20", ["AISLE"], True, 2, 108),
        ("21", ["WINDOW"], True, 2, 144),
    )
    # sitting in 19, only 15 is free — same wishes met, lower number
    m = seatmap(free=layout, assigned=("3", "19"))
    m["seatsPossibleToSelect"] = {"3": ["15"]}
    here = assigned_seat(m)
    assert here is not None and here["single"] and here["forward"]
    assert _seat_hint(here, m, ["single", "forward"]) == ""

    # now free a paired seat as well: still no gain, so still silent
    m["seatsPossibleToSelect"] = {"3": ["15", "21"]}
    assert _seat_hint(here, m, ["single", "forward"]) == ""


def test_since_renders_cancelled_journeys_with_a_marker_and_counts_them(capsys):
    c = FakeClient()
    c.bookings_list = [_cancelled_booking_item("NUM1", [_segment(PAST_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, since=date.fromisoformat(PAST_DATE))

    assert c.include_cancelled_calls == [True]  # asked with includeCancelledBookings on
    out = capsys.readouterr().out
    assert "NUM1   cancelled" in out
    assert "1 day(s) · 1 booking(s) · 1 in the past · 1 cancelled" in out


def test_without_since_cancelled_journeys_are_not_shown(capsys):
    c = FakeClient()
    c.bookings_list = [_cancelled_booking_item("NUM1", [_segment(PAST_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {})

    assert c.include_cancelled_calls == [False]  # asked with includeCancelledBookings off
    out = capsys.readouterr().out
    assert "cancelled" not in out
    assert "no bookings found" in out


def test_cancelled_legs_are_never_sent_to_seat_details(capsys):
    # seatMapAvailable/seatMapSearchId are on the segment and it has not
    # departed, so a live leg would qualify — a cancelled one must not.
    c = FakeClient()
    c.bookings_list = [_cancelled_booking_item("NUM1", [_segment(FUTURE_DATE, "D1")])]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, since=date.fromisoformat(PAST_DATE))

    assert not any(call[0] == "seatmap" for call in c.calls)
    assert "NUM1   cancelled" in capsys.readouterr().out


def _full_segment(tag):
    """Like _segment, but with every other column filled so none falls back to an em dash."""
    seg = _segment(FUTURE_DATE, tag)
    seg["duration"] = "PT4H37M"
    seg["productFamily"] = {
        "salesCategoryComfort": "SECOND_CALM",
        "salesCategoryFlexibility": "FULLFLEX",
    }
    seg["serviceBrandNameDescription"] = "X 2000"
    seg["publicServiceName"] = "520"
    return seg


def test_since_mixed_listing_marks_only_the_cancelled_leg(capsys):
    # One active and one cancelled booking on the same day: the active leg's
    # cancelled cell must be dropped rather than shown as an em dash (the
    # normal placeholder for a missing value elsewhere), the cancelled leg
    # must end "NUM2   cancelled", and only the active leg's seat map is
    # fetched under --seat-details.
    c = FakeClient()
    c.seatmaps["SM-D1"] = seatmap(assigned=("3", "39"), assigned_codes=["WINDOW"])
    c.bookings_list = [
        _booking_item("NUM1", [_full_segment("D1")]),
        _cancelled_booking_item("NUM2", [_full_segment("D2")]),
    ]

    handle_list_bookings(c, "TOKEN", {}, seat_details=True, since=date.fromisoformat(PAST_DATE))

    assert c.calls.count(("seatmap", "ID-NUM1", "SM-D1")) == 1
    assert not any(call[0] == "seatmap" and call[2] == "SM-D2" for call in c.calls)
    out = capsys.readouterr().out
    assert "NUM2   cancelled" in out
    assert "—" not in out
    assert "1 cancelled" in out


def test_the_hint_appears_when_a_free_seat_meets_more_wishes():
    from sj_cli.booking import _seat_hint
    from sj_cli.seats import assigned_seat
    from tests.fakes import seatmap

    layout = (
        ("15", ["WINDOW"], True, 1, 5),
        ("16", ["AISLE"], True, 1, 108),
        ("17", ["WINDOW"], True, 1, 144),
    )
    # sitting in the paired seat 17, the single 15 is free: a real improvement
    m = seatmap(free=layout, assigned=("3", "17"))
    m["seatsPossibleToSelect"] = {"3": ["15"]}
    here = assigned_seat(m)
    assert here is not None and not here["single"]
    assert (
        _seat_hint(here, m, ["single", "forward"]) == " · could take 15 · single, window, forward"
    )
