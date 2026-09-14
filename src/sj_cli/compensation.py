"""--request-compensation: a delay-compensation claim for one ticket, through sj.se's form API."""

import logging
import sys
from datetime import date, timedelta
from typing import Any

import httpx

from sj_cli.booking import (
    _add_delays,
    _segment_to_display_row,
    booking_date_range,
    fetch_bookings_with_spinner,
    is_active_booking,
    pass_validity,
)
from sj_cli.client import SJClient
from sj_cli.config import delay_thresholds, personal_identity_number, trafikverket_key
from sj_cli.dates import sweden_now, to_sweden
from sj_cli.errors import error_text
from sj_cli.identity import mask_personal_number, normalise_personal_number
from sj_cli.output import (
    ask_optional,
    blank,
    confirm,
    pdim,
    print_day_cards,
    print_fact,
    pstatus,
    pwarn,
    select_list,
    spinner,
)

logger = logging.getLogger(__name__)


# --- the lookup ---------------------------------------------------------------------


def _card_type(travel_pass: dict) -> str:
    """The pass type as the form names it: the pass name without its `SJ ` prefix."""
    name = str(travel_pass.get("name") or "")
    return name.removeprefix("SJ ").strip()


def _element_segment(item: dict) -> dict:
    """
    A booked-segment-shaped dict built from an eligible item's own journey element.

    The bookings listing normally supplies the real segment (class, seat,
    duration); this is the fallback for a ticket it does not show, carrying
    what the lookup itself knows: train, stations with codes, and the
    Swedish date and times, enough for the card and the delay lookup.
    """
    group = item.get("serviceGroup") or {}
    items = (group.get("detail") or {}).get("items") or [{}]
    elements = items[0].get("elements") or [{}]
    el = elements[0]
    dep_loc = el.get("departureLocation") or {}
    arr_loc = el.get("arrivalLocation") or {}
    dep_date = (el.get("departureDate") or {}).get("date") or ""
    dep_time = (el.get("departureTime") or {}).get("time") or ""
    arr_date = (el.get("arrivalDate") or {}).get("date") or dep_date
    arr_time = (el.get("arrivalTime") or {}).get("time") or ""
    return {
        "direction": "OUTBOUND",
        # Naive timestamps read as Swedish local time (dates.parse_api_datetime).
        "departureDateTime": f"{dep_date}T{dep_time}:00" if dep_date and dep_time else "",
        "arrivalDateTime": f"{arr_date}T{arr_time}:00" if arr_date and arr_time else "",
        "publicServiceName": el.get("transportId"),
        "serviceBrandNameDescription": el.get("serviceBrandName"),
        "departureStation": {"name": dep_loc.get("name"), "uicStationCode": dep_loc.get("id")},
        "arrivalStation": {"name": arr_loc.get("name"), "uicStationCode": arr_loc.get("id")},
    }


def _booked_segments(items: list[dict], booking_number: str) -> dict[str, dict]:
    """The booking's segments by ticket number, from the bookings listing (empty when not found)."""
    for entry in items:
        booking = entry.get("booking") or {}
        if booking.get("bookingNumber") != booking_number or not is_active_booking(booking):
            continue
        found: dict[str, dict] = {}
        for journey in booking.get("journeys") or []:
            for seg in journey.get("segments") or []:
                # One ticket per passenger, each on its required product.
                for product in seg.get("requiredProducts") or []:
                    number = product.get("ticketNumber")
                    if number:
                        found[str(number)] = seg
        return found
    return {}


def _ticket_rows(
    eligible: list[dict], segments: dict[str, dict]
) -> list[tuple[dict[str, Any], dict]]:
    """(display row, segment) per eligible ticket: the booked segment if known, else the lookup."""
    now = sweden_now()
    rows: list[tuple[dict[str, Any], dict]] = []
    for item in eligible:
        ticket = str((item.get("service") or {}).get("ticketNumber") or "—")
        segment = segments.get(ticket) or _element_segment(item)
        row = _segment_to_display_row(segment, ticket, now)
        row["ticket"] = ticket
        rows.append((row, segment))
    return rows


