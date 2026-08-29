"""
Tests for handle_upgrade_class: the preview and the release-and-re-book write path.

The preview half asserts nothing was written (no provisional created,
checked out, cancelled or seat-updated) — all it may do is search (without
the travel pass) and read offers. The write half asserts the order and the
scope of what it does write: never a cancel without a probe that found
seats, never a cancel of anything but the one journey being upgraded, and
never a cancel that is not followed straight away by its own re-book.
"""

from datetime import timedelta

import pytest

from sj_cli import booking, output
from sj_cli.booking import handle_upgrade_class
from sj_cli.dates import sweden_now
from tests.fakes import FakeClient, base_cfg, dep, offers

# Computed from the real clock at test-run time so these are never
# accidentally in the past (see tests/test_change_seat.py for the same idiom).
FUTURE_DATE = (sweden_now() + timedelta(days=30)).date().isoformat()
FUTURE_DATE_2 = (sweden_now() + timedelta(days=31)).date().isoformat()
PAST_DATE = (sweden_now() - timedelta(days=2)).date().isoformat()

ORIGIN, ORIGIN_ID = "Göteborg Central", "740000002"
DEST, DEST_ID = "Stockholm Central", "740000001"

_WRITE_TAGS = {"create", "add", "customer", "checkout", "seats", "cancel", "finalize"}


def _segment(
    date,
    dep_time="06:00",
    train="520",
    comfort_code="SECOND",
    service_id=None,
    reverse=False,
):
    """
    A booked segment on the configured route, in comfort_code, at dep_time.

    service_id is what a cancel payload names the journey by; pass "" for a
    segment the API gave none, which cannot be released at all.
    """
    origin, dest = (DEST, ORIGIN) if reverse else (ORIGIN, DEST)
    origin_id, dest_id = (DEST_ID, ORIGIN_ID) if reverse else (ORIGIN_ID, DEST_ID)
    return {
        "direction": "INBOUND" if reverse else "OUTBOUND",
        "departureDateTime": f"{date}T{dep_time}:00+02:00",
        "arrivalDateTime": f"{date}T{dep_time[:2]}:59:00+02:00",
        "departureStation": {"uicStationCode": origin_id, "name": origin},
        "arrivalStation": {"uicStationCode": dest_id, "name": dest},
        "publicServiceName": train,
        "serviceName": train,
        "serviceBrandNameDescription": "X 2000",
        "productFamily": {"salesCategoryComfort": comfort_code},
        "serviceIdentifier": f"SI-{train}" if service_id is None else service_id,
        "passengers": [{"id": "p1"}],
    }


def _booking(number, date, **seg_kwargs):
    """One active booking, one journey, one segment."""
    return {
        "booking": {
            "bookingNumber": number,
            "bookingId": f"ID-{number}",
            "bookingStatus": "CONFIRMED",
            "journeys": [{"segments": [_segment(date, **seg_kwargs)]}],
        }
    }


def _key(date):
    """The FakeClient search-id key for a pass-free one-way search on this route/date."""
    return f"{ORIGIN}->{DEST}@{date}"


def _assert_no_writes(c):
    assert not any(call[0] in _WRITE_TAGS for call in c.calls), c.calls


def test_fallback_leg_with_purchasable_seats_is_worth_trying(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=345, second_price=0)  # calm sold, paid

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    out = capsys.readouterr().out
    assert "holds 2 class" in out
    assert "seats exist" in out
    assert "1 leg(s) not in 2 class calm" in out and "1 worth trying" in out
    _assert_no_writes(c)


def test_fallback_leg_with_no_seats_is_not_possible(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=None, second_price=0)  # calm not sold at all

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    out = capsys.readouterr().out
    assert "no seats on this departure" in out
    assert "seats exist" not in out
    assert "1 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


