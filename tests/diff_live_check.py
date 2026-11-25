"""Live check for cell-based change detection (diff.py v2).

Launches `notepad.exe <our own temp file>`, types "abc" via act() and
expects changed=true, then does a no-op move and expects changed=false.
All input goes through NotepadSession.act -> assert_safe_target, so restored
session tabs are never touched. See _live_notepad.py for the safety model.

    .venv\\Scripts\\python tests\\diff_live_check.py
"""

import os
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
from windesktopctl.core import api  # noqa: E402
from windesktopctl.core import input as _in  # noqa: E402
from windesktopctl.core import state as st  # noqa: E402
from windesktopctl.core import windows as win  # noqa: E402


def main() -> int:
    path = live.make_temp_file(prefix="windesktopctl_diff_")
    base = os.path.basename(path)
    print(f"temp file: {path}")
    sess = None
    rc = 1
    try:
        sess = live.NotepadSession(path).launch().focus()
        hwnd = sess.hwnd
        print(f"notepad hwnd={hwnd} pid={sess.pid} shared={sess.preexisting}")

        # click into the editor so typing lands in the document
        live.run(api.observe, "focused", "auto", 120)
        obs = st.get_observe()
        rect = None
        for d in (obs.get("elements") or {}).values():
            if d.get("role") in ("Edit", "Document"):
                rect = d.get("rect")
        if rect and rect[2] > 0:
            print(sess.act("click", f"{rect[0] + 80},{rect[1] + 50}"))
            time.sleep(0.5)

        r1 = sess.act("type", None, "abc")
        print("type:", r1)
        ok1 = "changed=true" in r1
        print("type changed=true?", ok1)

        # no-op move to the CURRENT cursor position
        cx, cy = _in.cursor_pos()
        r2 = sess.act("move", f"{cx},{cy}")
        print("noop move:", r2)
        ok2 = "changed=false" in r2
        print("noop changed=false?", ok2)

        rc = 0 if (ok1 and ok2) else 1
        print("RESULT:", "PASS" if rc == 0 else "CHECK OUTPUT ABOVE")
    except live.UnsafeTarget as ex:
        print("ABORT (unsafe target):", ex)
        return 1
    finally:
        if sess is not None:
            sess.close()
            if sess.hwnd and sess.pid and not sess.preexisting:
                still = [w for w in win.list_windows() if w.hwnd == sess.hwnd]
                if still:
                    print("WARN: our Notepad window is still open (hwnd={}); close it manually. "
                          "Not force-killing: the process may host other Notepad windows.".format(sess.hwnd))
        live.cleanup_temp(path)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