def _existing_claim_line(entry: object) -> str:
    """A claim SJ already holds: `{ticketNumber, serviceRequestIds}` as seen live, else verbatim."""
    if isinstance(entry, dict):
        ticket = entry.get("ticketNumber")
        ids = [str(i) for i in entry.get("serviceRequestIds") or []]
        if ticket and ids:
            return f"SJ already has a claim on ticket {ticket}: {', '.join(ids)}"
    return f"SJ already has a claim on this booking: {entry}"


def _delay_line(row: dict) -> str | None:
    """The `!` line for a ticket the lookup does not show as worth claiming, or None."""
    if row.get("claim"):
        return None
    if row.get("delay") in ("", "no data"):
        return "no arrival data was found for this train · SJ may refuse the claim"
    source = str(row.get("delay_source") or "").strip("()")
    figure = str(row.get("delay") or "")
    where = f"{figure}, {source}" if source else figure
    return f"this train does not seem to have arrived late ({where}) · SJ may refuse the claim"


def _ticket_line(row: dict) -> str:
    """One picker row: date, times, train, ticket number and the delay verdict."""
    parts = [
        str(row.get("date") or "—"),
        f"{row.get('departure') or '—'} → {row.get('arrival') or '—'}",
        str(row.get("train") or "—"),
        str(row.get("ticket") or "—"),
        " ".join(p for p in (row.get("delay"), row.get("delay_source")) if p),
    ]
    return "   ".join(p for p in parts if p)


def _aborted() -> bool:
    blank()
    pstatus(False, "aborted, nothing requested")
    return False


def _ask_identity_number() -> str | None:
    """Ask for the personal identity number until it parses; None on Ctrl-D."""
    while True:
        answer = ask_optional("personal identity number: ")
        if answer is None:
            return None
        try:
            return normalise_personal_number(answer)
        except ValueError as e:
            pwarn(str(e))


# --- the mode -----------------------------------------------------------------------


