"""Unit tests for cli.py --json output shape (api layer mocked)."""

import json

import pytest

from deskctl.cli import main
from deskctl.core import DeskctlError
from deskctl.core import api


@pytest.fixture
def mock_run(monkeypatch):
    """Replace cli._run: observe_json returns canned JSON, everything else a
    marker string (or raises for the error case)."""
    state = {"mode": "ok"}

    def fake_run(fn, *args, **kwargs):
        if state["mode"] == "err":
            raise DeskctlError("boom")
        if fn is api.observe_json:
            return json.dumps({
                "window": {"title": "T", "proc": "p.exe", "hwnd": 1,
                           "rect": [0, 0, 10, 10], "source": "uia",
                           "screen": False},
                "changed": True,
                "truncated": 0,
                "walk_truncated": False,
                "elements": [{"id": 1, "role": "Button", "name": "OK",
                              "value": "", "flags": [], "rect": [0, 0, 5, 5],
                              "center": [2, 2]}],
            })
        if fn is api.observe:
            return 'window: "T" p.exe hwnd=1 rect=0,0,10,10 source=uia changed=true ~tokens=1\n[1] Button "OK" @2,2'
        return "plain-result"

    monkeypatch.setattr("deskctl.cli._run", fake_run)
    return state


def test_json_observe_is_structured(mock_run, capsys):
    rc = main(["--json", "observe"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["window"]["hwnd"] == 1
    assert out["elements"][0]["role"] == "Button"
    assert out["changed"] is True


def test_json_text_command_wraps_result(mock_run, capsys):
    rc = main(["--json", "windows"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": True, "result": "plain-result"}


def test_json_error_shape(mock_run, capsys):
    mock_run["mode"] = "err"
    rc = main(["--json", "status"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "boom" in out["error"]


def test_plain_observe_unchanged(mock_run, capsys):
    rc = main(["observe"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith('window: "T"')
    assert "[1] Button" in out


def test_plain_error_still_stderr(mock_run, capsys):
    mock_run["mode"] = "err"
    rc = main(["status"])
    assert rc == 1
    cap = capsys.readouterr()
    assert "error: boom" in cap.err
    assert cap.out == ""
