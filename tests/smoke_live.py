"""Live desktop smoke test - touches the REAL desktop. Run manually:

    .venv\\Scripts\\python tests\\smoke_live.py

Read-only part always runs (windows, observe uia/ocr, screenshot, wait).
Input part runs ONLY if no Notepad process is already running; it launches
its own notepad.exe, verifies foreground before typing, and closes only the
window it launched.
"""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskctl.core import api  # noqa: E402
from deskctl.core import state as st  # noqa: E402
from deskctl.core import windows as win  # noqa: E402
from deskctl.core import worker  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


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


def close_window(hwnd):
    """WM_CLOSE the window; if a save prompt appears, click Don't Save."""
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(20):
        time.sleep(0.4)
        alive = any(w.hwnd == hwnd for w in win.list_windows())
        if not alive:
            return True
        # look for a Don't Save / Save dialog button via observe
        try:
            run(api.observe, str(hwnd), "auto", 100)
            obs = st.get_observe()
            for eid, d in (obs.get("elements") or {}).items():
                name = (d.get("name") or "").lower()
                if "don" in name and "save" in name:
                    run(api.act, "click", str(eid))
                    break
        except Exception:
            pass
    return not any(w.hwnd == hwnd for w in win.list_windows())


def input_test():
    print("\n===== input test: notepad =====")
    existing = [w for w in win.list_windows() if "notepad" in (w.process or "").lower()]
    if existing:
        print(f"SKIPPED: notepad already running (hwnd={existing[0].hwnd}); "
              "not touching it to stay safe")
        return 0

    proc = subprocess.Popen(["notepad.exe"])
    our_pid = proc.pid
    hwnd = 0
    title = ""
    for _ in range(60):
        time.sleep(0.25)
        for w in win.list_windows():
            if "notepad" in (w.process or "").lower() and w.title:
                hwnd, title = w.hwnd, w.title
                break
        if hwnd:
            break
    if not hwnd:
        print("FAIL: notepad window never appeared")
        proc.terminate()
        return 1
    print(f"launched notepad pid={our_pid} hwnd={hwnd} title={title!r}")

    try:
        print(run(api.focus, str(hwnd)))
        time.sleep(1.0)  # let session/tab restore settle before typing
        fg = win.focused_hwnd()
        if fg != hwnd:
            print(f"FAIL: foreground is {fg}, expected {hwnd}; refusing to type")
            return 1
        print("foreground verified = notepad")

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
            print(run(api.act, "click", f"{cx},{cy}"))
            time.sleep(0.5)
        elif eid:
            print(run(api.act, "click", str(eid)))
            time.sleep(0.5)
        # verify keyboard focus is inside the editor before typing
        def _focused():
            import uiautomation as auto

            fc = auto.GetFocusedControl()
            return f"{fc.ControlTypeName} {fc.Name!r}"

        try:
            print("focused control:", run(_focused))
        except Exception as ex:
            print("focused-control check failed:", ex)
        print(run(api.act, "type", None, "hello from deskctl"))
        time.sleep(0.3)
        print(run(api.act, "key", None, None, "ctrl+a"))
        time.sleep(0.5)

        ok, detail = verify_text_present()
        if ok:
            print(f"PASS: typed text verified via {detail}")
        else:
            print("FAIL: typed text not found in observe")
            print(detail)

        print("closing notepad window (WM_CLOSE + Don't Save if prompted)...")
        closed = close_window(hwnd)
        print("closed:", closed)
        # final safety: only kill the process we launched, by pid
        still = [w for w in win.list_windows() if w.hwnd == hwnd]
        if still:
            print("WARN: window still present; terminating our pid", our_pid)
            try:
                proc.terminate()
            except Exception as ex:
                print("terminate failed:", ex)
        return 0 if ok else 1
    finally:
        try:
            proc.poll()
        except Exception:
            pass


def main():
    print("== deskctl smoke_live ==")
    timed("windows()", api.windows)
    timed("observe(focused, auto)", api.observe, "focused", "auto", 150)
    timed("observe(focused, ocr)", api.observe, "focused", "ocr", 150)
    data, info = run(api.screenshot, "focused", 1280, True, "png")
    p = st.state_dir() / "smoke_last.png"
    p.write_bytes(data)
    print(f"\n===== screenshot =====\n{info}\nsaved: {p}")
    timed("wait_for_change(1s)", api.wait_for_change, 1.0, "focused")
    rc = input_test()
    print("\n== smoke_live done ==")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