def handle_request_compensation(
    client: SJClient,
    access_token: str,
    cfg: dict,
    active_pass: dict,
    email: str,
    *,
    booking_number: str,
    dry_run: bool = False,
    http: httpx.Client | None = None,
) -> bool:
    """
    Request delay compensation for one ticket of a booking, Swish as the payout.

    The steps sj.se's form takes, in order: the account's session (the
    mobile number and the name), the bookings listing (the ticket's own
    facts), the token lookup (which tickets SJ considers eligible), the
    --delays lookup per ticket (so the user sees whether the train was late
    before claiming), the pick, the identity number (config or prompt),
    one confirmation, then travel details → contact → Swish payout →
    confirmation. Nothing is filed before the last call.

    Args:
        client: The SJ HTTP client.
        access_token: Valid access token (the session endpoint needs it; the
            compensation API itself is public).
        cfg: The validated config: [compensation].personal_identity_number
            when set, [delays] for the thresholds and the Trafikverket key.
        active_pass: The travel pass (card number and type identify the claim).
        email: The account's e-mail address, the form's "order security".
        booking_number: The booking to claim on.
        dry_run: Show the eligible tickets and their delays, file nothing,
            ask nothing — so it also runs without a terminal.
        http: The client for the external delay sources; tests inject theirs.

    Returns:
        True when the claim was filed (or a dry run completed); False when
        refused, aborted, declined, nothing was eligible, or a call failed.

    """
    if not dry_run and not sys.stdin.isatty():
        pstatus(False, "not a terminal · --request-compensation asks before filing a claim")
        return False

    # Who is claiming: the account's name and mobile number
    try:
        with spinner("fetching account details", trail=False):
            customer = (client.get_customer_session(access_token) or {}).get("customer") or {}
    except Exception as e:
        logger.error(f"customer session: {e}")
        pstatus(False, f"could not fetch your account details ({error_text(e)})")
        return False
    phone = str(customer.get("privatePhoneNumber") or customer.get("loginPhoneNumber") or "")
    first_name = str(customer.get("firstName") or "")
    last_name = str(customer.get("lastName") or "")
    if not phone:
        pstatus(
            False, "no mobile number on your SJ account · add one under account settings on sj.se"
        )
        return False

    # The ticket's own facts, when the bookings listing shows the booking
    segments: dict[str, dict] = {}
    try:
        items = fetch_bookings_with_spinner(
            client,
            access_token,
            *booking_date_range(active_pass),
            label="fetching bookings",
            trail=False,
        )
        segments = _booked_segments(items, booking_number)
    except Exception as e:
        logger.error(f"bookings: {e}")
        pwarn(
            f"could not read the bookings list ({error_text(e)}) · the lookup's own facts are shown"
        )

    # What SJ considers eligible
    card_number = str(active_pass.get("code") or "")
    try:
        with spinner(f"looking up booking {booking_number}"):
            found = client.create_compensation_token(
                email, card_number, _card_type(active_pass), booking_number
            )
    except Exception as e:
        logger.error(f"compensation lookup for {booking_number}: {e}")
        blank()
        pstatus(False, f"could not look up booking {booking_number} ({error_text(e)})")
        return False
    token = str(found.get("delayCompensationToken") or "")
    eligible = found.get("eligibleOrderItems") or []
    for existing in found.get("existingServiceRequests") or []:
        pwarn(_existing_claim_line(existing))
    if not eligible or not token:
        blank()
        pstatus(False, f"no ticket on {booking_number} is eligible for compensation")
        return False

    # Was each train late? The --delays lookup, so the user sees it before claiming.
    tasks = _ticket_rows(eligible, segments)
    _add_delays(client, tasks, delay_thresholds(cfg), trafikverket_key(cfg), http)
    rows = [row for row, _ in tasks]
    blank()
    print_day_cards(rows)
    blank()
    if dry_run:
        for row in rows:
            line = _delay_line(row)
            if line:
                pwarn(f"{row['ticket']}: {line}")
        pstatus(None, "dry run · nothing requested")
        return True

    # The pick
    if len(rows) == 1:
        chosen = rows[0]
    else:
        default = next((i for i, row in enumerate(rows) if row.get("claim")), 0)
        picked = select_list("ticket", rows, _ticket_line, default_index=default)
        if picked is None:
            return _aborted()
        chosen = picked
    line = _delay_line(chosen)
    if line:
        pwarn(line)

    # The identity number, then the facts and the consent
    identity = personal_identity_number(cfg)
    if identity is None:
        identity = _ask_identity_number()
        if identity is None:
            return _aborted()
    blank()
    print_fact(
        "ticket", f"{chosen['ticket']} · {chosen['date']} {chosen['departure']} {chosen['route']}"
    )
    print_fact("train", str(chosen.get("train") or "—"))
    delay = " ".join(p for p in (chosen.get("delay"), chosen.get("delay_source")) if p)
    print_fact("delay", delay or "no data")
    print_fact("contact", f"{first_name} {last_name} · {email} · {phone}".strip())
    print_fact("payout", f"Swish {phone}")
    print_fact("identity", mask_personal_number(identity))
    blank()
    if not confirm("request compensation? [y/N]: "):
        return _aborted()

    # The write: nothing is filed before the last call
    try:
        with spinner("sending travel details"):
            step = client.put_compensation_travel_details(token, [chosen["ticket"]])
            token = str(step.get("delayCompensationToken") or token)
        with spinner("sending contact details"):
            step = client.put_compensation_contact(token, email, phone, first_name, last_name)
            token = str(step.get("delayCompensationToken") or token)
        with spinner("registering the Swish payout"):
            bar_id = str(client.create_swish_payout(token, identity, phone).get("barId") or "")
    except Exception as e:
        logger.error(f"compensation for {booking_number}: {e}")
        blank()
        pstatus(False, f"could not request compensation ({error_text(e)}) · nothing was filed")
        return False
    try:
        with spinner("filing the claim"):
            filed = client.confirm_compensation(token, bar_id)
    except Exception as e:
        logger.error(f"compensation filing for {booking_number}: {e}")
        blank()
        pstatus(
            False,
            f"claim status unknown ({error_text(e)}) · "
            "check your claims on sj.se before trying again",
        )
        return False
    claims = [str(c) for c in filed.get("ticketCompensationServiceRequests") or []]
    blank()
    if claims:
        pstatus(True, f"compensation requested · claim {', '.join(claims)}")
    else:
        pstatus(True, "compensation requested")
        pdim("SJ returned no claim number · check your claims on sj.se")
    return True


# --- the listing --------------------------------------------------------------------


