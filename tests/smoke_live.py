"""Live desktop smoke test - touches the REAL desktop. Run manually:

    .venv\\Scripts\\python tests\\smoke_live.py

Read-only part always runs (windows, observe uia/ocr, screenshot, wait_for).
Input part launches `notepad.exe <our own temp file>` and types ONLY while
the foreground title contains that file's basename (assert_safe_target before
every input action) - restored session tabs are never touched. See
_live_notepad.py for the safety model.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root

import _live_notepad as live  # noqa: E402
from deskctl.core import api  # noqa: E402
from deskctl.core import state as st  # noqa: E402
from deskctl.core import windows as win  # noqa: E402
from deskctl.core import worker  # noqa: E402


def run(fn, *a, **k):
    return worker.run(fn, *a, **k)


def timed(label, fn, *a, **k):
    t = time.time()
    out = run(fn, *a, **k)
    dt = time.time() - t
    print(f"\n===== {label} ({dt:.2f}s) =====")
    print(out)
    return out


def find_editor_element():
    """Return (id, rect) of an Edit/Document control from last observe."""
    obs = st.get_observe()
    if not obs:
        return None, None
    best = None
    for eid, d in (obs.get("elements") or {}).items():
        if d.get("role") in ("Edit", "Document"):
            best = (int(eid), d.get("rect") or [0, 0, 0, 0])
    return best if best else (None, None)


def _char_count():
    """Largest 'N characters' / 'N of M characters' total in last observe."""
    import re

    obs = st.get_observe()
    best = 0
    for d in (obs.get("elements") or {}).values():
        nm = d.get("name") or ""
        for m in re.finditer(r"(\d+)\s+of\s+(\d+)\s+characters", nm):
            best = max(best, int(m.group(2)))
        for m in re.finditer(r"(\d+)\s+characters", nm):
            best = max(best, int(m.group(1)))
    return best


def verify_text_present(text="hello from deskctl", min_chars=15):
    """Fresh observe; PASS if doc value/name contains text OR char count grew."""
    needle = text.lower()
    before = _char_count()
    out = run(api.observe, "focused", "auto", 200)
    obs = st.get_observe()
    for d in (obs.get("elements") or {}).values():
        blob = (d.get("name", "") + " " + d.get("value", "")).lower()
        if needle in blob:
            return True, "uia element value/name"
    after = _char_count()
    if before and after > before + min_chars:
        return True, f"char count {before} -> {after}"
    out2 = run(api.observe, "focused", "ocr", 200)
    if needle in out2.lower():
        return True, "ocr text"
    return False, out[:800] + "\n---OCR---\n" + out2[:800]


def input_test():
    """Type into OUR OWN notepad.exe <tempfile> - never a restored tab.
    assert_safe_target runs before every input action via sess.act()."""
    print("\n===== input test: notepad (own temp file) =====")
    path = live.make_temp_file()
    base = os.path.basename(path)
    print(f"temp file: {path}")
    sess = None
    rc = 1
    try:
        sess = live.NotepadSession(path).launch().focus()
        hwnd = sess.hwnd
        print(f"notepad hwnd={hwnd} pid={sess.pid} shared={sess.preexisting}")

        timed("observe notepad", api.observe, "focused", "auto", 120)
        eid, rect = find_editor_element()
        print("editor element id:", eid, "rect:", rect)
        chars_before = _char_count()
        print("char count before typing:", chars_before)
        if rect and rect[2] > 0:
            # click near the TOP of the editor so typed text lands early in
            # the (80-char-truncated) document value
            cx, cy = rect[0] + 80, rect[1] + 50
            print(f"clicking editor at {cx},{cy}")
            print(sess.act("click", f"{cx},{cy}"))
            time.sleep(0.5)
        elif eid:
            print(sess.act("click", str(eid)))
            time.sleep(0.5)
        # verify keyboard focus is inside the editor before typing
        def _focused():
            import uiautomation as auto

            fc = auto.GetFocusedControl()
            return f"{fc.ControlTypeName} {fc.Name!r}"

        try:
            print("focused control:", live.run(_focused))
        except Exception as ex:
            print("focused-control check failed:", ex)

        # the clicked editor should now carry the `focused` flag
        live.run(api.observe, "focused", "auto", 120)
        obs = st.get_observe()
        focused_hit = None
        for eid2, d in (obs.get("elements") or {}).items():
            if "focused" in (d.get("flags") or []):
                focused_hit = (eid2, d.get("role"), d.get("name"))
        print("focused-flag element:", focused_hit)
        if focused_hit is None:
            print("WARN: no element shows the focused flag")

        print(sess.act("type", None, "hello from deskctl"))
        time.sleep(0.3)
        print(sess.act("key", None, None, "ctrl+a"))
        time.sleep(0.5)

        ok, detail = verify_text_present()
        if ok:
            print(f"PASS: typed text verified via {detail}")
        else:
            print("FAIL: typed text not found in observe")
            print(detail)

        rc = 0 if ok else 1
        return rc
    except live.UnsafeTarget as ex:
        print("ABORT (unsafe target):", ex)
        return 1
    finally:
        if sess is not None:
            print("closing our notepad tab/window...")
            sess.close()
            # final safety net for a dedicated window we created
            if sess.hwnd and sess.pid and not sess.preexisting:
                still = [w for w in win.list_windows() if w.hwnd == sess.hwnd]
                if still:
                    print("WARN: our Notepad window is still open (hwnd={}); close it manually. "
                          "Not force-killing: the process may host other Notepad windows.".format(sess.hwnd))
        live.cleanup_temp(path)


def main():
    print("== deskctl smoke_live ==")
    timed("windows()", api.windows)
    timed("observe(focused, auto)", api.observe, "focused", "auto", 150)
    timed("observe(focused, ocr)", api.observe, "focused", "ocr", 150)
    data, info = run(api.screenshot, "focused", 1280, True, "png")
    p = st.state_dir() / "smoke_last.png"
    p.write_bytes(data)
    print(f"\n===== screenshot =====\n{info}\nsaved: {p}")
    timed("wait_for(changed, 1s)", api.wait_for, "changed", None, 1.0, "focused")
    rc = input_test()
    print("\n== smoke_live done ==")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
