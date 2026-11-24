"""Unit tests for privacy.py - exclusion matching + blackout rect math."""

import pytest

from deskctl.core import DeskctlError
from deskctl.core import privacy


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKCTL_HOME", str(tmp_path / "deskctl"))
    monkeypatch.delenv("DESKCTL_EXCLUDE", raising=False)
    return tmp_path / "deskctl"


def test_default_exclusions(home):
    for p in ("1password.exe", "keepass.exe", "keepassxc.exe",
              "bitwarden.exe", "enpass.exe"):
        assert privacy.is_excluded_proc(p), p
    assert not privacy.is_excluded_proc("notepad.exe")


def test_exclusion_normalization(home):
    # case-insensitive, .exe optional, env adds to (never removes) defaults
    assert privacy.is_excluded_proc("KeePassXC.EXE")
    assert privacy.is_excluded_proc("keepassxc")
    assert not privacy.is_excluded_proc("keepassxcer.exe")
    import os
    os.environ["DESKCTL_EXCLUDE"] = "MyVault.exe, other tool "
    assert privacy.is_excluded_proc("myvault")
    assert privacy.is_excluded_proc("other tool")
    assert privacy.is_excluded_proc("1password.exe")  # defaults still apply
    del os.environ["DESKCTL_EXCLUDE"]


def test_check_window_allowed_by_proc(home):
    privacy.check_window_allowed(123, "Vault", "notepad.exe")  # no raise
    with pytest.raises(DeskctlError, match="privacy"):
        privacy.check_window_allowed(123, "Vault", "bitwarden.exe")


def test_blackout_rects_clipping():
    # capture (0,0,100,100); windows partially inside/outside
    wins = [
        (10, 10, 20, 20),     # fully inside
        (-50, -50, 60, 60),   # overlaps top-left corner -> clipped to (0,0,10,10)
        (90, 90, 50, 50),     # overlaps bottom-right -> (90,90,10,10)
        (200, 200, 10, 10),   # fully outside
        (0, 0, 100, 100),     # covers whole capture
    ]
    out = privacy.blackout_rects((0, 0, 100, 100), wins)
    assert (10, 10, 20, 20) in out
    assert (0, 0, 10, 10) in out
    assert (90, 90, 10, 10) in out
    assert (0, 0, 100, 100) in out
    assert len(out) == 4  # the fully-outside one is dropped


def test_blackout_rects_empty_and_degenerate():
    assert privacy.blackout_rects((0, 0, 100, 100), []) == []
    # zero-area window rect produces nothing
    assert privacy.blackout_rects((0, 0, 100, 100), [(10, 10, 0, 0)]) == []
    # capture with non-zero origin offsets correctly
    out = privacy.blackout_rects((100, 100, 50, 50), [(120, 120, 20, 20)])
    assert out == [(120, 120, 20, 20)]