def _departed_bookings(items: list[dict], now: Any) -> list[tuple[str, dict]]:
    """(booking number, booking) for every active booking with at least one departed segment."""
    found: list[tuple[str, dict]] = []
    for entry in items:
        booking = entry.get("booking") or {}
        number = booking.get("bookingNumber")
        if not number or not is_active_booking(booking):
            continue
        departed = False
        for journey in booking.get("journeys") or []:
            for seg in journey.get("segments") or []:
                try:
                    departed |= to_sweden(seg.get("departureDateTime") or "") < now
                except (ValueError, TypeError):
                    continue
        if departed:
            found.append((str(number), booking))
    return found


def _segments_by_ticket(booking: dict) -> dict[str, dict]:
    """The booking's segments by ticket number (one ticket per passenger, on the product)."""
    found: dict[str, dict] = {}
    for journey in booking.get("journeys") or []:
        for seg in journey.get("segments") or []:
            for product in seg.get("requiredProducts") or []:
                number = product.get("ticketNumber")
                if number:
                    found[str(number)] = seg
    return found


def handle_list_claims(
    client: SJClient,
    access_token: str,
    active_pass: dict,
    email: str,
    *,
    since: date | None = None,
) -> bool:
    """
    List the compensation claims SJ holds on the account's bookings (--list-claims).

    SJ offers no list of claims, only the per-booking lookup that starts a
    claim, which names the tickets already claimed on and their claim
    numbers — nothing about their state. So: every active booking in the
    pass window with a departed leg (a claim needs a finished trip) is
    looked up once, and each claimed ticket is shown as a leg row with its
    claim number in a column after the booking number. `since` (from
    --since) starts the walk at that date instead of the pass start. Read-only:
    the lookup creates nothing.

    Returns:
        True when the listing completed, even with no claims; False when the
        bookings fetch or any lookup failed (the rest is still shown).

    """
    now = sweden_now()
    first, _ = pass_validity(active_pass)
    start = (since or first or (now.date() - timedelta(days=90))).isoformat()
    try:
        items = fetch_bookings_with_spinner(
            client, access_token, start, now.date().isoformat(), label="fetching bookings"
        )
    except Exception as e:
        logger.error(f"bookings: {e}")
        blank()
        pstatus(False, f"could not fetch bookings ({error_text(e)})")
        return False
    bookings = _departed_bookings(items, now)
    card_number = str(active_pass.get("code") or "")
    card_type = _card_type(active_pass)

    rows: list[dict] = []
    notes: list[str] = []
    failed: list[str] = []
    claimed_bookings: set[str] = set()
    claim_count = 0
    total = len(bookings)
    with spinner("looking up claims") as update:
        for done, (number, booking) in enumerate(bookings):
            if done:
                update(f"looking up claims · {done} of {total}")
            try:
                found = client.create_compensation_token(email, card_number, card_type, number)
            except Exception as e:
                logger.error(f"claim lookup for {number}: {e}")
                failed.append(number)
                continue
            segments = _segments_by_ticket(booking)
            for entry in found.get("existingServiceRequests") or []:
                ticket = entry.get("ticketNumber") if isinstance(entry, dict) else None
                ids = (
                    [str(i) for i in entry.get("serviceRequestIds") or []]
                    if isinstance(entry, dict)
                    else []
                )
                if not ticket or not ids:
                    notes.append(f"{number}: {_existing_claim_line(entry)}")
                    continue
                segment = segments.get(str(ticket))
                if segment is None:
                    notes.append(
                        f"{number}: a claim on ticket {ticket} ({', '.join(ids)}), "
                        "which the booking does not show"
                    )
                    continue
                row = _segment_to_display_row(segment, number, now)
                row["claim_ref"] = f"claim {', '.join(ids)}"
                rows.append(row)
                claimed_bookings.add(number)
                claim_count += len(ids)
    if failed:
        pwarn(f"claim lookup failed for {len(failed)} booking(s): {', '.join(failed)}")
    for note in notes:
        pwarn(note)
    blank()
    if rows:
        print_day_cards(rows)
        blank()
        pstatus(None, f"{claim_count} claim(s) on {len(claimed_bookings)} booking(s)")
    else:
        pstatus(None, "no claims found")
    return not failed
