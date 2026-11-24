"""Privacy exclusions: never observe/screenshot password-manager windows,
and blackout their rects in screen/region captures before OCR/encoding.

DESKCTL_EXCLUDE (comma-separated process names) ADDS to the built-in
password-manager list - exclusions only ever grow, never shrink.
"""

from __future__ import annotations

import os

from deskctl.core import DeskctlError
from deskctl.core import windows as win

DEFAULT_EXCLUDE = (
    "1password.exe",
    "keepass.exe",
    "keepassxc.exe",
    "bitwarden.exe",
    "enpass.exe",
)


def _norm(name: str) -> str:
    n = (name or "").strip().lower()
    if n.endswith(".exe"):
        n = n[: -len(".exe")]
    return n


def excluded_procs() -> set[str]:
    """Normalized process basenames that must never be observed."""
    out = {_norm(p) for p in DEFAULT_EXCLUDE}
    env = os.environ.get("DESKCTL_EXCLUDE", "")
    for p in env.split(","):
        n = _norm(p)
        if n:
            out.add(n)
    return out


def is_excluded_proc(process: str) -> bool:
    return _norm(process) in excluded_procs()


def is_excluded_hwnd(hwnd: int) -> bool:
    info = win.window_info(int(hwnd))
    return info is not None and is_excluded_proc(info.process)


def check_window_allowed(hwnd: int, title: str = "", proc: str | None = None) -> None:
    """Raise DeskctlError if hwnd belongs to an excluded process."""
    if proc is None:
        info = win.window_info(int(hwnd))
        proc = info.process if info else ""
    if is_excluded_proc(proc):
        raise DeskctlError(
            f'privacy: window "{title}" ({proc}) is excluded; '
            "set DESKCTL_EXCLUDE to change the list"
        )


def blackout_rects(
    cap_rect: tuple[int, int, int, int],
    win_rects: list[tuple[int, int, int, int]],
) -> list[tuple[int, int, int, int]]:
    """Intersect each window rect (x,y,w,h screen px) with the capture rect;
    return non-empty clipped rects in CAPTURE-IMAGE pixel space is NOT done
    here - callers offset by the capture origin. Pure function, unit-tested.
    """
    cx, cy, cw, ch = (int(v) for v in cap_rect)
    out = []
    for wr in win_rects:
        wx, wy, ww, wh = (int(v) for v in wr)
        x1 = max(wx, cx)
        y1 = max(wy, cy)
        x2 = min(wx + ww, cx + cw)
        y2 = min(wy + wh, cy + ch)
        if x2 > x1 and y2 > y1:
            out.append((x1, y1, x2 - x1, y2 - y1))
    return out


def apply_blackouts(img, origin) -> None:
    """Fill excluded-window rects with black on a captured PIL image (in place).
    origin = capture top-left screen px; the image covers origin..origin+size."""
    from PIL import ImageDraw

    ox, oy = int(origin[0]), int(origin[1])
    iw, ih = img.size
    win_rects = [
        w.rect
        for w in win.list_windows()
        if w.visible and not w.iconic and is_excluded_proc(w.process)
    ]
    rects = blackout_rects((ox, oy, iw, ih), win_rects)
    if not rects:
        return
    draw = ImageDraw.Draw(img)
    for rx, ry, rw, rh in rects:
        draw.rectangle([rx - ox, ry - oy, rx - ox + rw, ry - oy + rh], fill=(0, 0, 0))
