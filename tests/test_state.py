"""Unit tests for state.py - target parsing, image->screen mapping, staleness."""

import time

import pytest

from deskctl.core import DeskctlError
from deskctl.core import elements as el
from deskctl.core import state as st
from deskctl.core import windows as win


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "deskctl"
    monkeypatch.setenv("DESKCTL_HOME", str(d))
    return d


def _wininfo(hwnd=42, rect=(0, 0, 100, 100), title="T", proc="p.exe"):
    return win.WindowInfo(hwnd, title, 1, proc, rect, False, True)


@pytest.fixture
def live_window(monkeypatch):
    """Mock window_info/focused_hwnd so hwnd=42 exists, is foreground, rect
    (0,0,100,100) - matching what _record() stores."""
    monkeypatch.setattr(win, "window_info", lambda h: _wininfo(int(h)))
    monkeypatch.setattr(win, "focused_hwnd", lambda: 42)
    monkeypatch.setattr(win, "set_foreground", lambda h: True)


def _record(hwnd=42, elements=None, ts=None):
    elems = elements if elements is not None else [
        el.Element(role="Button", name="Save", rect=(100, 200, 20, 20))
    ]
    for i, e in enumerate(elems, 1):
        e.id = i
    st.record_observe(
        hwnd=hwnd, window_key=hwnd, title="T", proc="p.exe",
        rect=(0, 0, 100, 100), source="uia", elements=elems,
        truncated_count=0, walk_truncated=False,
    )
    if ts is not None:
        s = st.load()
        s["observe"]["ts"] = ts
        st.save(s)


def test_observe_roundtrip(home):
    e = el.Element(role="Button", name="Save", rect=(10, 20, 30, 40))
    _record(elements=[e])
    obs = st.get_observe()
    assert obs["hwnd"] == 42
    got = st.get_element(1)
    assert got.name == "Save" and got.rect == (10, 20, 30, 40)
    assert st.get_element(99) is None


def test_resolve_element_id(home, live_window):
    _record()
    x, y, label = st.resolve_target("1")
    assert (x, y) == (110, 210)  # center of rect (100,200,20,20)
    assert "Save" in label and "[1]" in label


def test_resolve_element_stale(home):
    _record(ts=time.time() - 700)
    with pytest.raises(DeskctlError, match="stale"):
        st.resolve_target("1")


def test_stale_id_window_gone(home, monkeypatch):
    _record()
    monkeypatch.setattr(win, "window_info", lambda h: None)
    with pytest.raises(DeskctlError, match="window closed"):
        st.resolve_target("1")


def test_stale_id_window_moved(home, monkeypatch):
    _record()
    monkeypatch.setattr(win, "window_info", lambda h: _wininfo(rect=(0, 0, 200, 200)))
    monkeypatch.setattr(win, "focused_hwnd", lambda: 42)
    with pytest.raises(DeskctlError, match="moved/resized"):
        st.resolve_target("1")


def test_stale_id_refocuses(home, monkeypatch):
    _record()
    calls = []
    monkeypatch.setattr(win, "window_info", lambda h: _wininfo())
    monkeypatch.setattr(win, "focused_hwnd", lambda: 999)  # something else focused
    monkeypatch.setattr(win, "set_foreground", lambda h: calls.append(h) or True)
    x, y, _ = st.resolve_target("1")
    assert calls == [42]
    assert (x, y) == (110, 210)


def test_stale_id_screen_observe_skips_checks(home, monkeypatch):
    """screen observes (hwnd 0) don't run window freshness checks."""
    _record(hwnd=0)
    monkeypatch.setattr(win, "window_info", lambda h: None)
    monkeypatch.setattr(win, "focused_hwnd", lambda: 0)
    x, y, _ = st.resolve_target("1")
    assert (x, y) == (110, 210)


def test_resolve_unknown_id(home):
    _record()
    with pytest.raises(DeskctlError, match="unknown element id"):
        st.resolve_target("77")


def test_resolve_screen_coords(home):
    assert st.resolve_target("123,456") == (123, 456, "123,456")
    assert st.resolve_target(" -5 , 6 ") == (-5, 6, "-5,6")


def test_resolve_image_coords(home):
    st.record_screenshot(
        origin_x=100, origin_y=50, scale=2.0, width=640, height=360, window_key="screen"
    )
    x, y, label = st.resolve_target("i:10,20")
    assert (x, y) == (120, 90)  # 100+10*2, 50+20*2
    assert label == "i:10,20"
    x, y, _ = st.resolve_target("I:0,0")
    assert (x, y) == (100, 50)


def test_resolve_image_stale(home):
    st.record_screenshot(
        origin_x=0, origin_y=0, scale=1.0, width=10, height=10, window_key="screen"
    )
    s = st.load()
    s["screenshot"]["ts"] = time.time() - 700
    st.save(s)
    with pytest.raises(DeskctlError, match="stale"):
        st.resolve_target("i:1,1")


def test_resolve_bad_target(home):
    with pytest.raises(DeskctlError, match="unrecognized target"):
        st.resolve_target("click here")
    with pytest.raises(DeskctlError, match="unrecognized target"):
        st.resolve_target("1,2,3")


def test_baseline_roundtrip(home):
    data = b"\x00" * (64 * 36)
    st.set_baseline(data)
    assert st.get_baseline() == data
    assert st.get_baseline() is not None


def test_no_state_returns_empty(home):
    assert st.load() == {}
    assert st.get_observe() is None
    assert st.get_screenshot() is None
    assert st.get_baseline() is None
    assert st.observe_age() is None
    assert st.screenshot_age() is None
