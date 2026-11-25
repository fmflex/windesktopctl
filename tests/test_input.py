"""Unit tests for input.py key-combo parsing (no real input sent)."""

import pytest

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import input as _in

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # alt
VK_LWIN = 0x5B


def test_parse_ctrl_shift_s():
    mods, key, _sh = _in.parse_combo("ctrl+shift+s")
    assert VK_CONTROL in mods and VK_SHIFT in mods
    assert key == ord("S")


def test_parse_alt_tab():
    mods, key, _ = _in.parse_combo("alt+tab")
    assert mods == [VK_MENU]
    assert key == 0x09


def test_parse_single_keys():
    assert _in.parse_combo("enter")[1] == 0x0D
    assert _in.parse_combo("f5")[1] == 0x74  # 0x6F + 5
    assert _in.parse_combo("f24")[1] == 0x87
    assert _in.parse_combo("escape")[1] == 0x1B
    assert _in.parse_combo("space")[1] == 0x20


def test_parse_win_r():
    mods, key, _ = _in.parse_combo("win+r")
    assert VK_LWIN in mods
    assert key == ord("R")


def test_parse_case_and_spaces():
    mods, key, _ = _in.parse_combo("Ctrl + Shift + K")
    assert VK_CONTROL in mods and VK_SHIFT in mods
    assert key == ord("K")


def test_parse_named_aliases():
    assert _in.parse_combo("pgdn")[1] == 0x22
    assert _in.parse_combo("pgup")[1] == 0x21
    assert _in.parse_combo("del")[1] == 0x2E
    assert _in.parse_combo("ins")[1] == 0x2D


def test_parse_shifted_punctuation_adds_shift():
    mods, key, _ = _in.parse_combo("!")
    assert key == ord("1")  # '!' is shift+1
    assert VK_SHIFT in mods
    mods, key, _ = _in.parse_combo("ctrl+!")
    assert VK_CONTROL in mods and VK_SHIFT in mods and key == ord("1")


def test_parse_errors():
    with pytest.raises(WindesktopctlError):
        _in.parse_combo("")
    with pytest.raises(WindesktopctlError):  # modifiers only, no key
        _in.parse_combo("ctrl+shift")
    with pytest.raises(WindesktopctlError):  # two non-modifier keys
        _in.parse_combo("a+b")
    with pytest.raises(WindesktopctlError):  # unknown named key
        _in.parse_combo("notakey")
    with pytest.raises(WindesktopctlError):  # f-key out of range
        _in.parse_combo("f25")


def test_utf16_units_for_type_text():
    # surrogate pair (emoji) must produce two UTF-16 code units
    data = "a😀".encode("utf-16-le")
    import struct

    units = struct.unpack(f"<{len(data) // 2}H", data)
    assert len(units) == 3  # 'a' + high surrogate + low surrogate


def test_type_text_chunk_abort(monkeypatch):
    """user_active callable -> abort mid-string with char counts."""
    sent = []
    monkeypatch.setattr(_in, "_send", lambda inputs: sent.append(len(inputs)))
    text = "x" * 45  # 3 chunks of 20/20/5
    calls = {"n": 0}

    def user_active():
        calls["n"] += 1
        return calls["n"] >= 1  # active right after first chunk

    with pytest.raises(WindesktopctlError, match="stopped after 20/45 chars"):
        _in.type_text(text, user_active=user_active)
    assert len(sent) == 1  # only the first chunk went out


def test_type_text_no_abort_when_idle(monkeypatch):
    sent = []
    monkeypatch.setattr(_in, "_send", lambda inputs: sent.append(len(inputs)))
    _in.type_text("x" * 45, user_active=lambda: False)
    assert len(sent) == 3  # 20 + 20 + 5 chars -> 3 SendInput calls