def test_leg_already_in_configured_class_is_not_reported(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND_CALM")]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # never probed: nothing to upgrade
    out = capsys.readouterr().out
    assert "NUM1" not in out  # no card at all
    assert "0 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


def test_probe_uses_pass_free_search_not_the_travel_pass():
    # The whole point of this feature: a search WITH the travel pass reports
    # every class unavailable on a departure the account already holds, so
    # the probe must never pass a travel_pass_id.
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", FUTURE_DATE, comfort_code="SECOND")]
    c.departures[_key(FUTURE_DATE)] = [dep("520", FUTURE_DATE, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=0, second_price=0)

    cfg = base_cfg(comfort_class="2 class calm")
    handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert c.search_tp_ids == [None]
    search_calls = [call for call in c.calls if call[0] == "search"]
    assert search_calls == [("search", ORIGIN, DEST, FUTURE_DATE, None)]


def test_probe_matches_the_booked_departure_not_the_closest_to_time_leave(capsys):
    # Two departures that day; time_leave (09:00) is much closer to the decoy
    # (08:55, a different train) than to the one actually booked (06:00). A
    # probe that picked "closest to time_leave" instead of the booked
    # departure would read the decoy's offers and wrongly call this worth
    # trying.
    c = FakeClient()
    c.bookings_list = [
        _booking("NUM1", FUTURE_DATE, dep_time="06:00", train="520", comfort_code="SECOND")
    ]
    c.departures[_key(FUTURE_DATE)] = [
        dep("520", FUTURE_DATE, "06:00", "07:46"),  # the actual booked departure
        dep("999", FUTURE_DATE, "08:55", "10:41"),  # closer to time_leave, a different train
    ]
    c.offers_by_dep["520"] = offers(calm_price=None, second_price=0)  # booked one: no seats
    c.offers_by_dep["999"] = offers(calm_price=430, second_price=0)  # decoy: seats exist

    cfg = base_cfg(comfort_class="2 class calm", time_leave="09:00")
    handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    offer_calls = [call for call in c.calls if call[0] == "offers"]
    assert offer_calls == [("offers", "520")]
    out = capsys.readouterr().out
    assert "no seats on this departure" in out
    assert "seats exist" not in out


def test_past_leg_is_skipped(capsys):
    c = FakeClient()
    c.bookings_list = [_booking("NUM1", PAST_DATE, comfort_code="SECOND")]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[PAST_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # never probed: already departed
    out = capsys.readouterr().out
    assert "no bookings found for the given dates" in out
    _assert_no_writes(c)


def test_leg_on_a_date_outside_the_given_range_is_skipped(capsys):
    # One booking, two travel days: FUTURE_DATE (in range, already the
    # configured class) and FUTURE_DATE_2 (out of range, a fallback class
    # that would print a card and get probed if the day scoping leaked it in).
    booking = {
        "booking": {
            "bookingNumber": "NUM1",
            "bookingId": "ID-NUM1",
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {"segments": [_segment(FUTURE_DATE, train="520", comfort_code="SECOND_CALM")]},
                {"segments": [_segment(FUTURE_DATE_2, train="777", comfort_code="SECOND")]},
            ],
        }
    }
    c = FakeClient()
    c.bookings_list = [booking]

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True)

    assert ok is True
    assert c.search_tp_ids == []  # neither leg needed a probe
    out = capsys.readouterr().out
    assert "777" not in out
    assert "0 leg(s) not in 2 class calm" in out and "0 worth trying" in out
    _assert_no_writes(c)


@pytest.mark.parametrize("dates", [[], ["2020-01-01"]])  # empty selection / date well in the past
def test_nothing_matched_is_reported_red_but_not_a_failure(capsys, dates):
    c = FakeClient()
    cfg = base_cfg(comfort_class="2 class calm")

    ok = handle_upgrade_class(c, "tok", cfg, dates=dates, dry_run=True)

    assert ok is True
    if dates:
        out = capsys.readouterr().out
        assert "no bookings found for the given dates" in out
    _assert_no_writes(c)


# --- the write path: release the ticket, then re-book the same departure -------
#
# Shared shape for these: an existing booking OLD1 in plain 2 class on the
# 06:00 train, a pass-free probe that finds calm seats for sale, and the same
# departure offered again — under a *different* departureId — to the search
# made with the travel pass, so a test can serve the probe and the re-book
# different answers for the one departure.

TP_ID = "TP-PRODUCT"
TP_TOKEN = "TP-PASSENGER"


def _pass_dep(date, dep_time="06:00", train="520", arr="07:46"):
    """The booked departure as the travel-pass search returns it: same train, own id."""
    departure = dep(train, date, dep_time, arr)
    departure["departureId"] = f"PASS-{train}"
    return departure


def _upgradeable(c, *, date=FUTURE_DATE, held="SECOND", pass_offers=None, booking=None):
    """Arm a FakeClient so one leg is in a fallback class and probes as worth trying."""
    c.bookings_list = [booking or _booking("OLD1", date, comfort_code=held)]
    c.departures[_key(date)] = [dep("520", date, "06:00", "07:46")]
    c.offers_by_dep["520"] = offers(calm_price=345, second_price=0)  # SJ sells calm
    c.departures["OUT"] = [_pass_dep(date)]
    c.offers_by_dep["PASS-520"] = (
        offers(calm_price=0, second_price=0) if pass_offers is None else pass_offers
    )
    return c


def _run_upgrade(c, cfg, monkeypatch, *, answer="y", dates=(FUTURE_DATE,)):
    """Run the real (non-dry) path at a fake terminal, answering the one prompt."""
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)
    asked = []

    def _ask(text):
        asked.append(text)
        return answer

    monkeypatch.setattr(booking, "ask", _ask)
    monkeypatch.setattr(output, "ask", _ask)
    ok = handle_upgrade_class(
        c,
        "tok",
        cfg,
        dates=list(dates),
        dry_run=False,
        tp_product_id=TP_ID,
        tp_token_id=TP_TOKEN,
    )
    return ok, asked


