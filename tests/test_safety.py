"""Unit tests for safety.py failsafe/pause logic (cursor pos mocked)."""

import pytest

from deskctl.core import DeskctlError
from deskctl.core import dpi
from deskctl.core import input as _in
from deskctl.core import safety


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKCTL_HOME", str(tmp_path / "deskctl"))
    return tmp_path / "deskctl"


def test_failsafe_corner_primary_at_origin(monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (0, 0, 1920, 1080))
    assert safety.is_failsafe_position(0, 0)
    assert safety.is_failsafe_position(2, 0)
    assert safety.is_failsafe_position(0, 2)
    assert safety.is_failsafe_position(2, 2)
    assert not safety.is_failsafe_position(3, 0)
    assert not safety.is_failsafe_position(0, 3)
    assert not safety.is_failsafe_position(100, 100)


def test_failsafe_corner_negative_origin(monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (-1920, -1080, 3840, 2160))
    assert safety.is_failsafe_position(-1920, -1080)
    assert safety.is_failsafe_position(-1918, -1079)
    assert not safety.is_failsafe_position(0, 0)
    assert not safety.is_failsafe_position(-1917, -1080)


def test_pause_resume_toggle(home):
    assert not safety.is_paused()
    safety.pause()
    assert safety.is_paused()
    assert safety.toggle() is False  # was paused -> now resumed
    assert not safety.is_paused()
    assert safety.toggle() is True
    assert safety.is_paused()
    safety.resume()
    assert not safety.is_paused()


def test_check_before_action_refuses_when_paused(home):
    safety.pause()
    with pytest.raises(DeskctlError, match="paused"):
        safety.check_before_action()
    safety.resume()


def test_check_before_action_failsafe_pauses(home, monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (0, 0, 100, 100))
    monkeypatch.setattr(_in, "cursor_pos", lambda: (0, 0))
    with pytest.raises(DeskctlError, match="failsafe"):
        safety.check_before_action()
    assert safety.is_paused()  # failsafe auto-pauses
    safety.resume()


def test_check_before_action_ok(home, monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (0, 0, 100, 100))
    monkeypatch.setattr(_in, "cursor_pos", lambda: (50, 50))
    safety.check_before_action()  # should not raise
