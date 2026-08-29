"""--book-journey: the prompts, the pick lists and the write path through the Cart."""

from datetime import timedelta

import pytest

from sj_cli import journey
from sj_cli.dates import sweden_now
from sj_cli.stations import StationIndex, parse_stations
from tests.fakes import FakeClient

TODAY = sweden_now().date()
FUTURE = TODAY + timedelta(days=30)
VALID = (TODAY - timedelta(days=10), TODAY + timedelta(days=300))


class Answers:
    """Scripted replies for ask_optional; records the prompts it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked = []

    def __call__(self, text):
        self.asked.append(text)
        return self.replies.pop(0)


def index():
    return StationIndex(parse_stations(FakeClient.STATION_LIST))


# --- _ask_date -----------------------------------------------------------------


def test_ask_date_enter_keeps_the_default(monkeypatch):
    answers = Answers("")
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == TODAY
    assert answers.asked == [f"date [{TODAY.isoformat()}]: "]


def test_ask_date_reasks_on_garbage_past_and_outside_the_pass(monkeypatch, capsys):
    answers = Answers(
        "soon",
        (TODAY - timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=400)).isoformat(),
        FUTURE.isoformat(),
    )
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == FUTURE
    out = capsys.readouterr().out
    assert " ! not a date, use YYYY-MM-DD\n" in out
    assert " ! date is in the past\n" in out
    assert f" ! the pass is valid {VALID[0].isoformat()} – {VALID[1].isoformat()}\n" in out


def test_ask_date_uses_the_callers_too_early_text_and_skips_unknown_bounds(monkeypatch, capsys):
    answers = Answers((FUTURE - timedelta(days=1)).isoformat(), FUTURE.isoformat())
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert (
        journey._ask_date(
            "return date", FUTURE, FUTURE, "return date is before the outbound date", (None, None)
        )
        == FUTURE
    )
    assert " ! return date is before the outbound date\n" in capsys.readouterr().out


def test_ask_date_ctrl_d_is_none(monkeypatch):
    monkeypatch.setattr(journey, "ask_optional", Answers(None))
    assert journey._ask_date("date", TODAY, TODAY, "x", VALID) is None


def test_ask_date_default_is_clamped_to_the_pass_start(monkeypatch):
    valid = (TODAY + timedelta(days=5), TODAY + timedelta(days=300))
    answers = Answers("")
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", valid) == valid[0]
    assert answers.asked == [f"date [{valid[0].isoformat()}]: "]


def test_ask_date_whitespace_reply_keeps_the_default(monkeypatch):
    answers = Answers("   ")
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert journey._ask_date("date", TODAY, TODAY, "date is in the past", VALID) == TODAY


def test_ask_date_accepts_both_pass_boundary_days(monkeypatch, capsys):
    valid = (TODAY, TODAY + timedelta(days=10))
    answers = Answers(valid[0].isoformat(), valid[1].isoformat())
    monkeypatch.setattr(journey, "ask_optional", answers)
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
    answers = Answers(reply)
    monkeypatch.setattr(journey, "ask_optional", answers)
    assert journey._ask_yes_no("return?", default) is expected
    assert answers.asked == [f"return? {shown}: "]


def test_ask_yes_no_whitespace_reply_keeps_the_default(monkeypatch):
    monkeypatch.setattr(journey, "ask_optional", Answers("   "))
    assert journey._ask_yes_no("return?", True) is True


def test_default_station_prefers_the_live_list_then_the_map():
    idx = index()
    assert journey._default_station(idx, FakeClient(), "göteborg central")["code"] == "740000002"
    assert journey._default_station(idx, FakeClient(), "Linköping Central") == {
        "name": "Linköping Central",
        "code": "Linköping Central",
        "synonyms": [],
    }