def _never_prompts(monkeypatch, reason):
    """Fail the test if anything asks a question — with a message naming why."""

    def _refuse(_t):
        pytest.fail(reason)

    monkeypatch.setattr(booking, "ask", _refuse)
    monkeypatch.setattr(output, "ask", _refuse)


def test_upgrade_releases_one_journey_then_rebooks_the_same_departure(monkeypatch, capsys):
    c = _upgradeable(FakeClient())
    # A decoy far closer to time_leave than the booked 06:00 train: re-booking
    # "the closest departure" instead of the released one would move the
    # traveller to another train without saying so.
    c.departures["OUT"].append(_pass_dep(FUTURE_DATE, dep_time="08:55", train="999", arr="10:41"))
    c.offers_by_dep["PASS-999"] = offers(calm_price=0, second_price=0)

    cfg = base_cfg(comfort_class="2 class calm", time_leave="09:00")
    ok, asked = _run_upgrade(c, cfg, monkeypatch)

    assert ok is True
    # One confirmation for the whole run, saying plainly what it costs.
    assert asked == ["upgrade 1 leg(s) to 2 class calm, cancelling each ticket first? [y/n]: "]

    # Exactly the one journey, and only it, is cancelled.
    assert c.cancel_payloads == [
        ("ID-OLD1", [{"serviceIdentifier": "SI-520", "passengerIds": ["p1"]}])
    ]

    tags = [call[0] for call in c.calls]
    # cancel → confirm → re-book, in that order and with nothing in between.
    assert tags.index("cancel") < tags.index("finalize") < tags.index("create")
    assert tags.count("cancel") == 1 and tags.count("create") == 1
    assert "checkout" in tags

    # The re-book searched WITH the pass (the probe before it, without).
    assert c.search_tp_ids == [None, TP_ID]
    # ...and asked the released departure for its offer, never the decoy.
    assert ("offers", "PASS-520") in c.calls
    assert ("offers", "PASS-999") not in c.calls

    out = capsys.readouterr().out
    assert "✓ creating booking with the same departure at " in out
    assert "upgraded to 2 class calm · new booking NUM1" in out
    assert "1 leg(s) attempted" in out and "1 upgraded to 2 class calm" in out


def test_the_other_journey_of_a_roundtrip_booking_is_left_alone(monkeypatch, capsys):
    # One booking, two journeys the same day: the outbound in plain 2 class
    # (to be upgraded) and the return already calm (nothing to do). Cancelling
    # the booking rather than the journey would throw the return away too.
    roundtrip = {
        "booking": {
            "bookingNumber": "OLD1",
            "bookingId": "ID-OLD1",
            "bookingStatus": "CONFIRMED",
            "journeys": [
                {"segments": [_segment(FUTURE_DATE, train="520", comfort_code="SECOND")]},
                {
                    "segments": [
                        _segment(
                            FUTURE_DATE,
                            dep_time="17:22",
                            train="543",
                            comfort_code="SECOND_CALM",
                            reverse=True,
                        )
                    ]
                },
            ],
        }
    }
    c = _upgradeable(FakeClient(), booking=roundtrip)

    cfg = base_cfg(comfort_class="2 class calm")
    ok, asked = _run_upgrade(c, cfg, monkeypatch)

    assert ok is True
    ((_, payload),) = c.cancel_payloads
    assert [entry["serviceIdentifier"] for entry in payload] == ["SI-520"]
    assert "SI-543" not in str(c.cancel_payloads)
    # Only the outbound was listed for upgrading; the calm return never was.
    assert asked == ["upgrade 1 leg(s) to 2 class calm, cancelling each ticket first? [y/n]: "]
    assert "543" not in capsys.readouterr().out


