"""Screen capture via mss -> PIL Image + capture origin (top-left screen px)."""

import mss
from PIL import Image

from windesktopctl.core import dpi
from windesktopctl.core import WindesktopctlError

_sct: mss.MSS | None = None


def _mss() -> mss.MSS:
    global _sct
    if _sct is None:
        # One process-lifetime instance on the worker thread. We deliberately
        # do NOT close it at exit: GDI DCs are thread-affine and mss's atexit
        # close raises ScreenShotError when run on another thread; the OS
        # reclaims the handle at process exit anyway.
        _sct = mss.MSS()
    return _sct


def grab_rect(x: int, y: int, w: int, h: int) -> tuple[Image.Image, tuple[int, int]]:
    """Grab a screen rectangle (absolute virtual-screen px, may be negative).

    Returns (PIL RGB image, (origin_x, origin_y)). The rect is clamped to the
    virtual screen; raises WindesktopctlError if it does not intersect it at all.
    """
    x, y, w, h = int(x), int(y), int(w), int(h)
    if w <= 0 or h <= 0:
        raise WindesktopctlError(f"invalid capture rect {x},{y},{w},{h}")

    vx, vy, vw, vh = dpi.virtual_screen()
    x1 = max(x, vx)
    y1 = max(y, vy)
    x2 = min(x + w, vx + vw)
    y2 = min(y + h, vy + vh)
    if x2 <= x1 or y2 <= y1:
        raise WindesktopctlError(f"capture rect {x},{y},{w},{h} is outside the screen")

    shot = _mss().grab({"left": x1, "top": y1, "width": x2 - x1, "height": y2 - y1})
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return img, (x1, y1)


def grab_virtual_screen() -> tuple[Image.Image, tuple[int, int]]:
    vx, vy, vw, vh = dpi.virtual_screen()
    return grab_rect(vx, vy, vw, vh)
