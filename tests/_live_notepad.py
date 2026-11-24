"""Shared helpers for live scripts that type into Notepad.

SAFETY: Windows 11 Notepad restores previous session tabs, so a bare
`notepad.exe` launch can put the cursor in the user's restored documents.
We therefore:

- launch `notepad.exe <our own empty temp file>` so the tab belongs to us;
- re-verify the FOREGROUND window title contains that file's basename via
  assert_safe_target() before EVERY input action (click/type/key/move);
- close only our tab (shared window -> ctrl+w) or our window (dedicated ->
  ctrl+s + WM_CLOSE), never a window we didn't create.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deskctl.core import api  # noqa: E402
from deskctl.core import state as st  # noqa: E402
from deskctl.core import windows as win  # noqa: E402
from deskctl.core import worker  # noqa: E402

user32 = ctypes.windll.user32
WM_CLOSE = 0x0010


def run(fn, *a, **k):
    return worker.run(fn, *a, **k)


class UnsafeTarget(RuntimeError):
    """Foreground window is not our temp-file Notepad - abort all input."""


def make_temp_file(prefix: str = "deskctl_smoke_") -> str:
    """Create an empty temp .txt and return its path (caller deletes it)."""
    fd, path = tempfile.mkstemp(
        prefix=prefix, suffix=".txt", dir=os.environ.get("TEMP") or None
    )
    os.close(fd)
    return path


def assert_safe_target(basename: str):
    """Hard guard: the FOREGROUND window title must contain our temp file's
    basename (e.g. 'deskctl_smoke_ab12cd.txt - Notepad'), proving typed text
    lands in our file - not a restored session tab. Returns the hwnd."""
    fg = win.focused_hwnd()
    info = win.window_info(fg) if fg else None
    title = (info.title if info else "") or ""
    if basename.lower() not in title.lower():
        raise UnsafeTarget(
            f"foreground hwnd={fg} title {title!r} does not contain "
            f"{basename!r} - refusing to send input"
        )
    return fg


def find_window_by_title(text: str, timeout: float = 10.0) -> int:
    """Poll list_windows for a title containing text; return hwnd or 0."""
    deadline = time.monotonic() + timeout
    while True:
        for w in win.list_windows():
            if text.lower() in (w.title or "").lower():
                return w.hwnd
        if time.monotonic() >= deadline:
            return 0
        time.sleep(0.25)


def close_window_dont_save(hwnd: int) -> bool:
    """WM_CLOSE a window; if a save prompt appears, click Don't Save through
    deskctl itself (dialog buttons are safe: we initiated this close)."""
    try:
        run(api.window, "close", str(hwnd))
    except Exception as ex:
        print("window close failed, falling back to PostMessageW:", ex)
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    for _ in range(20):
        time.sleep(0.4)
        if not any(w.hwnd == hwnd for w in win.list_windows()):
            return True
        try:
            run(api.observe, str(hwnd), "auto", 100)
            obs = st.get_observe() or {}
            for eid, d in (obs.get("elements") or {}).items():
                name = (d.get("name") or "").lower()
                if "don" in name and "save" in name:
                    run(api.act, "click", str(eid))
                    break
        except Exception:
            pass
    return not any(w.hwnd == hwnd for w in win.list_windows())


class NotepadSession:
    """Our own `notepad.exe <tempfile>` window or tab."""

    def __init__(self, path: str):
        self.path = path
        self.basename = os.path.basename(path)
        self.hwnd = 0
        self.pid = 0
        self.preexisting = False  # hwnd existed before launch -> shared window

    def launch(self) -> "NotepadSession":
        before = {w.hwnd for w in win.list_windows()}
        out = run(api.launch, f'notepad.exe "{self.path}"')
        print("launch:", out)
        m = re.search(r"hwnd=(\d+)", out)
        hwnd = int(m.group(1)) if m else 0
        if hwnd and hwnd in before:
            self.preexisting = True
        if not hwnd:
            # no NEW top-level window: our file likely opened as a tab in an
            # existing Notepad window - find it by its title
            hwnd = find_window_by_title(self.basename, timeout=10)
        if not hwnd:
            raise RuntimeError(
                f"could not find a window titled with {self.basename!r}"
            )
        self.hwnd = hwnd
        self.preexisting = self.preexisting or hwnd in before
        info = win.window_info(hwnd)
        self.pid = info.pid if info else 0
        title = info.title if info else "?"
        print(
            f"our notepad: hwnd={hwnd} pid={self.pid} "
            f"shared_window={self.preexisting} title={title!r}"
        )
        return self

    def focus(self) -> "NotepadSession":
        run(api.focus, str(self.hwnd))
        # foreground title must contain OUR file's basename
        run(api.wait_for, "window", self.basename, 10.0, "focused")
        time.sleep(0.8)  # let the tab/document settle
        assert_safe_target(self.basename)
        return self

    def act(self, *args, **kwargs) -> str:
        """api.act guarded by assert_safe_target before every input action."""
        assert_safe_target(self.basename)
        return run(api.act, *args, **kwargs)

    def close(self) -> bool:
        """Save our file, then close ONLY our tab (shared window) or window.
        Every key press is preceded by the title guard."""
        try:
            assert_safe_target(self.basename)
            run(api.act, "key", None, None, "ctrl+s")  # save ours -> no prompt
            time.sleep(0.4)
            assert_safe_target(self.basename)
            if self.preexisting:
                # shared Notepad window: close just our tab, never the window
                run(api.act, "key", None, None, "ctrl+w")
                time.sleep(0.6)
                info = win.window_info(self.hwnd)
                title = (info.title if info else "") or ""
                if self.basename.lower() not in title.lower():
                    print("our tab closed (ctrl+w); shared window left open")
                    return True
                print(
                    "WARN: ctrl+w did not close our tab; leaving the shared "
                    "window untouched rather than risk other tabs"
                )
                return False
            return close_window_dont_save(self.hwnd)
        except UnsafeTarget as ex:
            print("cleanup skipped (unsafe target):", ex)
            return False
        except Exception as ex:
            print("cleanup error:", ex)
            return False


def cleanup_temp(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
