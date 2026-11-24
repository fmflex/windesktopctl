"""High-level deskctl operations.

Every function here is designed to run ON the core worker thread (submit via
worker.run / worker.submit from the CLI and MCP server).
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import subprocess
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
from deskctl.core import privacy
from deskctl.core import safety
from deskctl.core import state as st
from deskctl.core import uia
from deskctl.core import windows as win

OBSERVE_STALE = st.OBSERVE_MAX_AGE
MARKS_MAX_AGE = st.OBSERVE_MAX_AGE
ACT_SETTLE = 0.25
WAIT_POLL = 0.3
WAIT_MAX_ELEMENTS = 500
LAUNCH_WAIT = 10.0

VALID_MODES = ("auto", "uia", "ocr", "both")
VALID_WAIT = ("changed", "text", "window", "element", "gone")
VALID_WINDOW_ACTIONS = ("minimize", "maximize", "restore", "close", "move", "resize")

# Browser processes eligible for observe(dom=True). Chromium family exposes
# AutomationId=="RootWebArea"; Firefox is handled via its first Document.
_DOM_PROCS = frozenset(
    {
        "chrome.exe",
        "msedge.exe",
        "brave.exe",
        "vivaldi.exe",
        "opera.exe",
        "firefox.exe",
    }
)

_URL_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


# ------------------------------------------------------------- resolution --

def _is_int(s: str) -> bool:
    return re.fullmatch(r"-?\d+", str(s).strip()) is not None


def _resolve_title(spec: str) -> int:
    """Title spec -> hwnd. Exact (case-insensitive) title wins; otherwise
    exactly one substring match is required - several is an error listing
    up to 8 candidates."""
    s = str(spec).strip()
    wins = win.list_windows()
    low = s.lower()
    exact = [w for w in wins if (w.title or "").lower() == low]
    if exact:
        return exact[0].hwnd
    matches = [w for w in wins if low in (w.title or "").lower()]
    if not matches:
        raise DeskctlError(f'no visible window matching "{s}"')
    if len(matches) > 1:
        cand = "; ".join(
            f'hwnd={w.hwnd} "{w.title}" {w.process or "-"}' for w in matches[:8]
        )
        more = f" (+{len(matches) - 8} more)" if len(matches) > 8 else ""
        raise DeskctlError(f'ambiguous window "{s}": {len(matches)} matches - {cand}{more}')
    return matches[0].hwnd


def _resolve_window(spec) -> dict:
    """Resolve a window spec to {hwnd,title,proc,rect,screen,pid}.

    spec: "focused" | hwnd int | title (exact > unique substring) | "screen".
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
        hwnd = _resolve_title(s)

    info = win.window_info(hwnd)
    if info is None:
        raise DeskctlError(f"window hwnd={hwnd} is gone")
    if info.iconic:
        raise DeskctlError(f'window "{info.title}" is minimized; restore it first')
    privacy.check_window_allowed(hwnd, info.title, info.process)
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

    target: "focused" | "screen" | hwnd | title | "x,y,w,h".
    window_key matches what record_observe stored ('screen' or hwnd int) so
    screenshot marks only apply to the same window; 'screen' also marks the
    capture for privacy blackouts.
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
        privacy.check_window_allowed(hwnd, info.title, info.process)
        return info.rect, hwnd

    if _is_int(s):
        hwnd = int(s)
        info = win.window_info(hwnd)
        if info is None:
            raise DeskctlError(f"no such window hwnd={hwnd}")
        if info.iconic:
            raise DeskctlError(f'window "{info.title}" is minimized; restore it first')
        privacy.check_window_allowed(hwnd, info.title, info.process)
        return info.rect, hwnd

    m = re.fullmatch(r"(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)", s)
    if m:
        rect = tuple(int(g) for g in m.groups())
        return rect, "screen"

    # title substring (exact > unique) - same rules as observe/focus
    hwnd = _resolve_title(s)
    info = win.window_info(hwnd)
    if info is None:
        raise DeskctlError(f"window hwnd={hwnd} is gone")
    if info.iconic:
        raise DeskctlError(f'window "{info.title}" is minimized; restore it first')
    privacy.check_window_allowed(hwnd, info.title, info.process)
    return info.rect, hwnd


# ---------------------------------------------------------------- observe --

def _ocr_elements(rect: tuple[int, int, int, int], blackout: bool = False):
    """Capture rect and OCR it. Returns (elements, pil_image)."""
    img, origin = capture.grab_rect(*rect)
    if blackout:
        privacy.apply_blackouts(img, origin)
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


