"""Unit tests for challenge detection/relay + browser helpers.

Pure logic - no real desktop interaction (win/uia/capture mocked).
"""

from __future__ import annotations

import pytest

from deskctl.core import DeskctlError
from deskctl.core import api
from deskctl.core import challenge as ch
from deskctl.core import elements as el
from deskctl.core import state as st
from deskctl.core import windows as win

WIN = (0, 0, 1200, 800)


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "deskctl"
    monkeypatch.setenv("DESKCTL_HOME", str(d))
    return d


def _el(role, name, rect=(0, 0, 50, 20), value=""):
    return el.Element(role=role, name=name, value=value, rect=rect)


# ------------------------------------------------------- detect_challenge --

def test_detect_grid_kind_and_fallback_rect():
    elems = [
        _el("Text", "Verify you are human"),
        _el("Text", "Select all squares with traffic lights", (0, 30, 400, 20)),
        _el("Button", "tile 1", (100, 100, 100, 100)),
    ]
    d = ch.detect_challenge("Some Page - Google Chrome", elems, WIN)
    assert d is not None and d["kind"] == "grid"
    # union of the two text rects +40 pad is < MIN_RECT_H -> content fallback
    assert d["rect"] == WIN and d["fallback"] is True
    assert len(d["matched"]) == 2


def test_detect_grid_keeps_large_union():
    elems = [
        _el("Text", "Select all images with cars", (0, 0, 600, 60)),
        _el("Text", "click each image that matches", (0, 80, 600, 60)),
        _el("Text", "select all squares", (0, 140, 600, 40)),
    ]
    d = ch.detect_challenge("x", elems, WIN)
    assert d["kind"] == "grid"
    assert d["fallback"] is False
    assert d["rect"][2] >= 600 and d["rect"][3] >= 170


def test_detect_title_only_unknown():
    d = ch.detect_challenge("Just a moment...", [], WIN)
    assert d is not None
    assert d["kind"] == "unknown"
    assert d["rect"] == WIN and d["matched"] == []


def test_detect_text_kind_by_needle():
    elems = [
        _el("Text", "Verify you are human"),
        _el("Text", "Type the characters you see", (0, 40, 300, 20)),
        _el("Edit", "answer", (0, 80, 200, 30)),
    ]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "text"


def test_detect_text_kind_by_edit_in_region():
    # a matched element + an Edit inside the region -> text kind even
    # without text-entry needles
    elems = [
        _el("Text", "recaptcha badge", (0, 0, 200, 20)),
        _el("Edit", "field", (100, 100, 200, 30)),
    ]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "text"


def test_detect_press_hold():
    elems = [_el("Text", "Press and hold to verify", (0, 0, 300, 30))]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "press_hold"


def test_detect_checkbox_needs_checkbox_element():
    elems = [
        _el("Text", "Verify you are human", (0, 0, 200, 20)),
        _el("CheckBox", "I'm not a robot", (0, 40, 200, 30)),
    ]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "checkbox"


def test_detect_checkbox_via_turnstile():
    elems = [_el("Text", "turnstile widget", (0, 0, 200, 20))]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "checkbox"


def test_detect_unknown_for_recaptcha_text():
    elems = [_el("Text", "recaptcha badge", (0, 0, 200, 20))]
    d = ch.detect_challenge("Page", elems, WIN)
    assert d["kind"] == "unknown"


def test_detect_no_false_positive_on_robot_prose():
    elems = [
        _el("Text", "A robot is a machine that automates tasks."),
        _el("Text", "Verify your email address to continue."),
        _el("Button", "Read more"),
    ]
    assert ch.detect_challenge("Daily News", elems, WIN) is None
    assert ch.detect_challenge("Are you a robot? The movie", [], WIN) is not None  # title phrase DOES hit


def test_detect_case_insensitive():
    elems = [_el("Text", "SELECT ALL SQUARES WITH TRAFFIC LIGHTS")]
    d = ch.detect_challenge("x", elems, WIN)
    assert d is not None and d["kind"] == "grid"


# --------------------------------------------------------- grid / cells --

def test_parse_cells():
    assert ch.parse_cells("b3, C4 D2", 6, 8) == [(1, 2), (2, 3), (3, 1)]
    assert ch.parse_cells("A1", 1, 1) == [(0, 0)]
    assert ch.parse_cells("  b3  c4  ", 6, 8) == [(1, 2), (2, 3)]


def test_parse_cells_errors():
    with pytest.raises(ValueError, match="bad cell"):
        ch.parse_cells("hello", 4, 4)
    with pytest.raises(ValueError, match="out of range"):
        ch.parse_cells("Z9", 4, 4)
    with pytest.raises(ValueError, match="no cells"):
        ch.parse_cells("", 4, 4)
    with pytest.raises(ValueError, match="out of range"):
        ch.parse_cells("A5", 4, 4)  # row 5 > 4 rows


