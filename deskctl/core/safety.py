"""Safety: pause flag, JSONL action log, failsafe corner, hotkey guard,
and physical-input takeover detection.

Takeover: WH_MOUSE_LL + WH_KEYBOARD_LL hooks live on the same daemon thread
as the Ctrl+Alt+Shift+K hotkey (one message loop). Injected events
(LLMHF_INJECTED / LLKHF_INJECTED - i.e. our own SendInput and tools like
Sunshine) are ignored; any physical key/mouse-button press or >=40px of
cumulative mouse movement marks the user active until
now + DESKCTL_IDLE_SECONDS (default 3). A flusher thread publishes the
active-until timestamp to %LOCALAPPDATA%\\deskctl\\user_active (<=200ms
throttle) so other processes (CLI vs MCP server) see it too, plus a ~1s
heartbeat file used by status() to report whether detection is live.
"""

from __future__ import annotations

import ctypes
import json
import os
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
kernel32 = ctypes.windll.kernel32


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


# ------------------------------------------------------- user takeover --

_USER_ACTIVE_FILE = "user_active"
_HEARTBEAT_FILE = "guard_heartbeat"
_MOUSE_MOVE_THRESHOLD = 40.0  # cumulative px before motion counts as takeover
_FLUSH_INTERVAL = 0.2  # max user_active file write frequency
_HEARTBEAT_INTERVAL = 1.0
_HEARTBEAT_LIVE_AGE = 3.0

_user_active_until = 0.0  # epoch; set by hook callbacks in this process
_last_user_event = 0.0
_mouse_accum = 0.0
_last_mouse_pos: tuple[int, int] | None = None


def idle_seconds() -> float:
    try:
        v = float(os.environ.get("DESKCTL_IDLE_SECONDS", "3"))
    except Exception:
        v = 3.0
    return v if v > 0 else 3.0


def _mark_user_active(now: float | None = None) -> None:
    global _user_active_until, _last_user_event
    now = time.time() if now is None else now
    _last_user_event = now
    _user_active_until = now + idle_seconds()


def _read_ts_file(name: str) -> float | None:
    try:
        return float((st.state_dir() / name).read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _file_active_until() -> float:
    return _read_ts_file(_USER_ACTIVE_FILE) or 0.0


def is_user_active() -> bool:
    """True while physical input is fresher than DESKCTL_IDLE_SECONDS -
    in-process hook state OR the shared user_active timestamp file."""
    now = time.time()
    return now < max(_user_active_until, _file_active_until())


def _active_ago() -> float:
    until = max(_user_active_until, _file_active_until())
    return max(0.0, idle_seconds() - (until - time.time()))


def takeover_live() -> bool:
    """True if a guard heartbeat was written within the last ~3s."""
    ts = _read_ts_file(_HEARTBEAT_FILE)
    return ts is not None and (time.time() - ts) < _HEARTBEAT_LIVE_AGE


def takeover_status() -> str:
    if takeover_live():
        return "live"
    return "off (run the MCP server or `deskctl guard`)"


def check_user_takeover() -> None:
    if is_user_active():
        raise DeskctlError(
            f"user is using the PC (physical input {_active_ago():.1f}s ago); retry when idle"
        )


def check_before_action() -> None:
    """Gate every act(): refuse while paused or while the user is physically
    active; slamming the cursor into the top-left corner failsafes."""
    if is_paused():
        raise DeskctlError("paused - Ctrl+Alt+Shift+K toggles pause; `deskctl resume` clears it")
    check_user_takeover()
    x, y = _input.cursor_pos()
    if is_failsafe_position(x, y):
        pause()
        raise DeskctlError(
            f"failsafe triggered: cursor at screen corner ({x},{y}); automation paused"
        )


# ------------------------------------------------------- low-level hooks --

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
LLMHF_INJECTED = 0x0001
LLKHF_INJECTED = 0x0010
WM_MOUSEMOVE = 0x0200
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_mouse_proc = None
_key_proc = None
_mouse_hook = None
_key_hook = None


def _note_mouse(wparam: int, x: int, y: int) -> None:
    """Hook callback body: memory only, never blocks or does I/O."""
    global _mouse_accum, _last_mouse_pos
    now = time.time()
    if wparam == WM_MOUSEMOVE:
        if _last_mouse_pos is not None:
            _mouse_accum += abs(x - _last_mouse_pos[0]) + abs(y - _last_mouse_pos[1])
        _last_mouse_pos = (x, y)
        if _mouse_accum >= _MOUSE_MOVE_THRESHOLD:
            _mouse_accum = 0.0
            _mark_user_active(now)
    else:
        # any button press/release or wheel event
        _last_mouse_pos = (x, y)
        _mouse_accum = 0.0
        _mark_user_active(now)


def _mouse_hook_proc(nCode, wParam, lParam):
    global _mouse_hook
    try:
        if nCode >= 0:
            data = ctypes.cast(lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            if not (data.flags & LLMHF_INJECTED):
                _note_mouse(int(wParam), int(data.pt.x), int(data.pt.y))
    except Exception:
        pass
    return user32.CallNextHookEx(_mouse_hook, nCode, wParam, lParam)


def _key_hook_proc(nCode, wParam, lParam):
    global _key_hook
    try:
        if nCode >= 0 and int(wParam) in (WM_KEYDOWN, WM_SYSKEYDOWN):
            data = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            if not (data.flags & LLKHF_INJECTED):
                _mark_user_active()
    except Exception:
        pass
    return user32.CallNextHookEx(_key_hook, nCode, wParam, lParam)


def _install_hooks() -> None:
    """Install WH_MOUSE_LL + WH_KEYBOARD_LL on the calling (message-loop) thread."""
    global _mouse_proc, _key_proc, _mouse_hook, _key_hook
    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
    user32.SetWindowsHookExW.restype = wt.HANDLE
    user32.CallNextHookEx.argtypes = [wt.HANDLE, ctypes.c_int, wt.WPARAM, wt.LPARAM]
    user32.CallNextHookEx.restype = wt.LPARAM
    user32.UnhookWindowsHookEx.argtypes = [wt.HANDLE]

    hmod = kernel32.GetModuleHandleW(None)
    _mouse_proc = HOOKPROC(_mouse_hook_proc)
    _key_proc = HOOKPROC(_key_hook_proc)
    _mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _mouse_proc, hmod, 0)
    _key_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _key_proc, hmod, 0)
    if not _mouse_hook or not _key_hook:
        print(
            f"deskctl: low-level hook install failed (mouse={_mouse_hook} key={_key_hook})",
            file=sys.stderr,
            flush=True,
        )


