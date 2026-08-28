"""Tests for handle_list_bookings, including the --seat-details modifier."""

from datetime import timedelta

from sj_cli.booking import handle_list_bookings
from sj_cli.dates import sweden_now
from tests.fakes import FakeClient, seatmap

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