def test_cell_center_and_label():
    grid = {"rect": [100, 200, 400, 300], "rows": 4, "cols": 8}
    x, y = ch.cell_center(grid, 1, 2)
    assert x == 100 + int(2.5 * 400 / 8)
    assert y == 200 + int(1.5 * 300 / 4)
    assert ch.cell_label(1, 2) == "B3"
    assert ch.cell_label(0, 0) == "A1"


def test_grid_dims_defaults_and_overrides():
    r, c = ch.grid_dims((0, 0, 800, 400))
    assert r == round(400 / 70) and c == round(800 / 70)
    assert ch.grid_dims((0, 0, 800, 400), rows=4, cols=8) == (4, 8)
    # caps
    assert ch.grid_dims((0, 0, 99999, 99999))[0] <= ch.MAX_ROWS


def test_overlay_grid_draws_and_preserves_size():
    from PIL import Image

    img = Image.new("RGB", (400, 300), (255, 255, 255))
    out = ch.overlay_grid(img, (100, 100, 400, 300), (100, 100), 3, 4)
    assert out.size == (400, 300) and out.mode == "RGB"
    px = out.load()
    # grid lines are red-ish - at least one non-white pixel exists
    nonwhite = any(
        px[x, y] != (255, 255, 255)
        for x in range(0, 400, 7)
        for y in range(0, 300, 7)
    )
    assert nonwhite


# ------------------------------------------------------------- webhook --

def test_webhook_multipart_body():
    ctype, body = ch.webhook_multipart("challenge.png", b"\x89PNGDATA", "hello user")
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    assert body.startswith(b"--" + boundary)
    assert body.rstrip().endswith(b"--" + boundary + b"--")
    assert b'name="content"' in body and b"hello user" in body
    assert b'filename="challenge.png"' in body
    assert b"\x89PNGDATA" in body
    assert b"Content-Type: image/png" in body


def test_notify_webhook_posts_multipart(home, monkeypatch):
    captured = {}

    class FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["ctype"] = req.headers.get("Content-type")
        captured["body"] = req.data
        return FakeResp()

    monkeypatch.setenv("DESKCTL_NOTIFY_WEBHOOK", "https://discord.example/hook")
    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    warn = api._notify_webhook({"id": 3, "kind": "grid", "title": "T"}, b"PNGBYTES")
    assert warn is None
    assert captured["url"] == "https://discord.example/hook"
    assert b"PNGBYTES" in captured["body"]
    assert b"challenge id=3" in captured["body"]


def test_notify_webhook_unset_and_failure(home, monkeypatch):
    monkeypatch.delenv("DESKCTL_NOTIFY_WEBHOOK", raising=False)
    assert api._notify_webhook({"id": 1}, b"x") is None
    monkeypatch.setenv("DESKCTL_NOTIFY_WEBHOOK", "https://x.example/")

    def boom(req, timeout=0):
        raise OSError("no route")

    monkeypatch.setattr(api.urllib.request, "urlopen", boom)
    warn = api._notify_webhook({"id": 1, "kind": "grid", "title": "T"}, b"x")
    assert warn and "webhook notify failed" in warn
    assert "https://x.example" not in warn  # URL never leaks into messages


# ------------------------------------------------- challenge state / gate --

def _challenge_rec(hwnd=42, rect=(0, 0, 100, 100)):
    return {
        "hwnd": hwnd,
        "window_rect": [0, 0, 800, 600],
        "rect": list(rect),
        "kind": "grid",
        "grid": {"rect": list(rect), "rows": 4, "cols": 4},
        "title": "Verify you are human",
    }


def test_active_challenge_age(home):
    rec = st.record_challenge(_challenge_rec())
    assert st.active_challenge()["id"] == rec["id"]
    # age it out
    data = st.load()
    data["challenge"]["ts"] = 0
    st.save(data)
    assert st.active_challenge() is None
    assert st.get_challenge() is not None  # still stored, just stale


def test_set_challenge_preserves_id_same_window(home):
    a = st.set_challenge(_challenge_rec(hwnd=42))
    b = st.set_challenge(_challenge_rec(hwnd=42))
    c = st.set_challenge(_challenge_rec(hwnd=43))
    assert a["id"] == b["id"] != c["id"]


def test_act_refused_when_challenge_foreground(home, monkeypatch):
    st.record_challenge(_challenge_rec(hwnd=42))
    monkeypatch.setattr(win, "focused_hwnd", lambda: 42)
    monkeypatch.setattr(api.safety, "check_before_action", lambda: None)
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api.act("click", "100,100")
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api.act("type", None, "hello")