def _uia_elements(hwnd: int, rect, dom: bool = False):
    """Returns (elements, walk_truncated)."""
    records, walk_truncated = uia.walk(hwnd, rect, dom=dom)
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


def _element_json(e: el.Element) -> dict:
    return {
        "id": e.id,
        "role": e.role,
        "name": e.name,
        "value": e.effective_value(),
        "flags": sorted(e.flags),
        "rect": [int(v) for v in e.rect],
        "center": [int(e.cx), int(e.cy)],
    }


def observe_data(window="focused", mode="auto", max_elements=150, dom=False) -> dict:
    """Structured observe result: window info, source, changed, elements."""
    mode = str(mode).lower()
    if mode not in VALID_MODES:
        raise DeskctlError(f'mode must be one of {", ".join(VALID_MODES)}')
    max_elements = int(max_elements)
    if max_elements < 1:
        raise DeskctlError("max_elements must be >= 1")
    dom = bool(dom)

    w = _resolve_window(window)
    rect = w["rect"]
    is_screen = w["screen"]

    if dom and not is_screen:
        proc = (w.get("proc") or "").lower()
        if proc not in _DOM_PROCS:
            raise DeskctlError(f'dom mode: not a supported browser (process "{proc or "?"}")')

    if is_screen:
        if mode == "uia":
            raise DeskctlError('"screen" target supports OCR only (mode auto/ocr/both)')
        elements, img = _ocr_elements(rect, blackout=True)
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
                uia_elems, walk_truncated = _uia_elements(hwnd, rect, dom=dom)
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

    return {
        "window": {
            "title": w["title"],
            "proc": w["proc"],
            "hwnd": w["hwnd"],
            "rect": [int(v) for v in rect],
            "source": source,
            "screen": is_screen,
        },
        "changed": changed,
        "truncated": dropped,
        "walk_truncated": walk_truncated,
        "elements": [_element_json(e) for e in kept],
        "_format": {
            "title": w["title"],
            "proc": w["proc"],
            "hwnd": w["hwnd"],
            "rect": rect,
            "source": source,
            "changed": changed,
            "elements": kept,
            "truncated_count": dropped,
            "walk_truncated": walk_truncated and dropped == 0,
        },
    }


def observe(window="focused", mode="auto", max_elements=150, dom=False) -> str:
    d = observe_data(window, mode, max_elements, dom=dom)
    return el.format_observe(**d["_format"])


def observe_json(window="focused", mode="auto", max_elements=150, dom=False) -> str:
    d = observe_data(window, mode, max_elements, dom=dom)
    d = {k: v for k, v in d.items() if k != "_format"}
    return json.dumps(d, ensure_ascii=False)


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
    if window_key == "screen":
        privacy.apply_blackouts(img, origin)

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


def _do_act(action, target, text, keys, amount, to, horizontal):
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
        safety.check_before_action()  # re-check right before the drag starts
        _input.drag(x1, y1, x2, y2)
        return f"ok: dragged {l1} -> {l2} ({x1},{y1} -> {x2},{y2})", [x2, y2], {"from": [x1, y1]}

    if action == "scroll":
        if amount is None:
            raise DeskctlError("scroll requires amount")
        amt = float(amount)
        if target is not None:
            x, y, label = st.resolve_target(target)
            _input.scroll(amt, x, y, horizontal=horizontal)
            axis = "h" if horizontal else "v"
            return f"ok: scrolled {axis} {amt:g} at {x},{y}", [x, y], {"amount": amt, "horizontal": horizontal}
        _input.scroll(amt, horizontal=horizontal)
        axis = "h" if horizontal else "v"
        return f"ok: scrolled {axis} {amt:g}", None, {"amount": amt, "horizontal": horizontal}

    if action == "type":
        if text is None:
            raise DeskctlError("type requires text")
        s = str(text)
        _input.type_text(s, user_active=safety.is_user_active)
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


def act(
    action,
    target=None,
    text=None,
    keys=None,
    amount=None,
    to=None,
    horizontal=False,
) -> str:
    a = str(action).strip().lower().replace("-", "_")
    safety.check_before_action()

    before = _screen_thumb()
    msg, xy, extra = _do_act(a, target, text, keys, amount, to, bool(horizontal))

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
        "horizontal": bool(horizontal),
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