def _unhook() -> None:
    global _mouse_hook, _key_hook
    for h in (_mouse_hook, _key_hook):
        if h:
            try:
                user32.UnhookWindowsHookEx(h)
            except Exception:
                pass
    _mouse_hook = _key_hook = None


# ------------------------------------------------------------- hotkey guard --

_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 1
_VK_K = 0x4B

_guard_lock = threading.Lock()
_guard_started = False
_flusher_started = False


def _flusher_loop() -> None:
    """Publish user_active (<=200ms) + guard heartbeat (~1s) timestamp files."""
    last_write = 0.0
    last_hb = 0.0
    active_file = st.state_dir() / _USER_ACTIVE_FILE
    hb_file = st.state_dir() / _HEARTBEAT_FILE
    while True:
        try:
            now = time.time()
            if now - last_hb >= _HEARTBEAT_INTERVAL:
                last_hb = now
                try:
                    hb_file.write_text(repr(now), encoding="utf-8")
                except Exception:
                    pass
            until = _user_active_until
            active = now < until
            if active:
                if now - last_write >= _FLUSH_INTERVAL:
                    last_write = now
                    try:
                        active_file.write_text(repr(until), encoding="utf-8")
                    except Exception:
                        pass
            elif active_file.exists():
                try:
                    active_file.unlink()
                except Exception:
                    pass
                last_write = now
        except Exception:
            pass
        time.sleep(0.05)


def _ensure_flusher() -> None:
    global _flusher_started
    with _guard_lock:
        if _flusher_started:
            return
        _flusher_started = True
    t = threading.Thread(target=_flusher_loop, name="deskctl-flusher", daemon=True)
    t.start()


def start_hotkey_guard() -> bool:
    """Start the guard: Ctrl+Alt+Shift+K hotkey + LL input hooks on one
    message-loop thread, plus the user_active/heartbeat flusher. Idempotent;
    returns True if the guard thread was started."""
    global _guard_started
    with _guard_lock:
        if _guard_started:
            _ensure_flusher()
            return False
        _guard_started = True
    _ensure_flusher()
    t = threading.Thread(target=_guard_loop, name="deskctl-guard", daemon=True)
    t.start()
    return True


def _guard_loop() -> None:
    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
    user32.RegisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
    user32.GetMessageW.restype = ctypes.c_int

    _install_hooks()
    hotkey_ok = user32.RegisterHotKey(
        None, _HOTKEY_ID, _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, _VK_K
    )
    if not hotkey_ok:
        print("deskctl: failed to register Ctrl+Alt+Shift+K hotkey", file=sys.stderr, flush=True)
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
        if hotkey_ok:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
        _unhook()
