"""High-level deskctl operations.

Every function here is designed to run ON the core worker thread (submit via
worker.run / worker.submit from the CLI and MCP server).
"""

from __future__ import annotations

import io
import re
import time
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageFont

from deskctl.core import dpi
from deskctl.core import DeskctlError
from deskctl.core import capture
from deskctl.core import diff
from deskctl.core import elements as el
from deskctl.core import input as _input
from deskctl.core import ocr
from deskctl.core import safety
from deskctl.core import state as st
from deskctl.core import uia
from deskctl.core import windows as win

OBSERVE_STALE = st.OBSERVE_MAX_AGE
MARKS_MAX_AGE = st.OBSERVE_MAX_AGE
ACT_SETTLE = 0.25

VALID_MODES = ("auto", "uia", "ocr", "both")


# ------------------------------------------------------------- resolution --

def _is_int(s: str) -> bool:
    return re.fullmatch(r"-?\d+", str(s).strip()) is not None


def _resolve_window(spec) -> dict:
    """Resolve a window spec to {hwnd,title,proc,rect,screen}.

    spec: "focused" | hwnd int | title substring | "screen".
    """
    s = str(spec).strip()
    low = s.lower()

    if low == "screen":
        vx, vy, vw, vh = dpi.virtual_screen()
        return {
            "screen": True,
            "hwnd": 0,
            "title": "(virtual screen)",
            "proc": "-",
            "rect": (vx, vy, vw, vh),
        }

    if low in ("focused", "focus", "foreground", "active"):
        hwnd = win.focused_hwnd()
        if not hwnd:
            raise DeskctlError("no foreground window")
    elif _is_int(s):
        hwnd = int(s)
        if not win.window_info(hwnd):
            raise DeskctlError(f"no such window hwnd={hwnd}")
    else:
        needle = s.lower()
        matches = [w for w in win.list_windows() if needle in (w.title or "").lower()]
        if not matches:
            raise DeskctlError(f'no visible window matching "{s}"')
        hwnd = matches[0].hwnd

    info = win.window_info(hwnd)
    if info is None:
        raise DeskctlError(f"window hwnd={hwnd} is gone")
    if info.iconic:
        raise DeskctlError(f'window "{info.title}" is minimized; restore it first')
    return {
        "screen": False,
        "hwnd": hwnd,
        "title": info.title,
        "proc": info.process,
        "rect": info.rect,
        "pid": info.pid,
    }


def _resolve_capture(target) -> tuple[tuple[int, int, int, int], object]:
    """Resolve a capture target to (rect, window_key).

    target: "focused" | "screen" | hwnd | "x,y,w,h".
    window_key matches what record_observe stored ('screen' or hwnd int) so
    screenshot marks only apply to the same window.
    """
    s = str(target).strip()
    low = s.lower()

    if low == "screen":
        vx, vy, vw, vh = dpi.virtual_screen()
        return (vx, vy, vw, vh), "screen"

    if low in ("focused", "focus", "foreground", "active"):
        hwnd = win.focused_hwnd()
        if not hwnd:
            raise DeskctlError("no foreground window")
        info = win.window_info(hwnd)
        if info is None:
            raise DeskctlError("foreground window is gone")
        if info.iconic:
            raise DeskctlError('foreground window is minimized; restore it first')
        return info.rect, hwnd

    if _is_int(s):
        hwnd = int(s)
        info = win.window_info(hwnd)
        if info is None:
            raise DeskctlError(f"no such window hwnd={hwnd}")
        if info.iconic:
            raise DeskctlError(f'window "{info.title}" is minimized; restore it first')
        return info.rect, hwnd

    m = re.fullmatch(r"(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)", s)
    if m:
        rect = tuple(int(g) for g in m.groups())
        return rect, "screen"

    raise DeskctlError(
        f'unrecognized capture target "{target}" '
        '(use "focused", "screen", an hwnd, or "x,y,w,h")'
    )


# ---------------------------------------------------------------- observe --