def test_falling_back_to_the_class_it_started_in_is_reported_as_no_gain(monkeypatch, capsys):
    # The probe was right that SJ sells calm, but the pass gets no calm offer
    # once the ticket is released: the class chain lands on plain 2 class
    # again. A ticket exists, so this is not a failure — just no gain.
    c = _upgradeable(FakeClient(), pass_offers=offers(calm_price=None, second_price=0))

    cfg = base_cfg(comfort_class="2 class calm")
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is True
    out = capsys.readouterr().out
    assert "no gain: re-booked in 2 class again · new booking NUM1" in out
    assert "1 re-booked in a lower class" in out
    assert "no ticket for this leg" not in out


def test_nothing_bookable_after_the_cancel_is_loud_and_fails(monkeypatch, capsys):
    # Neither class is offered to the pass any more: the old ticket is gone
    # and nothing replaced it. The worst outcome the flag can produce.
    c = _upgradeable(FakeClient(), pass_offers=offers(calm_price=None, second_price=None))

    # A dates selection covering the day, and no calendar skipping, so --book
    # really would book it again.
    cfg = base_cfg(
        comfort_class="2 class calm",
        dates=FUTURE_DATE,
        skip_weekends=False,
        skip_holidays=False,
    )
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is False
    assert [call[0] for call in c.calls].count("create") == 0
    out = capsys.readouterr().out
    assert "no ticket for this leg: the old one is cancelled and nothing was booked back" in out
    assert "recover: run: sj-cli --book" in out
    assert "1 leg(s) now have no ticket:" in out  # said again at the end
    assert "1 left with no ticket" in out


def test_a_lost_leg_outside_the_dates_selection_points_at_sj_se(monkeypatch, capsys):
    # --book only books the days its own selection names, so promising it
    # here would send the traveller to a command that does nothing.
    c = _upgradeable(FakeClient(), pass_offers=offers(calm_price=None, second_price=None))

    cfg = base_cfg(comfort_class="2 class calm", dates=FUTURE_DATE_2)
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is False
    out = capsys.readouterr().out
    assert "book it again on sj.se by hand" in out
    assert f"{FUTURE_DATE} is outside the config's dates selection" in out
    assert "sj-cli --book" not in out


def test_a_failed_cancel_keeps_the_ticket_and_books_nothing(monkeypatch, capsys):
    c = _upgradeable(FakeClient())
    c.cancel_error = RuntimeError("SJ said no")

    cfg = base_cfg(comfort_class="2 class calm")
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is False
    tags = [call[0] for call in c.calls]
    assert "create" not in tags and "checkout" not in tags and "finalize" not in tags
    out = capsys.readouterr().out
    assert "could not release the ticket: SJ said no" in out
    assert "the ticket is untouched, so nothing was booked either" in out
    assert "1 left untouched" in out


def test_an_unconfirmed_cancel_is_reported_and_nothing_is_booked(monkeypatch, capsys):
    # The PATCH landed but the confirmation did not: the booking is in a
    # pending cancellation that only the user can resolve, so re-booking on
    # top of it is not this run's call.
    c = _upgradeable(FakeClient())
    c.finalize_error = RuntimeError("gateway hiccup")

    cfg = base_cfg(comfort_class="2 class calm")
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is False
    assert "create" not in [call[0] for call in c.calls]
    out = capsys.readouterr().out
    assert "cancellation started but not confirmed: gateway hiccup" in out
    assert "sj-cli --cancel-booking OLD1" in out
    assert "1 left in a pending cancellation" in out


