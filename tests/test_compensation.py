"""--request-compensation: the lookup, the delay check, the prompts and the claim's write path."""

from datetime import timedelta

import httpx
import pytest

from sj_cli import compensation
from sj_cli.dates import sweden_now, to_sweden
from tests.fakes import FakeClient, base_cfg

PAST = (sweden_now() - timedelta(days=2)).date().isoformat()
NUM = "ABCD1234"
PASS = {
    "name": "SJ Årskort Silver",
    "code": "1234567890123456",
    "travelPassId": "TP",
    "startTravelValidityDateTime": (sweden_now() - timedelta(days=10)).isoformat(),
    "endTravelValidityDateTime": (sweden_now() + timedelta(days=300)).isoformat(),
}
WRITES = {"comp-travel", "comp-contact", "comp-swish", "comp-confirm"}


def element(dep="06:30", arr="10:05", train="520"):
    return {
        "elementId": "segment_1",
        "producer": {"id": "74", "name": "SJ"},
        "transportId": train,
        "serviceBrandName": "X 2000",
        "departureLocation": {"id": "740000002", "name": "Göteborg Central"},
        "arrivalLocation": {"id": "740000001", "name": "Stockholm Central"},
        "departureDate": {"date": PAST},
        "departureTime": {"time": dep},
        "arrivalDate": {"date": PAST},
        "arrivalTime": {"time": arr},
    }


def eligible(ticket, **kw):
    """One eligibleOrderItems entry, shaped like the token response."""
    return {
        "serviceGroup": {"id": "G1", "detail": {"items": [{"elements": [element(**kw)]}]}},
        "service": {
            "ticketNumber": ticket,
            "consumerName": {"firstName": "Anna", "lastName": "Svensson"},
            "totalPrice": {"amount": 0.0, "currency": "SEK"},
        },
    }


def lookup(*items, existing=()):
    return {
        "delayCompensationToken": "eJ1",
        "existingServiceRequests": list(existing),
        "eligibleOrderItems": list(items),
        "upcomingJourneys": [],
    }


def segment(ticket, dep="06:30", arr="10:05", train="520"):
    """The booked segment the bookings listing returns for that ticket."""
    return {
        "direction": "OUTBOUND",
        "departureDateTime": f"{PAST}T{dep}:00+02:00",
        "arrivalDateTime": f"{PAST}T{arr}:00+02:00",
        "duration": "PT1H39M",
        "publicServiceName": train,
        "serviceBrandNameDescription": "X 2000",
        "departureStation": {"name": "Göteborg Central", "uicStationCode": "740000002"},
        "arrivalStation": {"name": "Stockholm Central", "uicStationCode": "740000001"},
        "productFamily": {
            "salesCategoryComfort": "SECOND_CALM",
            "salesCategoryFlexibility": "FULLFLEX",
        },
        # the ticket number hangs off the product, one per passenger, not off the segment
        "requiredProducts": [
            {"ticketNumber": ticket, "seat": {"carriageNumber": "3", "number": "17"}}
        ],
    }


def booking_item(*segments):
    return {
        "bookingId": f"ID-{NUM}",
        "booking": {
            "bookingNumber": NUM,
            "bookingStatus": "CONFIRMED",
            "journeys": [{"segments": list(segments)}],
        },
    }


def sj_arrival(minutes_late, arr="10:05"):
    planned = to_sweden(f"{PAST}T{arr}:00+02:00")
    actual = (planned + timedelta(minutes=minutes_late)).strftime("%H:%M")
    return {
        "segments": [
            {
                "missingData": False,
                "stations": [
                    {
                        "name": "Stockholm C",
                        "arrived": True,
                        "cancelled": False,
                        "arrival": {
                            "originalTime": f"{PAST} {arr}",
                            "currentTime": f"{PAST} {actual}",
                            "cancelled": False,
                        },
                    }
                ],
            }
        ]
    }


def mock_http():
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(404, json={"unreachable": str(req.url)}, request=req)
        )
    )


