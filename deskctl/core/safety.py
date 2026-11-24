"""Safety: pause flag, JSONL action log, failsafe corner, hotkey guard."""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from ctypes import wintypes as wt
from datetime import datetime, timezone

from deskctl.core import dpi
from deskctl.core import state as st
from deskctl.core import input as _input
from deskctl.core import DeskctlError

user32 = ctypes.windll.user32


def _pause_file():
    return st.state_dir() / "paused"


def is_paused() -> bool:
    return _pause_file().exists()


def pause() -> None:
    _pause_file().write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def resume() -> None:
    try:
        _pause_file().unlink()
    except FileNotFoundError:
        pass


def toggle() -> bool:
    if is_paused():
        resume()
        return False
    pause()
    return True


def log_action(record: dict) -> None:
    p = st.state_dir() / "actions.log"
    record = dict(record)
    record.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def is_failsafe_position(x: int, y: int) -> bool:
    """True if (x,y) is within 2px of the virtual-screen top-left corner."""
    vx, vy, _vw, _vh = dpi.virtual_screen()
    return abs(int(x) - vx) <= 2 and abs(int(y) - vy) <= 2


def check_before_action() -> None:
    """Gate every act(): refuse while paused; slamming the cursor into the
    top-left corner failsafes (refuses and pauses)."""
    if is_paused():
        raise DeskctlError("paused - Ctrl+Alt+Shift+K toggles pause; `deskctl resume` clears it")
    x, y = _input.cursor_pos()
    if is_failsafe_position(x, y):
        pause()
        raise DeskctlError(
            f"failsafe triggered: cursor at screen corner ({x},{y}); automation paused"
        )


# ----------------------------------------------------------- hotkey guard --

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 1
_VK_K = 0x4B

_hotkey_lock = threading.Lock()
_hotkey_started = False


def start_hotkey_guard() -> bool:
    """Start the Ctrl+Alt+Shift+K pause-toggle hotkey on a daemon thread.
    Idempotent; returns True if a thread was started."""
    global _hotkey_started
    with _hotkey_lock:
        if _hotkey_started:
            return False
        _hotkey_started = True
    t = threading.Thread(target=_hotkey_loop, name="deskctl-hotkey", daemon=True)
    t.start()
    return True


def _hotkey_loop() -> None:
    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int

    ok = user32.RegisterHotKey(
        None, _HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, _VK_K
    )
    if not ok:
        print("deskctl: failed to register Ctrl+Alt+Shift+K hotkey", file=sys.stderr, flush=True)
        return
    msg = wt.MSG()
    try:
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r == 0 or r == -1:
                break
            if msg.message == _WM_HOTKEY and int(msg.wParam) == _HOTKEY_ID:
                paused = toggle()
                print(
                    f"deskctl: {'paused' if paused else 'resumed'} (Ctrl+Alt+Shift+K)",
                    file=sys.stderr,
                    flush=True,
                )
    finally:
        user32.UnregisterHotKey(None, _HOTKEY_ID)