def _ocr_elements(rect: tuple[int, int, int, int]):
    """Capture rect and OCR it. Returns (elements, pil_image)."""
    img, origin = capture.grab_rect(*rect)
    lines = ocr.recognize(img)
    ox, oy = origin
    elems = []
    for ln in lines:
        lx, ly, lw, lh = ln["rect"]
        elems.append(
            el.Element(
                role="text",
                name=el.clean_text(ln["text"]),
                value="",
                rect=(ox + lx, oy + ly, lw, lh),
                source="ocr",
            )
        )
    return elems, img


def _uia_elements(hwnd: int, rect):
    """Returns (elements, walk_truncated)."""
    records, walk_truncated = uia.walk(hwnd, rect)
    return el.from_uia_records(records), walk_truncated


def _changed_and_store_baseline(img: Image.Image) -> bool:
    t = diff.thumb(img)
    prev = st.get_baseline()
    if prev is None:
        changed = True
        prev_img = None
    else:
        prev_img = diff.from_bytes(prev)
        changed = diff.changed(t, prev_img)
    st.set_baseline(diff.to_bytes(t))
    return changed


def observe(window="focused", mode="auto", max_elements=150) -> str:
    mode = str(mode).lower()
    if mode not in VALID_MODES:
        raise DeskctlError(f'mode must be one of {", ".join(VALID_MODES)}')
    max_elements = int(max_elements)
    if max_elements < 1:
        raise DeskctlError("max_elements must be >= 1")

    w = _resolve_window(window)
    rect = w["rect"]
    is_screen = w["screen"]

    if is_screen:
        if mode == "uia":
            raise DeskctlError('"screen" target supports OCR only (mode auto/ocr/both)')
        elements, img = _ocr_elements(rect)
        elements = el.dedup(elements)
        source = "ocr"
        walk_truncated = False
    else:
        hwnd = w["hwnd"]
        uia_elems: list[el.Element] = []
        walk_truncated = False
        uia_error: Exception | None = None

        want_uia = mode in ("auto", "uia", "both")
        if want_uia:
            try:
                uia_elems, walk_truncated = _uia_elements(hwnd, rect)
            except DeskctlError as ex:
                uia_error = ex
                if mode == "uia":
                    raise

        if mode == "uia":
            elements, source = uia_elems, "uia"
            img = None
        elif mode == "ocr":
            elements, img = _ocr_elements(rect)
            elements = el.dedup(elements)
            source = "ocr"
        elif mode == "both":
            try:
                ocr_elems, img = _ocr_elements(rect)
                elements = el.dedup(uia_elems + ocr_elems)
                source = "both" if uia_elems else "ocr"
            except DeskctlError:
                elements, source, img = uia_elems, "uia", None
        else:  # auto
            if uia_error is None and len(uia_elems) >= 5:
                elements, source, img = uia_elems, "uia", None
            else:
                try:
                    ocr_elems, oimg = _ocr_elements(rect)
                except DeskctlError:
                    ocr_elems, oimg = [], None
                if ocr_elems:
                    elements, source, img = ocr_elems, "ocr", oimg
                else:
                    elements, source, img = uia_elems, "uia", None
                    if uia_error is not None and not elements:
                        raise uia_error

    kept, dropped = el.cap(elements, max_elements)

    if img is None:
        try:
            img, _ = capture.grab_rect(*rect)
        except DeskctlError:
            img = None
    changed = _changed_and_store_baseline(img) if img is not None else True

    st.record_observe(
        hwnd=w["hwnd"],
        window_key="screen" if is_screen else w["hwnd"],
        title=w["title"],
        proc=w["proc"],
        rect=rect,
        source=source,
        elements=kept,
        truncated_count=dropped,
        walk_truncated=walk_truncated,
    )

    return el.format_observe(
        title=w["title"],
        proc=w["proc"],
        hwnd=w["hwnd"],
        rect=rect,
        source=source,
        changed=changed,
        elements=kept,
        truncated_count=dropped,
        walk_truncated=walk_truncated and dropped == 0,
    )


# ------------------------------------------------------------ screenshot --

