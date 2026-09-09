"""The paged bookings fetch: its progress callback and the counting spinner around it."""

from sj_cli import output
from sj_cli.booking import fetch_all_bookings, fetch_bookings_with_spinner
from tests.fakes import FakeClient, TtyOut


def _items(n: int) -> list[dict]:
    return [{"booking": {"bookingNumber": f"B{i:02d}"}} for i in range(n)]


def _paged(n: int, page_size: int | None) -> FakeClient:
    client = FakeClient()
    client.bookings_list = _items(n)
    client.page_size = page_size
    return client


class _NoTotal:
    """Two pages without a totalCount, should the API ever drop it."""

    def get_bookings(self, token, start_date, end_date, page=0):
        return {"bookings": [{"i": page}], "nextPage": 1 if page == 0 else None}


class _EchoPage:
    """A server that answers every page with nextPage == the page asked for."""

    def get_bookings(self, token, start_date, end_date, page=0):
        return {"bookings": [{"i": page}], "nextPage": page, "totalCount": 1}


class _SkipsAhead:
    """Page 0 points at page 7: the loop follows the server's value, not a counter."""

    def get_bookings(self, token, start_date, end_date, page=0):
        return {"bookings": [{"i": page}], "nextPage": 7 if page == 0 else None, "totalCount": 2}


def test_paged_fetch_reports_progress_while_pages_remain():
    client = _paged(42, 10)
    seen: list[tuple[int, int | None]] = []
    got = fetch_all_bookings(
        client, "tok", "2026-09-09", "2027-08-04", progress=lambda n, t: seen.append((n, t))
    )
    assert got == _items(42)
    assert seen == [(10, 42), (20, 42), (30, 42), (40, 42)]
    assert client.calls.count(("bookings", "2026-09-09", "2027-08-04")) == 5


def test_single_page_fetch_never_reports():
    seen: list[tuple[int, int | None]] = []
    for page_size in (None, 10):  # no nextPage at all / nextPage None on the only page
        client = _paged(7, page_size)
        got = fetch_all_bookings(client, "tok", "d", "d", progress=lambda n, t: seen.append((n, t)))
        assert got == _items(7)
    assert seen == []


def test_progress_without_total_passes_none():
    seen: list[tuple[int, int | None]] = []
    got = fetch_all_bookings(_NoTotal(), "tok", "d", "d", progress=lambda n, t: seen.append((n, t)))
    assert got == [{"i": 0}, {"i": 1}]
    assert seen == [(1, None)]


def test_a_page_pointing_at_itself_ends_the_fetch_without_reporting():
    seen: list[tuple[int, int | None]] = []
    got = fetch_all_bookings(
        _EchoPage(), "tok", "d", "d", progress=lambda n, t: seen.append((n, t))
    )
    assert got == [{"i": 0}]
    assert seen == []


def test_the_loop_follows_next_page_rather_than_counting():
    seen: list[tuple[int, int | None]] = []
    got = fetch_all_bookings(
        _SkipsAhead(), "tok", "d", "d", progress=lambda n, t: seen.append((n, t))
    )
    assert got == [{"i": 0}, {"i": 7}]
    assert seen == [(1, 2)]


def test_spinner_counts_while_pages_remain_and_the_trail_keeps_the_label(monkeypatch):
    out = TtyOut()
    monkeypatch.setattr(output.sys, "stdout", out)
    client = _paged(3, 1)
    got = fetch_bookings_with_spinner(
        client, "tok", "2026-09-09", "2027-08-04", label="fetching bookings"
    )
    text = out.getvalue()
    assert got == _items(3)
    assert client.calls[0] == ("bookings", "2026-09-09", "2027-08-04")
    assert "fetching bookings · 1 of 3" in text
    assert "fetching bookings · 2 of 3" in text
    assert "· 3 of 3" not in text  # the last page ends the fetch: nothing left to count
    assert text.endswith("\r\x1b[2K ✓ fetching bookings\n")


def test_spinner_counts_so_far_without_a_total(monkeypatch):
    out = TtyOut()
    monkeypatch.setattr(output.sys, "stdout", out)
    fetch_bookings_with_spinner(_NoTotal(), "tok", "d", "d", label="fetching bookings")
    assert "fetching bookings · 1 so far" in out.getvalue()


def test_spinner_wrapper_without_a_tty_prints_only_the_trail(capsys):
    got = fetch_bookings_with_spinner(
        _paged(2, 1), "tok", "d", "d", label="fetching bookings", trail=False
    )
    assert got == _items(2)
    assert capsys.readouterr().out == ""
    fetch_bookings_with_spinner(_paged(2, 1), "tok", "d", "d", label="fetching bookings")
    assert capsys.readouterr().out == " ✓ fetching bookings\n"
