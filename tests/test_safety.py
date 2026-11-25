"""Unit tests for safety.py failsafe/pause logic (cursor pos mocked)."""

import pytest

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import dpi
from windesktopctl.core import input as _in
from windesktopctl.core import safety


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "windesktopctl"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(d))
    return d


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
    with pytest.raises(WindesktopctlError, match="paused"):
        safety.check_before_action()
    safety.resume()


def test_check_before_action_failsafe_pauses(home, monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (0, 0, 100, 100))
    monkeypatch.setattr(_in, "cursor_pos", lambda: (0, 0))
    with pytest.raises(WindesktopctlError, match="failsafe"):
        safety.check_before_action()
    assert safety.is_paused()  # failsafe auto-pauses
    safety.resume()


def test_check_before_action_ok(home, monkeypatch):
    monkeypatch.setattr(dpi, "virtual_screen", lambda: (0, 0, 100, 100))
    monkeypatch.setattr(_in, "cursor_pos", lambda: (50, 50))
    safety.check_before_action()  # should not raise


# ------------------------------------------------------------- takeover --

def _reset_takeover(monkeypatch):
    monkeypatch.setattr(safety, "_user_active_until", 0.0)
    monkeypatch.setattr(safety, "_last_user_event", 0.0)


def test_takeover_file_marks_active(home, monkeypatch):
    import time as _t

    _reset_takeover(monkeypatch)
    (home / "user_active").write_text(repr(_t.time() + 60))
    assert safety.is_user_active()
    monkeypatch.setattr(_in, "cursor_pos", lambda: (50, 50))
    with pytest.raises(WindesktopctlError, match="user is using the PC"):
        safety.check_before_action()


def test_takeover_file_expired(home, monkeypatch):
    import time as _t

    _reset_takeover(monkeypatch)
    (home / "user_active").write_text(repr(_t.time() - 60))
    assert not safety.is_user_active()


def test_takeover_inprocess_mark(home, monkeypatch):
    _reset_takeover(monkeypatch)
    monkeypatch.setattr(safety, "idle_seconds", lambda: 3.0)
    safety._mark_user_active()
    assert safety.is_user_active()
    monkeypatch.setattr(_in, "cursor_pos", lambda: (50, 50))
    with pytest.raises(WindesktopctlError, match="user is using"):
        safety.check_user_takeover()


def test_takeover_heartbeat_live(home, monkeypatch):
    import time as _t

    _reset_takeover(monkeypatch)
    (home / "guard_heartbeat").write_text(repr(_t.time()))
    assert safety.takeover_live()
    assert "live" in safety.takeover_status()
    (home / "guard_heartbeat").write_text(repr(_t.time() - 60))
    assert not safety.takeover_live()
    assert "off" in safety.takeover_status()


def test_takeover_idle_seconds_env(home, monkeypatch):
    monkeypatch.setenv("WINDESKTOPCTL_IDLE_SECONDS", "7")
    assert safety.idle_seconds() == 7.0
    monkeypatch.setenv("WINDESKTOPCTL_IDLE_SECONDS", "bogus")
    assert safety.idle_seconds() == 3.0