def _window_line(hwnd: int) -> str:
    """windows()-style line for one hwnd."""
    info = win.window_info(hwnd)
    if info is None:
        return f"hwnd={hwnd} (gone)"
    fg = win.focused_hwnd()
    mark = " [focused]" if info.hwnd == fg else ""
    r = info.rect
    return (
        f'hwnd={info.hwnd} "{info.title}" {info.process or "-"} '
        f"rect={r[0]},{r[1]},{r[2]},{r[3]}{mark}"
    )


def focus(window) -> str:
    s = str(window).strip()
    if _is_int(s):
        hwnd = int(s)
    else:
        hwnd = _resolve_title(s)
    if not win.set_foreground(hwnd):
        raise DeskctlError(f"could not focus hwnd={hwnd}")
    info = win.window_info(hwnd)
    title = info.title if info else "?"
    return f'focused hwnd={hwnd} "{title}"'


def window(action, window="focused", x=None, y=None, width=None, height=None) -> str:
    """minimize | maximize | restore | close | move | resize a window."""
    a = str(action).strip().lower()
    if a not in VALID_WINDOW_ACTIONS:
        raise DeskctlError(
            f'unknown window action "{action}" ({", ".join(VALID_WINDOW_ACTIONS)})'
        )
    w = _resolve_window(window)
    hwnd = w["hwnd"]
    title = w["title"]
    safety.check_before_action()

    past = {"minimize": "minimized", "maximize": "maximized", "restore": "restored",
            "close": "closed", "move": "moved", "resize": "resized"}[a]

    if a in ("minimize", "maximize", "restore"):
        cmd = {"minimize": 6, "maximize": 3, "restore": 9}[a]
        win.show_window(hwnd, cmd)
    elif a == "close":
        win.close_window(hwnd)
    else:
        info = win.window_info(hwnd)
        if info is None:
            raise DeskctlError(f"window hwnd={hwnd} is gone")
        cx, cy, cw, ch = info.rect
        if a == "move" and (x is None or y is None):
            raise DeskctlError("window move requires x and y")
        if a == "resize" and (width is None or height is None):
            raise DeskctlError("window resize requires width and height")
        nx = int(x) if x is not None else cx
        ny = int(y) if y is not None else cy
        nw = int(width) if width is not None else cw
        nh = int(height) if height is not None else ch
        if nw <= 0 or nh <= 0:
            raise DeskctlError("window width/height must be > 0")
        win.set_window_rect(hwnd, nx, ny, nw, nh)

    safety.log_action(
        {"action": f"window_{a}", "hwnd": hwnd, "title": title,
         "x": x, "y": y, "width": width, "height": height}
    )
    return f'ok: {past} hwnd={hwnd} "{title}"'


def _split_command(t: str) -> list[str]:
    """shlex split keeping Windows paths sane (posix=False + unquote)."""
    parts = shlex.split(t, posix=False)
    out = []
    for p in parts:
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1]
        out.append(p)
    return out


def _is_url_or_file(t: str) -> bool:
    if os.path.exists(t):
        return True
    m = _URL_RE.match(t)
    if m and len(m.group(1)) > 1:  # scheme longer than a drive letter
        return True
    return False


def launch(target) -> str:
    """Launch a URL, file, or command; wait for its window and report it."""
    t = str(target).strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        t = t[1:-1].strip()  # a single quoted path/URL
    if not t:
        raise DeskctlError("launch requires a target (URL, file path, or command)")
    safety.check_before_action()

    before = {w.hwnd for w in win.list_windows()}
    pid = None
    if _is_url_or_file(t):
        try:
            os.startfile(t)
        except OSError as ex:
            raise DeskctlError(f'cannot launch "{t}": {ex}')
    else:
        argv = _split_command(t)
        if not argv:
            raise DeskctlError(f'cannot parse launch target "{t}"')
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid = proc.pid
        except OSError as ex:
            raise DeskctlError(f'cannot launch "{t}": {ex}')

    deadline = time.monotonic() + LAUNCH_WAIT
    found = None
    while time.monotonic() < deadline:
        time.sleep(0.25)
        new = [w for w in win.list_windows() if w.hwnd not in before]
        if not new:
            continue
        if pid is not None:
            mine = [w for w in new if w.pid == pid]
            if mine:
                found = mine[0]
                break
        found = new[0]
        break

    if found is None:
        msg = "launched; no new window detected"
        safety.log_action({"action": "launch", "target": t, "pid": pid, "hwnd": None})
        return msg
    line = _window_line(found.hwnd)
    safety.log_action({"action": "launch", "target": t, "pid": pid, "hwnd": found.hwnd})
    return f"launched: {line}"


