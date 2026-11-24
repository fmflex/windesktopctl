"""UI Automation tree walk via the uiautomation package.

Runs on the core worker thread (COM initialized there). Produces raw
records consumed by elements.from_uia_records.
"""

from __future__ import annotations

import time
from collections import deque

from deskctl.core import DeskctlError
from deskctl.core import elements as el

MAX_DEPTH = 25
TIME_BUDGET = 3.0  # seconds, wall clock
MAX_VISITED = 6000

INTERACTIVE = el.INTERACTIVE_TYPES


def _role_name(ctrl) -> str:
    try:
        name = ctrl.ControlTypeName  # e.g. "ButtonControl"
    except Exception:
        return ""
    if name.endswith("Control"):
        name = name[: -len("Control")]
    return name


def _rect_of(ctrl) -> tuple[int, int, int, int] | None:
    try:
        r = ctrl.BoundingRectangle
        x, y = int(r.left), int(r.top)
        w, h = int(r.right - r.left), int(r.bottom - r.top)
        return (x, y, w, h)
    except Exception:
        return None


def _intersects(rect, win_rect) -> bool:
    x, y, w, h = rect
    wx, wy, ww, wh = win_rect
    return x < wx + ww and x + w > wx and y < wy + wh and y + h > wy


def _name_of(ctrl) -> str:
    try:
        return ctrl.Name or ""
    except Exception:
        return ""


def _value_of(ctrl) -> str:
    try:
        pattern = ctrl.GetPattern(10002)  # UIA_ValuePatternId
        if pattern is not None:
            v = pattern.Value
            return v if isinstance(v, str) else str(v)
    except Exception:
        pass
    return ""


def _silence_uiautomation(auto) -> None:
    """Keep uiautomation quiet: no @AutomationLog.txt, no stdout prints
    (critical when stdout is the MCP stdio channel; also keeps CLI output
    clean)."""
    try:
        auto.Logger.SetLogFile("")

        def _noop(*_a, **_k):
            return None

        auto.Logger.Write = staticmethod(_noop)
    except Exception:
        pass


def walk(hwnd: int, window_rect: tuple[int, int, int, int]) -> tuple[list[dict], bool]:
    """Walk the UIA tree of a top-level window.

    Returns (records, truncated). Records are dicts with keys
    role/name/value/rect (rect = x,y,w,h screen px). Skips offscreen
    elements (and their subtrees), empty/off-window rects, and non-kept
    roles. Stops early on the time/node budget and reports truncated=True.
    """
    import uiautomation as auto

    _silence_uiautomation(auto)
    root = auto.ControlFromHandle(int(hwnd))
    if root is None:
        raise DeskctlError(f"UIA: cannot get control for hwnd {hwnd}")

    records: list[dict] = []
    visited = 0
    truncated = False
    deadline = time.monotonic() + TIME_BUDGET

    queue: deque = deque([(root, 0)])
    while queue:
        if (visited & 31) == 0 and time.monotonic() > deadline:
            truncated = True
            break
        if visited >= MAX_VISITED:
            truncated = True
            break

        ctrl, depth = queue.popleft()
        visited += 1

        try:
            offscreen = bool(ctrl.IsOffscreen)
        except Exception:
            offscreen = False

        # Offscreen subtrees are clipped out of view; skipping them keeps the
        # budget for visible content.
        if not offscreen:
            rect = _rect_of(ctrl)
            if rect is not None and rect[2] > 0 and rect[3] > 0 and _intersects(rect, window_rect):
                role = _role_name(ctrl)
                if role in INTERACTIVE or role == "Text":
                    name = _name_of(ctrl)
                    if el.should_keep(role, name):
                        records.append(
                            {
                                "role": role,
                                "name": name,
                                "value": _value_of(ctrl),
                                "rect": rect,
                            }
                        )

            if depth < MAX_DEPTH:
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    children = []
                for child in children or []:
                    queue.append((child, depth + 1))
        # offscreen: do not descend

    return records, truncated
