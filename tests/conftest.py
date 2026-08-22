"""Shared fixtures: no colour, no sleeping, no real network."""

import time

import pytest


@pytest.fixture(autouse=True)
def _no_colour_no_sleep(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(time, "sleep", lambda *_: None)