# ------------------------------------------------------------------ wait_for --

def _wait_region(window) -> tuple[tuple[int, int, int, int], object]:
    """Capture region for wait_for(changed): window spec -> (rect, key)."""
    s = str(window).strip().lower()
    if s == "screen":
        vx, vy, vw, vh = dpi.virtual_screen()
        return (vx, vy, vw, vh), "screen"
    w = _resolve_window(window)
    return w["rect"], ("screen" if w["screen"] else w["hwnd"])


def _wait_timeout(cond_desc: str, timeout: float) -> str:
    return f"timeout after {timeout:g}s: {cond_desc} not met"


def _element_blob(e: el.Element) -> str:
    return f"{e.name} {e.effective_value()}".lower()


def _match_text(value: str, elems: list[el.Element]) -> list[el.Element]:
    needle = str(value).lower()
    return [e for e in elems if needle in _element_blob(e)]


def _parse_element_spec(value: str):
    """'Role:name-sub' -> (role_lower|None, name_sub); plain -> (None, value)."""
    v = str(value)
    if ":" in v:
        role, _, sub = v.partition(":")
        role = role.strip()
        sub = sub.strip()
        if role and sub:
            return role.lower(), sub.lower()
    return None, v.strip().lower()


def _match_element(value: str, elems: list[el.Element]) -> list[el.Element]:
    role, sub = _parse_element_spec(value)
    out = []
    for e in elems:
        if role is not None and e.role.lower() != role:
            continue
        hay = (e.name or e.effective_value() or "").lower()
        if sub and sub in hay:
            out.append(e)
    return out


def _wait_match(cond: str, value, elems: list[el.Element]) -> list[el.Element]:
    if cond == "text":
        return _match_text(value, elems)
    if cond == "element":
        return _match_element(value, elems)
    if cond == "gone":
        v = str(value or "")
        if ":" in v:
            return _match_element(v, elems)
        return _match_text(v, elems)
    return []


def wait_for(cond="changed", value=None, timeout=10.0, window="focused") -> str:
    """Poll until a condition is met. cond: changed | text | window | element |
    gone. Returns a result string; timeout is NOT an error."""
    c = str(cond).strip().lower()
    if c not in VALID_WAIT:
        raise DeskctlError(f'cond must be one of {", ".join(VALID_WAIT)}')
    timeout = float(timeout)
    if timeout <= 0:
        raise DeskctlError("timeout must be > 0")
    desc = c if value is None else f'{c} "{value}"'
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()

    if c == "changed":
        rect, _key = _wait_region(window)
        img, _ = capture.grab_rect(*rect)
        base = diff.thumb(img)
        while True:
            time.sleep(WAIT_POLL)
            try:
                img, _ = capture.grab_rect(*rect)
                t = diff.thumb(img)
            except Exception:
                t = None
            if t is not None and diff.changed(base, t):
                return f"changed after {time.monotonic() - t0:.1f}s"
            if time.monotonic() >= deadline:
                return _wait_timeout(desc, timeout)

    if c == "window":
        needle = str(value or "").lower()
        while True:
            hwnd = win.focused_hwnd()
            info = win.window_info(hwnd) if hwnd else None
            title = info.title if info else ""
            if needle and needle in title.lower():
                return f'foreground "{title}" after {time.monotonic() - t0:.1f}s'
            if time.monotonic() >= deadline:
                return _wait_timeout(desc, timeout)
            time.sleep(WAIT_POLL)

    # text / element / gone - poll observe (UIA, OCR fallback when sparse)
    while True:
        data = observe_data(window, "auto", WAIT_MAX_ELEMENTS)
        elems = [el.Element.from_dict(d) for d in data["elements"]]
        matches = _wait_match(c, value, elems)
        if c == "gone":
            if not matches:
                return f'gone after {time.monotonic() - t0:.1f}s'
        elif matches:
            lines = [el.format_element_line(e) for e in matches]
            lines.append("(element map updated)")
            return "\n".join(lines)
        if time.monotonic() >= deadline:
            return _wait_timeout(desc, timeout)
        time.sleep(WAIT_POLL)


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
        f"state={state}; hotkey=Ctrl+Alt+Shift+K; takeover={safety.takeover_status()}; "
        f"observe={obs_s}; screenshot={shot_s}; "
        f"action_log={st.state_dir() / 'actions.log'}"
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