def test_act_refused_target_inside_challenge_rect(home, monkeypatch):
    st.record_challenge(_challenge_rec(hwnd=42, rect=(100, 100, 200, 200)))
    monkeypatch.setattr(win, "focused_hwnd", lambda: 7)  # elsewhere focused
    monkeypatch.setattr(api.safety, "check_before_action", lambda: None)
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api.act("click", "150,150")
    # ...but a coord target outside the rect is allowed through the gate
    monkeypatch.setattr(api, "_do_act", lambda *a, **k: ("ok: mocked", None, {}))
    monkeypatch.setattr(api, "_act_region", lambda: (0, 0, 10, 10))
    monkeypatch.setattr(api, "_region_thumb", lambda r: None)
    out = api.act("click", "900,900")
    assert "ok: mocked" in out


def test_act_refused_element_id_of_challenge_window(home, monkeypatch):
    st.record_challenge(_challenge_rec(hwnd=42))
    monkeypatch.setattr(win, "focused_hwnd", lambda: 7)
    monkeypatch.setattr(api.safety, "check_before_action", lambda: None)
    monkeypatch.setattr(st, "get_observe", lambda: {"hwnd": 42, "elements": {}})
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api.act("click", "3")


def test_window_and_browser_gated(home, monkeypatch):
    st.record_challenge(_challenge_rec(hwnd=42))
    monkeypatch.setattr(api.safety, "check_before_action", lambda: None)
    fake_w = {
        "screen": False, "hwnd": 42, "title": "Verify you are human",
        "proc": "chrome.exe", "rect": (0, 0, 800, 600), "pid": 1,
    }
    monkeypatch.setattr(
        api, "_resolve_window", lambda spec, allow_iconic=False: fake_w
    )
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api.window("minimize", "42")
    with pytest.raises(DeskctlError, match="challenge on screen"):
        api._browser_navigate(fake_w, "https://example.com")


def test_challenge_answer_stale_id(home):
    rec = st.record_challenge(_challenge_rec())
    with pytest.raises(DeskctlError, match="stale challenge id"):
        api.challenge_answer(rec["id"] + 100)


def test_challenge_answer_expired(home, monkeypatch):
    rec = st.record_challenge(_challenge_rec())
    monkeypatch.setattr(st, "CHALLENGE_MAX_AGE", 0)
    with pytest.raises(DeskctlError, match="stale challenge id"):
        api.challenge_answer(rec["id"])


def test_point_in_rect():
    assert api._point_in_rect(50, 50, [0, 0, 100, 100])
    assert not api._point_in_rect(100, 50, [0, 0, 100, 100])
    assert not api._point_in_rect(50, 50, None)


# ------------------------------------------------------- browser helpers --

def test_format_dom_text_headings_links_dedup():
    recs = [
        {"role": "Text", "name": "Welcome", "value": "", "localized": "", "aria": ""},
        {"role": "Text", "name": "Welcome", "value": "", "localized": "", "aria": ""},
        {"role": "Text", "name": "Intro paragraph", "value": "", "localized": "", "aria": ""},
        {"role": "Text", "name": "Big Title", "value": "", "localized": "heading", "aria": ""},
        {"role": "Text", "name": "Aria Head", "value": "", "localized": "", "aria": "heading"},
        {"role": "Hyperlink", "name": "Click here", "value": "", "localized": "", "aria": ""},
        {"role": "Button", "name": "Sign in", "value": "", "localized": "", "aria": ""},
        {"role": "Text", "name": "", "value": "", "localized": "", "aria": ""},
        {"role": "Group", "name": "layout", "value": "", "localized": "", "aria": ""},
    ]
    lines = api._format_dom_text(recs, 4000).splitlines()
    assert lines.count("Welcome") == 1               # consecutive dup collapsed
    assert "# Big Title" in lines                    # localized heading
    assert "# Aria Head" in lines                     # aria heading
    assert "[Click here]" in lines                    # link
    assert "Sign in" in lines                         # interactive name as plain line
    assert "layout" not in lines                      # Group not a text role
    assert "" not in lines


def test_format_dom_text_truncation():
    recs = [
        {"role": "Text", "name": f"line {i} " + "x" * 40, "value": "",
         "localized": "", "aria": ""}
        for i in range(60)
    ]
    out = api._format_dom_text(recs, 300)
    assert "truncated" in out and "more chars" in out
    assert len(out) < 500


