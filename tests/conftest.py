"""Shared fixtures: no colour, no sleeping, no real network, prompts re-armed."""

import time

import pytest

from sj_cli import booking


@pytest.fixture(autouse=True)
def _no_colour_no_sleep(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def _seat_prompts_armed(monkeypatch):
    """
    Every test starts willing to prompt for a seat.

    `booking._ask_disabled` is module state that only the two production
    entry points reset; without this a test that hits EOF (or a missing
    terminal) would silently disable the picker for every test after it.
    """
    monkeypatch.setattr(booking, "_ask_disabled", False)