def _marks_image(
    img: Image.Image,
    origin: tuple[int, int],
    scale: float,
    cap_rect: tuple[int, int, int, int],
    window_key,
) -> Image.Image:
    obs = st.get_observe()
    if not obs or time.time() - float(obs.get("ts", 0)) > MARKS_MAX_AGE:
        return img
    if obs.get("window") != window_key:
        return img

    ox, oy = origin
    cw, ch = cap_rect[2], cap_rect[3]
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for eid, d in (obs.get("elements") or {}).items():
        try:
            ex, ey, ew, eh = [int(v) for v in d.get("rect", [0, 0, 0, 0])]
        except Exception:
            continue
        if ew <= 0 or eh <= 0:
            continue
        # element must intersect the captured rect
        if not (ex < ox + cw and ex + ew > ox and ey < oy + ch and ey + eh > oy):
            continue
        ix = (ex - ox) / scale
        iy = (ey - oy) / scale
        iw = ew / scale
        ih = eh / scale
        if iw <= 0 or ih <= 0:
            continue
        color = (255, 60, 60)
        draw.rectangle([ix, iy, ix + iw, iy + ih], outline=color, width=2)
        label = str(eid)
        lx, ly = ix, iy - 9
        if ly < 0:
            ly = iy + 2
        try:
            if font is not None:
                tb = draw.textbbox((lx, ly), label, font=font)
                draw.rectangle(
                    [tb[0] - 1, tb[1] - 1, tb[2] + 1, tb[3] + 1], fill=(255, 255, 255)
                )
                draw.text((lx, ly), label, fill=(0, 0, 0), font=font)
            else:
                draw.text((lx, ly), label, fill=color)
        except Exception:
            pass
    return img


def screenshot(target="focused", max_width=1280, marks=True, fmt="png"):
    """Capture -> (image_bytes, info_str)."""
    fmt = str(fmt).lower()
    if fmt not in ("png", "jpeg"):
        raise DeskctlError('fmt must be "png" or "jpeg"')
    max_width = int(max_width)
    if max_width < 16:
        raise DeskctlError("max_width too small")

    rect, window_key = _resolve_capture(target)
    img, origin = capture.grab_rect(*rect)

    w0, h0 = img.size
    if w0 > max_width:
        nw = max_width
        nh = max(1, int(round(h0 * nw / w0)))
        img = img.resize((nw, nh), Image.LANCZOS)
    scale = w0 / img.width  # screen px per image px

    if marks:
        img = _marks_image(img, origin, scale, rect, window_key)

    st.record_screenshot(
        origin_x=origin[0],
        origin_y=origin[1],
        scale=scale,
        width=img.width,
        height=img.height,
        window_key=window_key,
    )

    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, "JPEG", quality=90)
    else:
        img.save(buf, "PNG")

    scale_s = f"{scale:.4g}"
    info = (
        f"image {img.width}x{img.height} scale={scale_s} "
        f"origin={origin[0]},{origin[1]} ~tokens={img.width * img.height // 750} "
        '(use "i:x,y" targets to click image coords)'
    )
    return buf.getvalue(), info


# -------------------------------------------------------------------- act --

def _screen_thumb():
    try:
        img, _ = capture.grab_virtual_screen()
        return diff.thumb(img)
    except Exception:
        return None


def _do_act(action, target, text, keys, amount, to):
    """Perform the action; returns (message, xy, extra_log_fields)."""
    if action in ("click", "double_click", "right_click", "move"):
        if target is None:
            raise DeskctlError(f"{action} requires a target")
        x, y, label = st.resolve_target(target)
        if action == "click":
            _input.click(x, y, "left")
        elif action == "double_click":
            _input.click(x, y, "left", double=True)
        elif action == "right_click":
            _input.click(x, y, "right")
        else:
            _input.move_to(x, y)
        verb = {
            "click": "clicked",
            "double_click": "double-clicked",
            "right_click": "right-clicked",
            "move": "moved",
        }[action]
        return f"ok: {verb} {label} at {x},{y}", [x, y], {}

    if action == "drag":
        if target is None or to is None:
            raise DeskctlError("drag requires target and to")
        x1, y1, l1 = st.resolve_target(target)
        x2, y2, l2 = st.resolve_target(to)
        _input.drag(x1, y1, x2, y2)
        return f"ok: dragged {l1} -> {l2} ({x1},{y1} -> {x2},{y2})", [x2, y2], {"from": [x1, y1]}

    if action == "scroll":
        if amount is None:
            raise DeskctlError("scroll requires amount")
        amt = float(amount)
        if target is not None:
            x, y, label = st.resolve_target(target)
            _input.scroll(amt, x, y)
            return f"ok: scrolled {amt:g} at {x},{y}", [x, y], {"amount": amt}
        _input.scroll(amt)
        return f"ok: scrolled {amt:g}", None, {"amount": amt}

    if action == "type":
        if text is None:
            raise DeskctlError("type requires text")
        s = str(text)
        _input.type_text(s)
        return f"ok: typed {len(s)} chars", None, {"text_len": len(s)}

    if action == "key":
        if not keys:
            raise DeskctlError("key requires a combo like 'ctrl+s'")
        _input.press_combo(str(keys))
        return f"ok: pressed {keys}", None, {"keys": str(keys)}

    raise DeskctlError(
        f'unknown action "{action}" '
        "(click, double_click, right_click, move, drag, scroll, type, key)"
    )