def test_a_probe_that_found_no_seats_never_cancels(monkeypatch, capsys):
    # The probe is the gate: no seats to move to, so the held ticket is the
    # best available and must not be released to find that out.
    c = _upgradeable(FakeClient())
    c.offers_by_dep["520"] = offers(calm_price=None, second_price=0)  # calm not sold at all
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)
    _never_prompts(monkeypatch, "prompted with nothing to do")

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(
        c, "tok", cfg, dates=[FUTURE_DATE], dry_run=False, tp_product_id=TP_ID
    )

    assert ok is True
    _assert_no_writes(c)
    assert "none can be upgraded now" in capsys.readouterr().out


def test_declining_the_confirmation_writes_nothing(monkeypatch, capsys):
    c = _upgradeable(FakeClient())

    cfg = base_cfg(comfort_class="2 class calm")
    ok, asked = _run_upgrade(c, cfg, monkeypatch, answer="no thanks")

    assert ok is False
    assert len(asked) == 1
    _assert_no_writes(c)
    out = capsys.readouterr().out
    assert "● upgrade aborted, nothing was cancelled" in out


def test_without_a_terminal_it_refuses_before_any_request(monkeypatch, capsys):
    # A cron job must not be able to release tickets it cannot ask about.
    c = _upgradeable(FakeClient())
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: False)
    _never_prompts(monkeypatch, "prompted without a terminal")

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(
        c, "tok", cfg, dates=[FUTURE_DATE], dry_run=False, tp_product_id=TP_ID
    )

    assert ok is False
    assert c.calls == []  # not even the read half ran
    out = capsys.readouterr().out
    assert "needs a terminal" in out
    assert "--dry-run" in out


def test_a_leg_without_a_service_identifier_is_left_alone(monkeypatch, capsys):
    # Nothing to put in a cancel payload: releasing it is impossible, so it
    # is reported and skipped rather than half-attempted.
    c = _upgradeable(FakeClient(), booking=_booking("OLD1", FUTURE_DATE, service_id=""))
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)
    _never_prompts(monkeypatch, "prompted with nothing to do")

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(
        c, "tok", cfg, dates=[FUTURE_DATE], dry_run=False, tp_product_id=TP_ID
    )

    assert ok is True
    _assert_no_writes(c)
    assert "cannot be released" in capsys.readouterr().out


def test_the_new_ticket_gets_the_configured_seat_preference(monkeypatch):
    # The seat is chosen on the new provisional, before checkout — the whole
    # point of re-booking is landing in the right carriage.
    c = _upgradeable(FakeClient())

    cfg = base_cfg(comfort_class="2 class calm", seat_preference=["window"])
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is True
    assert [(bid, prov) for bid, _u, prov in c.seat_updates] == [("UUID-1", True)]
    tags = [call[0] for call in c.calls]
    assert tags.index("seats") < tags.index("checkout")


def test_dry_run_still_writes_nothing_even_at_a_terminal(monkeypatch, capsys):
    c = _upgradeable(FakeClient())
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)
    _never_prompts(monkeypatch, "a dry run must not prompt")

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=True, tp_product_id=TP_ID)

    assert ok is True
    _assert_no_writes(c)
    assert c.search_tp_ids == [None]  # only the probe searched
    assert "dry run · 1 leg(s) not in 2 class calm · 1 worth trying" in capsys.readouterr().out


def test_a_failed_checkout_also_leaves_the_leg_without_a_ticket(monkeypatch, capsys):
    # A provisional that never checked out is not a ticket: the old one is
    # gone all the same, so this reports as loudly as finding no offer.
    c = _upgradeable(FakeClient(checkout_ok=False))

    cfg = base_cfg(comfort_class="2 class calm")
    ok, _ = _run_upgrade(c, cfg, monkeypatch)

    assert ok is False
    out = capsys.readouterr().out
    assert "no ticket for this leg" in out
    assert "a provisional booking is left behind" in out
    assert "1 left with no ticket" in out


def test_without_a_travel_pass_product_it_refuses_to_touch_anything(monkeypatch, capsys):
    # The re-book needs the pass to find a 0-price offer; without it a
    # release could only lose the ticket.
    c = _upgradeable(FakeClient())
    monkeypatch.setattr(booking.sys.stdin, "isatty", lambda: True)
    _never_prompts(monkeypatch, "prompted with no pass to book on")

    cfg = base_cfg(comfort_class="2 class calm")
    ok = handle_upgrade_class(c, "tok", cfg, dates=[FUTURE_DATE], dry_run=False, tp_product_id="")

    assert ok is False
    assert c.calls == []
    assert "no travel pass to re-book with" in capsys.readouterr().out
