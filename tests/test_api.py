"""Unit tests for api.py pure-ish logic: title ambiguity + wait_for matchers.
No real desktop interaction (win.* is mocked where needed)."""

import os
import subprocess

import pytest

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import api
from windesktopctl.core import elements as el
from windesktopctl.core import windows as win


def _win(hwnd, title, proc="app.exe"):
    return win.WindowInfo(hwnd, title, hwnd, proc, (0, 0, 100, 100), False, True)


# ------------------------------------------------------- title resolution --

def test_resolve_title_exact_wins(monkeypatch):
    wins = [
        _win(1, "Notes - App"),
        _win(2, "notes"),          # exact case-insensitive match
        _win(3, "notes extra"),
    ]
    monkeypatch.setattr(win, "list_windows", lambda: wins)
    assert api._resolve_title("NOTES") == 2


def test_resolve_title_unique_substring(monkeypatch):
    wins = [_win(1, "Untitled - Notepad"), _win(2, "Other")]
    monkeypatch.setattr(win, "list_windows", lambda: wins)
    assert api._resolve_title("notepad") == 1


def test_resolve_title_ambiguous_lists_candidates(monkeypatch):
    wins = [_win(i, f"Project {i} - Editor") for i in range(1, 11)]
    monkeypatch.setattr(win, "list_windows", lambda: wins)
    with pytest.raises(WindesktopctlError) as ei:
        api._resolve_title("project")
    msg = str(ei.value)
    assert "ambiguous" in msg
    assert "hwnd=1" in msg and '"Project 1 - Editor"' in msg
    assert "hwnd=8" in msg            # up to 8 candidates listed
    assert "hwnd=9" not in msg        # 9th+ folded into "+N more"
    assert "+2 more" in msg


def test_resolve_title_no_match(monkeypatch):
    monkeypatch.setattr(win, "list_windows", lambda: [_win(1, "Foo")])
    with pytest.raises(WindesktopctlError, match="no visible window"):
        api._resolve_title("zzz")


# ---------------------------------------------------------- wait matchers --

def _elems():
    return [
        el.Element(role="Button", name="Save", value="", rect=(0, 0, 10, 10)),
        el.Element(role="Edit", name="Search", value="hello world",
                   rect=(0, 20, 10, 10)),
        el.Element(role="CheckBox", name="Word wrap", rect=(0, 40, 10, 10),
                   flags=frozenset({"checked"})),
    ]


def test_match_text_substring_case_insensitive():
    hits = api._match_text("HELLO", _elems())
    assert [e.name for e in hits] == ["Search"]
    # matches value too, not just name
    hits = api._match_text("world", _elems())
    assert len(hits) == 1 and hits[0].name == "Search"
    assert api._match_text("nothing-here", _elems()) == []


def test_parse_element_spec():
    assert api._parse_element_spec("Button:Save") == ("button", "save")
    assert api._parse_element_spec("save") == (None, "save")
    assert api._parse_element_spec("no-colon-here") == (None, "no-colon-here")
    # colon without both sides falls back to plain match
    assert api._parse_element_spec(":x") == (None, ":x")


def test_match_element_role_and_substring():
    hits = api._match_element("Button:save", _elems())
    assert [e.name for e in hits] == ["Save"]
    # wrong role -> no match
    assert api._match_element("Edit:save", _elems()) == []
    # plain name substring
    hits = api._match_element("sear", _elems())
    assert [e.name for e in hits] == ["Search"]
    hits = api._match_element("wrap", _elems())
    assert [e.name for e in hits] == ["Word wrap"]
    assert api._match_element("zzz-nope", _elems()) == []


def test_wait_match_gone_uses_element_syntax():
    # gone with "Role:sub" should use element matching, plain uses text
    elems = _elems()
    assert len(api._wait_match("gone", "Button:save", elems)) == 1
    assert api._wait_match("gone", "Button:nope", elems) == []
    assert len(api._wait_match("gone", "save", elems)) == 1  # text fallback


# ----------------------------------------------------------------- launch --

def _mock_launch_env(monkeypatch):
    """Neutralize safety + window polling so launch() returns fast."""
    monkeypatch.setattr(api.safety, "check_before_action", lambda: None)
    monkeypatch.setattr(api.safety, "log_action", lambda rec: None)
    calls = {"n": 0}

    def list_windows():
        calls["n"] += 1
        if calls["n"] == 1:
            return []                       # 'before' snapshot
        return [_win(99, "Launched App", proc="app.exe")]

    monkeypatch.setattr(api.win, "list_windows", list_windows)
    info = _win(99, "Launched App", proc="app.exe")
    monkeypatch.setattr(api.win, "window_info", lambda hwnd: info if hwnd == 99 else None)
    monkeypatch.setattr(api.win, "focused_hwnd", lambda: 0)
    return calls


def _capture_startfile(monkeypatch):
    rec = {}

    def fake(path, operation=None, arguments="", cwd=None, show_cmd=1):
        rec["path"] = path
        rec["cwd"] = cwd

    monkeypatch.setattr(os, "startfile", fake)
    return rec


def _capture_popen(monkeypatch):
    rec = {}

    class FakeProc:
        pid = 4242

    def fake(argv, **kw):
        rec["argv"] = argv
        rec["cwd"] = kw.get("cwd")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake)
    return rec


def test_launch_existing_file_startfile_cwd_is_its_dir(monkeypatch, tmp_path):
    exe = tmp_path / "gamedir" / "game.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    _mock_launch_env(monkeypatch)
    rec = _capture_startfile(monkeypatch)

    out = api.launch(str(exe))

    assert "launched" in out
    assert rec["path"] == str(exe)
    expected = os.path.dirname(os.path.abspath(str(exe)))
    assert os.path.normcase(rec["cwd"]) == os.path.normcase(expected)


def test_launch_url_keeps_inherited_cwd(monkeypatch):
    _mock_launch_env(monkeypatch)
    rec = _capture_startfile(monkeypatch)

    api.launch("https://example.com/")

    assert rec["path"] == "https://example.com/"
    assert rec["cwd"] is None


def test_launch_command_popen_cwd_from_argv0_file(monkeypatch, tmp_path):
    exe = tmp_path / "tools" / "myapp.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    _mock_launch_env(monkeypatch)
    rec = _capture_popen(monkeypatch)

    api.launch(f'"{exe}" --flag value')

    expected = os.path.dirname(os.path.abspath(str(exe)))
    assert os.path.normcase(rec["cwd"]) == os.path.normcase(expected)
    assert rec["argv"][0] == str(exe)


def test_launch_command_popen_cwd_from_path_lookup(monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "tool.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    _mock_launch_env(monkeypatch)
    rec = _capture_popen(monkeypatch)
    monkeypatch.setattr(api.shutil, "which", lambda name: str(exe) if name == "tool.exe" else None)

    api.launch("tool.exe run")

    expected = os.path.dirname(os.path.abspath(str(exe)))
    assert os.path.normcase(rec["cwd"]) == os.path.normcase(expected)


def test_launch_command_unresolvable_argv0_no_cwd(monkeypatch):
    _mock_launch_env(monkeypatch)
    rec = _capture_popen(monkeypatch)
    monkeypatch.setattr(api.shutil, "which", lambda name: None)

    api.launch("definitely-not-a-real-exe-xyz arg")

    assert rec["cwd"] is None
