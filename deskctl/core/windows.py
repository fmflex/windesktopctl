"""Top-level window enumeration and focus via ctypes (no pywin32)."""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes as wt

from deskctl.core import DeskctlError

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- prototypes (handles are 64-bit; restypes must be set or they truncate) --
user32.GetForegroundWindow.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.IsWindow.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetForegroundWindow.restype = wt.BOOL
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.AttachThreadInput.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.BringWindowToTop.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.restype = wt.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9


class WindowInfo:
    __slots__ = ("hwnd", "title", "pid", "process", "rect", "iconic", "visible")

    def __init__(self, hwnd, title, pid, process, rect, iconic, visible):
        self.hwnd = int(hwnd)
        self.title = title
        self.pid = int(pid)
        self.process = process
        self.rect = rect  # (x, y, w, h)
        self.iconic = bool(iconic)
        self.visible = bool(visible)


def _rect_of(hwnd) -> tuple[int, int, int, int]:
    rc = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return (0, 0, 0, 0)
    return (
        int(rc.left),
        int(rc.top),
        int(rc.right - rc.left),
        int(rc.bottom - rc.top),
    )


def process_name(pid: int) -> str:
    """Executable basename (e.g. 'notepad.exe') for a pid, '' on failure."""
    if not pid:
        return ""
    h = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)
    return ""


def _window_info(hwnd) -> WindowInfo | None:
    try:
        hwnd_i = int(hwnd)
    except Exception:
        return None
    if not hwnd_i or not user32.IsWindow(hwnd_i):
        return None
    n = user32.GetWindowTextLengthW(hwnd_i)
    title = ""
    if n > 0:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd_i, buf, n + 1)
        title = buf.value
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd_i, ctypes.byref(pid))
    return WindowInfo(
        hwnd=hwnd_i,
        title=title,
        pid=pid.value,
        process=process_name(pid.value),
        rect=_rect_of(hwnd_i),
        iconic=bool(user32.IsIconic(hwnd_i)),
        visible=bool(user32.IsWindowVisible(hwnd_i)),
    )


def window_info(hwnd: int) -> WindowInfo | None:
    return _window_info(hwnd)


def focused_hwnd() -> int:
    h = user32.GetForegroundWindow()
    return int(h) if h else 0


def list_windows() -> list[WindowInfo]:
    """Visible top-level windows with a non-empty title, in z-order."""
    out: list[WindowInfo] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            info = _window_info(hwnd)
            if info is not None:
                out.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return out


def set_foreground(hwnd: int) -> bool:
    """Bring a window to the foreground. Restores it if minimized.
    Uses the AttachThreadInput + ALT-key trick for the foreground lock."""
    hwnd = int(hwnd)
    if not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
        time.sleep(0.15)

    cur = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    for tid in {int(t) for t in (fg_tid, tgt_tid) if t and int(t) != int(cur)}:
        if user32.AttachThreadInput(cur, tid, True):
            attached.append(tid)

    try:
        # ALT tap satisfies the foreground-lock heuristic on modern Windows.
        try:
            from deskctl.core import input as _input

            _input.press_combo("alt")
        except Exception:
            pass
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur, tid, False)

    time.sleep(0.15)
    return int(user32.GetForegroundWindow() or 0) == hwnd