class Script:
    """Answers the prompts in order: ask() takes strings/None, confirm() bools, select_list() indexes."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []
        self.lists = []

    def _next(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def ask_optional(self, text):
        return self._next(text)

    def confirm(self, question):
        return self._next(question)

    def select_list(self, prompt, items, render, default_index=0, **_kw):
        self.lists.append((prompt, [render(i) for i in items], default_index))
        reply = self._next(prompt)
        return None if reply is None else items[reply]


def arm(monkeypatch, script, tty=True, minutes_late=64, items=None, booking=True):
    c = FakeClient()
    c.compensation_lookup = lookup(*(items if items is not None else [eligible(f"{NUM}-001")]))
    if booking:
        c.bookings_list = [
            booking_item(segment(f"{NUM}-001"), segment(f"{NUM}-002", "17:22", "19:02", "543"))
        ]
    if minutes_late is not None:
        c.traffic_segments[("520", PAST)] = sj_arrival(minutes_late)
    for name in ("ask_optional", "confirm", "select_list"):
        monkeypatch.setattr(compensation, name, getattr(script, name))
    monkeypatch.setattr(compensation.sys.stdin, "isatty", lambda: tty)
    return c


def cfg_with_number():
    cfg = base_cfg()
    cfg["compensation"] = {"personal_identity_number": "850315-0008"}
    return cfg


def run(c, cfg=None, dry_run=False):
    return compensation.handle_request_compensation(
        c,
        "tok",
        cfg or cfg_with_number(),
        PASS,
        "a@b.se",
        booking_number=NUM,
        dry_run=dry_run,
        http=mock_http(),
    )


# --- gates and the dry run -----------------------------------------------------------


def test_a_real_run_refuses_without_a_terminal_before_any_request(monkeypatch, capsys):
    c = arm(monkeypatch, Script(), tty=False)
    assert run(c) is False
    assert c.calls == []
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith("● not a terminal · --request-compensation asks before filing a claim")
    )


def test_a_dry_run_shows_the_eligible_ticket_with_its_delay_and_files_nothing(monkeypatch, capsys):
    c = arm(monkeypatch, Script(), tty=False)  # a dry run needs no terminal
    assert run(c, dry_run=True) is True
    assert [call[0] for call in c.calls] == ["session", "bookings", "comp-token", "traffic"]
    assert c.calls[2] == ("comp-token", "a@b.se", "1234567890123456", "Årskort Silver", NUM)
    out = capsys.readouterr().out
    assert f" ✓ looking up booking {NUM}\n" in out
    assert "06:30 – 10:05" in out and "X 2000 520" in out and f"{NUM}-001" in out
    assert "+64 min · claim compensation" in out and "(sj.se)" in out
    assert "2 class calm" in out  # the booked segment's own facts, matched by ticket number
    assert not any(call[0] in WRITES for call in c.calls)
    assert out.rstrip().endswith(" ● dry run · nothing requested")


def test_a_ticket_the_bookings_list_does_not_show_is_drawn_from_the_lookup_itself(
    monkeypatch, capsys
):
    c = arm(monkeypatch, Script(), tty=False, booking=False)
    assert run(c, dry_run=True) is True
    out = capsys.readouterr().out
    assert "Göteborg Central → Stockholm Central" in out
    assert "06:30 – 10:05" in out and "+64 min" in out


# --- the happy path -----------------------------------------------------------------


def test_the_claim_is_filed_after_one_confirmation(monkeypatch, capsys):
    s = Script(True)
    c = arm(monkeypatch, s)
    assert run(c) is True
    assert s.prompts == ["request compensation? [y/N]: "]
    assert [call for call in c.calls if call[0] in WRITES] == [
        ("comp-travel", "eJ1", (f"{NUM}-001",)),
        ("comp-contact", "eJ2", "a@b.se", "+46701234567", "Anna", "Svensson"),
        ("comp-swish", "eJ3", "19850315-0008", "+46701234567"),
        ("comp-confirm", "eJ3", "SWS1"),
    ]
    out = capsys.readouterr().out
    assert "ticket" in out and f"{NUM}-001" in out
    assert "payout" in out and "Swish +46701234567" in out
    assert "19850315-****" in out and "0008" not in out  # the identity number is masked
    assert " ✓ sending travel details\n" in out
    assert " ✓ sending contact details\n" in out
    assert " ✓ registering the Swish payout\n" in out
    assert " ✓ filing the claim\n" in out
    assert out.rstrip().endswith(" ● compensation requested · claim 1-123")


def test_the_identity_number_is_asked_for_when_the_config_has_none(monkeypatch, capsys):
    s = Script("19850315-0009", "", "850315-0008", True)  # wrong digit, empty, then right
    c = arm(monkeypatch, s)
    assert run(c, cfg=base_cfg()) is True
    assert s.prompts[:3] == ["personal identity number: "] * 3
    assert ("comp-swish", "eJ3", "19850315-0008", "+46701234567") in c.calls
    out = capsys.readouterr().out
    assert " ! the check digit does not match\n" in out
    assert " ! empty\n" in out


def test_several_tickets_are_picked_from_a_list_the_late_one_first(monkeypatch, capsys):
    s = Script(1, True)
    c = arm(
        monkeypatch,
        s,
        items=[
            eligible(f"{NUM}-001"),
            eligible(f"{NUM}-002", dep="17:22", arr="19:02", train="543"),
        ],
    )
    c.traffic_segments[("543", PAST)] = sj_arrival(0, "19:02")
    assert run(c) is True
    prompt, rows, default = s.lists[0]
    assert prompt == "ticket"
    assert len(rows) == 2 and f"{NUM}-001" in rows[0] and "+64 min" in rows[0]
    assert f"{NUM}-002" in rows[1] and "on time" in rows[1]
    assert default == 0  # the row worth claiming
    assert ("comp-travel", "eJ1", (f"{NUM}-002",)) in c.calls


@pytest.mark.parametrize(
    ("minutes_late", "line"),
    [
        (
            3,
            " ! this train does not seem to have arrived late (+3 min, sj.se) · SJ may refuse the claim\n",
        ),
        (None, " ! no arrival data was found for this train · SJ may refuse the claim\n"),
    ],
)
def test_a_train_not_known_to_be_late_is_a_warning_not_a_refusal(
    monkeypatch, capsys, minutes_late, line
):
    c = arm(monkeypatch, Script(True), minutes_late=minutes_late)
    assert run(c) is True
    out = capsys.readouterr().out
    assert line in out
    assert out.rstrip().endswith(" ● compensation requested · claim 1-123")


def test_existing_claims_are_named_and_the_run_goes_on(monkeypatch, capsys):
    c = arm(monkeypatch, Script(True))
    c.compensation_lookup = lookup(
        eligible(f"{NUM}-001"),
        existing=[{"ticketNumber": f"{NUM}-001", "serviceRequestIds": ["1-999", "1-998"]}, "odd"],
    )
    assert run(c) is True
    out = capsys.readouterr().out
    assert f" ! SJ already has a claim on ticket {NUM}-001: 1-999, 1-998\n" in out
    assert " ! SJ already has a claim on this booking: odd\n" in out  # an unknown shape, verbatim


# --- refusals and aborts --------------------------------------------------------------


def test_no_eligible_ticket_closes_red(monkeypatch, capsys):
    c = arm(monkeypatch, Script(), items=[])
    assert run(c) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(f"● no ticket on {NUM} is eligible for compensation")
    )


def test_a_missing_mobile_number_closes_red_before_the_lookup(monkeypatch, capsys):
    c = arm(monkeypatch, Script())
    c.customer_session = {"customer": {"firstName": "Anna", "lastName": "Svensson"}}
    assert run(c) is False
    assert [call[0] for call in c.calls] == ["session"]
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith("● no mobile number on your SJ account · add one under account settings on sj.se")
    )


def test_declining_files_nothing(monkeypatch, capsys):
    c = arm(monkeypatch, Script(False))
    assert run(c) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert capsys.readouterr().out.rstrip().endswith(" ● aborted, nothing requested")


def test_ctrl_d_at_the_identity_prompt_aborts(monkeypatch, capsys):
    c = arm(monkeypatch, Script(None))
    assert run(c, cfg=base_cfg()) is False
    assert not any(call[0] in WRITES for call in c.calls)
    assert capsys.readouterr().out.rstrip().endswith(" ● aborted, nothing requested")


def test_esc_in_the_ticket_list_aborts(monkeypatch, capsys):
    c = arm(
        monkeypatch,
        Script(None),
        items=[
            eligible(f"{NUM}-001"),
            eligible(f"{NUM}-002", dep="17:22", arr="19:02", train="543"),
        ],
    )
    assert run(c) is False
    assert capsys.readouterr().out.rstrip().endswith(" ● aborted, nothing requested")


# --- failures --------------------------------------------------------------------------


def test_a_failed_lookup_closes_red(monkeypatch, capsys):
    c = arm(monkeypatch, Script())
    c.compensation_errors["comp-token"] = RuntimeError("SJ said no")
    assert run(c) is False
    out = capsys.readouterr().out
    assert f" ✗ looking up booking {NUM}\n" in out
    assert out.rstrip().endswith(f"● could not look up booking {NUM} (SJ said no)")


@pytest.mark.parametrize("step", ["comp-travel", "comp-contact", "comp-swish"])
def test_a_failure_before_the_filing_call_files_nothing(monkeypatch, capsys, step):
    c = arm(monkeypatch, Script(True))
    c.compensation_errors[step] = RuntimeError("gateway hiccup")
    assert run(c) is False
    assert "comp-confirm" not in [call[0] for call in c.calls]
    out = capsys.readouterr().out
    assert " ✗ " in out
    assert out.rstrip().endswith(
        "● could not request compensation (gateway hiccup) · nothing was filed"
    )


def test_a_failure_on_the_filing_call_is_reported_as_unknown(monkeypatch, capsys):
    c = arm(monkeypatch, Script(True))
    c.compensation_errors["comp-confirm"] = RuntimeError("timeout")
    assert run(c) is False
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(
            "● claim status unknown (timeout) · check your claims on sj.se before trying again"
        )
    )


# --- --list-claims -------------------------------------------------------------------

FUTURE = (sweden_now() + timedelta(days=5)).date().isoformat()


def item(number, *segments):
    return {
        "bookingId": f"ID-{number}",
        "booking": {
            "bookingNumber": number,
            "bookingStatus": "CONFIRMED",
            "journeys": [{"segments": list(segments)}],
        },
    }


def future_segment(ticket):
    seg = segment(ticket)
    seg["departureDateTime"] = f"{FUTURE}T06:30:00+02:00"
    seg["arrivalDateTime"] = f"{FUTURE}T10:05:00+02:00"
    return seg


def claims(*entries):
    return {
        "delayCompensationToken": "eJ1",
        "eligibleOrderItems": [],
        "existingServiceRequests": list(entries),
    }


def list_claims(c):
    return compensation.handle_list_claims(c, "tok", PASS, "a@b.se")


def test_list_claims_asks_once_per_departed_booking_and_prints_the_claims(capsys):
    c = FakeClient()
    c.bookings_list = [
        item("AAAA0001", segment("AAAA0001-001"), segment("AAAA0001-002", "17:22", "19:02", "543")),
        item("BBBB0002", segment("BBBB0002-001")),
        item(
            "CCCC0003", future_segment("CCCC0003-001")
        ),  # not departed: nothing to have claimed on
    ]
    c.compensation_lookups = {
        "AAAA0001": claims(
            {"ticketNumber": "AAAA0001-001", "serviceRequestIds": ["1-111"]},
            {"ticketNumber": "AAAA0001-002", "serviceRequestIds": ["1-222", "1-223"]},
        ),
        "BBBB0002": claims(),
    }
    assert list_claims(c) is True
    assert [call for call in c.calls if call[0] == "comp-token"] == [
        ("comp-token", "a@b.se", "1234567890123456", "Årskort Silver", "AAAA0001"),
        ("comp-token", "a@b.se", "1234567890123456", "Årskort Silver", "BBBB0002"),
    ]
    out = capsys.readouterr().out
    assert " ✓ looking up claims\n" in out
    assert "06:30 – 10:05" in out and "AAAA0001   claim 1-111" in out
    assert "17:22 – 19:02" in out and "AAAA0001   claim 1-222, 1-223" in out
    assert "BBBB0002" not in out.split("✓ looking up claims")[1]
    assert out.rstrip().endswith(" ● 3 claim(s) on 1 booking(s)")


def test_list_claims_with_nothing_claimed_says_so(capsys):
    c = FakeClient()
    c.bookings_list = [item("BBBB0002", segment("BBBB0002-001"))]
    assert list_claims(c) is True
    assert capsys.readouterr().out.rstrip().endswith(" ● no claims found")


def test_list_claims_reports_a_failed_lookup_once_and_shows_the_rest(capsys):
    c = FakeClient()
    c.bookings_list = [
        item("AAAA0001", segment("AAAA0001-001")),
        item("BBBB0002", segment("BBBB0002-001")),
    ]
    c.compensation_lookups = {
        "BBBB0002": claims({"ticketNumber": "BBBB0002-001", "serviceRequestIds": ["1-333"]})
    }
    real = c.create_compensation_token

    def flaky(email, card, kind, number):
        if number == "AAAA0001":
            c.calls.append(("comp-token", email, card, kind, number))
            raise RuntimeError("gateway hiccup")
        return real(email, card, kind, number)

    c.create_compensation_token = flaky
    assert list_claims(c) is False
    out = capsys.readouterr().out
    assert " ! claim lookup failed for 1 booking(s): AAAA0001\n" in out
    assert "BBBB0002   claim 1-333" in out
    assert out.rstrip().endswith(" ● 1 claim(s) on 1 booking(s)")


def test_list_claims_names_a_claim_whose_ticket_the_booking_does_not_show(capsys):
    c = FakeClient()
    c.bookings_list = [item("AAAA0001", segment("AAAA0001-001"))]
    c.compensation_lookups = {
        "AAAA0001": claims({"ticketNumber": "AAAA0001-009", "serviceRequestIds": ["1-999"]}, "odd")
    }
    assert list_claims(c) is True
    out = capsys.readouterr().out
    assert (
        " ! AAAA0001: a claim on ticket AAAA0001-009 (1-999), which the booking does not show\n"
        in out
    )
    assert " ! AAAA0001: SJ already has a claim on this booking: odd\n" in out
    assert out.rstrip().endswith(" ● no claims found")
