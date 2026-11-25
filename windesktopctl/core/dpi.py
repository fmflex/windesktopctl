"""Per-monitor DPI awareness. Runs once at windesktopctl.core import, before any
capture or UIA work, so all coordinates are physical virtual-screen pixels."""

import ctypes

# GetSystemMetrics indexes
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4  # (HANDLE)-4
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def ensure_dpi_awareness() -> bool:
    """Make this process per-monitor DPI aware. Idempotent-ish; returns True
    if awareness was (or already is) enabled."""
    try:
        # SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            return True
    except Exception:
        pass
    try:
        # Fallback: SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        # returns S_OK (0) on success; fails if awareness already set.
        return ctypes.windll.shcore.SetProcessDpiAwareness(
            _PROCESS_PER_MONITOR_DPI_AWARE
        ) == 0
    except Exception:
        return False


def virtual_screen() -> tuple[int, int, int, int]:
    """Return (x, y, width, height) of the whole virtual screen in physical
    pixels. x/y may be negative with monitors left of/above the primary."""
    u = ctypes.windll.user32
    x = u.GetSystemMetrics(_SM_XVIRTUALSCREEN)
    y = u.GetSystemMetrics(_SM_YVIRTUALSCREEN)
    w = u.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
    h = u.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
    return int(x), int(y), int(w), int(h)