def test_resolve_tab_index_title_and_ambiguity():
    tabs = [
        {"name": "Alpha", "rect": (0, 0, 1, 1), "selected": True, "control": None},
        {"name": "Beta page", "rect": (0, 0, 1, 1), "selected": False, "control": None},
        {"name": "Beta other", "rect": (0, 0, 1, 1), "selected": False, "control": None},
    ]
    assert api._resolve_tab(tabs, "t2")["name"] == "Beta page"
    assert api._resolve_tab(tabs, "1")["name"] == "Alpha"
    assert api._resolve_tab(tabs, "alpha")["name"] == "Alpha"   # exact wins
    assert api._resolve_tab(tabs, "beta page")["name"] == "Beta page"
    with pytest.raises(DeskctlError, match="ambiguous tab"):
        api._resolve_tab(tabs, "beta")
    with pytest.raises(DeskctlError, match="out of range"):
        api._resolve_tab(tabs, "t9")
    with pytest.raises(DeskctlError, match="no tab matching"):
        api._resolve_tab(tabs, "zzz")
    with pytest.raises(DeskctlError, match="requires a value"):
        api._resolve_tab(tabs, "")


# ------------------------------------------------------------- dom retry --

def _fake_window(proc="chrome.exe", hwnd=42):
    return {
        "screen": False, "hwnd": hwnd, "title": "T - Google Chrome",
        "proc": proc, "rect": (0, 0, 800, 600), "pid": 1,
    }


def _mock_observe_env(monkeypatch, elems_seq):
    """Mock everything observe_data touches outside _uia_elements."""
    from PIL import Image

    monkeypatch.setattr(api, "_resolve_window", lambda spec: _fake_window())
    monkeypatch.setattr(
        api.capture, "grab_rect", lambda *r: (Image.new("RGB", (8, 8)), (0, 0))
    )
    monkeypatch.setattr(api, "_changed_and_store_baseline", lambda img: True)
    monkeypatch.setattr(api.uia, "dom_root_rect", lambda hwnd: None)
    calls = {"n": 0}

    def fake_uia(hwnd, rect, dom=False):
        calls["n"] += 1
        return elems_seq[min(calls["n"] - 1, len(elems_seq) - 1)], False

    monkeypatch.setattr(api, "_uia_elements", fake_uia)
    return calls


def test_dom_retry_on_sparse_tree(home, monkeypatch):
    seq = [
        [],
        [],
        [el.Element(role="Text", name=f"e{i}", rect=(0, 0, 5, 5)) for i in range(5)],
    ]
    calls = _mock_observe_env(monkeypatch, seq)
    d = api.observe_data("42", "auto", 50, dom=True)
    assert calls["n"] == 3
    assert any("warming up, retried 2" in n for n in d["notes"])


def test_dom_no_retry_when_populated(home, monkeypatch):
    seq = [[el.Element(role="Text", name=f"e{i}", rect=(0, 0, 5, 5)) for i in range(6)]]
    calls = _mock_observe_env(monkeypatch, seq)
    d = api.observe_data("42", "auto", 50, dom=True)
    assert calls["n"] == 1
    assert not any("warming up" in n for n in d["notes"])


def test_observe_sets_and_clears_challenge(home, monkeypatch):
    doc = el.Element(role="Document", name="Page", rect=(0, 0, 800, 600))
    challenge_elems = [
        doc,
        el.Element(role="Text", name="Verify you are human", rect=(0, 0, 200, 20)),
    ]
    _mock_observe_env(monkeypatch, [challenge_elems])
    d = api.observe_data("42", "uia", 50)
    assert any("!! challenge detected" in n for n in d["notes"])
    chst = st.get_challenge()
    assert chst is not None and chst["hwnd"] == 42

    # a later observe of the same hwnd without detection clears it
    clean = [[doc, el.Element(role="Text", name="hello", rect=(0, 0, 5, 5))]]
    _mock_observe_env(monkeypatch, clean)
    api.observe_data("42", "uia", 50)
    assert st.get_challenge() is None


def test_browser_observe_retries_until_document(home, monkeypatch):
    """Non-dom browser observe before the web a11y tree exists sees only
    chrome (no Document) -> retries the UIA walk."""
    seq = [
        [el.Element(role="Button", name="New Tab", rect=(0, 0, 5, 5))],
        [
            el.Element(role="Document", name="Page", rect=(0, 0, 50, 50)),
            el.Element(role="Text", name="hi", rect=(0, 0, 5, 5)),
        ],
    ]
    calls = _mock_observe_env(monkeypatch, seq)
    d = api.observe_data("42", "uia", 50)
    assert calls["n"] == 2
    assert any("warming up, retried 1" in n for n in d["notes"])


def test_observe_no_detection_on_non_browser(home, monkeypatch):
    _mock_observe_env(monkeypatch, [
        [el.Element(role="Text", name="Verify you are human", rect=(0, 0, 5, 5))]
    ])
    monkeypatch.setattr(api, "_resolve_window",
                        lambda spec: _fake_window(proc="notepad.exe"))
    d = api.observe_data("42", "uia", 50)
    assert st.get_challenge() is None
    assert not any("challenge" in n for n in d["notes"])