def act(action, target=None, text=None, keys=None, amount=None, to=None) -> str:
    a = str(action).strip().lower().replace("-", "_")
    safety.check_before_action()

    before = _screen_thumb()
    msg, xy, extra = _do_act(a, target, text, keys, amount, to)

    time.sleep(ACT_SETTLE)
    after = _screen_thumb()
    if before is None or after is None:
        changed = True
    else:
        changed = diff.changed(before, after)

    rec = {
        "action": a,
        "target": target,
        "xy": xy,
        "to": to,
        **extra,
    }
    safety.log_action(rec)
    return f"{msg}; screen changed={str(changed).lower()}"


# ---------------------------------------------------------------- windows --

def windows() -> str:
    fg = win.focused_hwnd()
    lines = []
    for w in win.list_windows():
        mark = " [focused]" if w.hwnd == fg else ""
        r = w.rect
        proc = w.process or "-"
        lines.append(
            f'hwnd={w.hwnd} "{w.title}" {proc} rect={r[0]},{r[1]},{r[2]},{r[3]}{mark}'
        )
    return "\n".join(lines) if lines else "(no windows)"


def focus(window) -> str:
    s = str(window).strip()
    if _is_int(s):
        hwnd = int(s)
    else:
        needle = s.lower()
        matches = [w for w in win.list_windows() if needle in (w.title or "").lower()]
        if not matches:
            raise DeskctlError(f'no visible window matching "{s}"')
        hwnd = matches[0].hwnd
    if not win.set_foreground(hwnd):
        raise DeskctlError(f"could not focus hwnd={hwnd}")
    info = win.window_info(hwnd)
    title = info.title if info else "?"
    return f'focused hwnd={hwnd} "{title}"'


def wait_for_change(timeout=10.0, target="focused") -> str:
    timeout = float(timeout)
    if timeout <= 0:
        raise DeskctlError("timeout must be > 0")
    rect, _key = _resolve_capture(target)
    img, _ = capture.grab_rect(*rect)
    base = diff.thumb(img)
    t0 = time.monotonic()
    deadline = t0 + timeout
    while True:
        time.sleep(0.2)
        try:
            img, _ = capture.grab_rect(*rect)
            t = diff.thumb(img)
        except Exception:
            t = None
        if t is not None and diff.changed(base, t):
            return f"changed after {time.monotonic() - t0:.1f}s"
        if time.monotonic() >= deadline:
            return f"no change in {timeout:g}s"


# ----------------------------------------------------------------- safety --

def pause() -> str:
    safety.pause()
    return "paused (Ctrl+Alt+Shift+K resumes; `deskctl resume` also works)"


def resume() -> str:
    safety.resume()
    return "resumed"


def status() -> str:
    state = "paused" if safety.is_paused() else "running"
    obs_age = st.observe_age()
    shot_age = st.screenshot_age()
    obs_s = f"{obs_age:.0f}s ago" if obs_age is not None else "none"
    shot_s = f"{shot_age:.0f}s ago" if shot_age is not None else "none"
    return (
        f"state={state}; hotkey=Ctrl+Alt+Shift+K; "
        f"observe={obs_s}; screenshot={shot_s}; "
        f"action_log={st.state_dir() / 'actions.log'}"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
