"""Human-verification challenge ("CAPTCHA") relay - detection + grid overlay.

deskctl NEVER solves or guesses challenges: no OCR-to-answer, no automatic
checkbox clicking, no evasion. It only (a) detects that a challenge-looking
page is on screen, (b) captures it with a labeled grid so the user can name
cells, and (c) types/clicks EXACTLY the answer the user gave.

Everything here is pure logic (no desktop access) so it unit-tests cleanly;
api.py wires it to UIA/capture/input.
"""

from __future__ import annotations

import re
import secrets

# Case-insensitive substring needles. Matching is on window title OR on
# element name+value. Deliberately phrase-level so a normal article that
# merely contains the word "robot" does NOT trip detection.
TITLE_NEEDLES = (
    "just a moment",
    "attention required",
    "verify you are human",
    "security check",
    "are you a robot",
)

NAME_NEEDLES = (
    "verify you are human",
    "i'm not a robot",
    "recaptcha",
    "hcaptcha",
    "turnstile",
    "cloudflare security challenge",
    "select all images",
    "select all squares",
    "click each image",
    "press and hold",
    "type the characters",
    "enter the characters",
    "type the text",
)

GRID_NEEDLES = ("select all", "click each image")
TEXT_NEEDLES = ("type the characters", "enter the characters", "type the text")
PRESS_NEEDLES = ("press and hold",)

# Union-of-matched rects smaller than this (either dimension) is considered
# "tiny" - the challenge widget is probably bigger than its text, so fall
# back to the web content area.
MIN_RECT_W = 300
MIN_RECT_H = 150
GRID_PAD = 40  # px of padding around matched elements
DEFAULT_CELL_PX = 70  # target cell size in screen px
MAX_DIM = 1024  # longest side of the saved challenge image
MAX_ROWS = 40
MAX_COLS = 40

_SUBMIT_WORDS = ("verify", "next", "submit", "skip", "continue")


def _attr(e, name, default=""):
    """Read an attribute off an Element object or a plain dict."""
    if isinstance(e, dict):
        return e.get(name, default)
    return getattr(e, name, default)


def _text_of(e) -> str:
    return f"{_attr(e, 'name') or ''} {_attr(e, 'value') or ''}".lower()


def _has(blob: str, needles) -> bool:
    return any(n in blob for n in needles)


def _rect_of(e):
    r = _attr(e, "rect", None)
    if not r or len(r) < 4:
        return None
    try:
        x, y, w, h = (int(v) for v in r[:4])
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _center_in_rect(e, rect) -> bool:
    r = _rect_of(e)
    if r is None or rect is None:
        return False
    cx = r[0] + r[2] // 2
    cy = r[1] + r[3] // 2
    x, y, w, h = rect
    return x <= cx < x + w and y <= cy < y + h


def detect_challenge(title, elements, window_rect=None):
    """Detect a human-verification challenge on a browser window.

    ``title`` is the window title; ``elements`` is a list of Element-like
    objects (or dicts) with role/name/value/rect; ``window_rect`` is the
    window rect (x,y,w,h) used for clamping and as the fallback "web
    content area".

    Returns None, or {"kind", "rect", "matched", "fallback"} where kind is
    one of grid|text|checkbox|press_hold|unknown, rect is (x,y,w,h) screen
    px, matched is a list of {"role","name"} dicts, and fallback marks that
    the content-area rect was used instead of the matched union.
    """
    elems = list(elements or [])
    title_l = (title or "").lower()
    title_hit = _has(title_l, TITLE_NEEDLES)

    matched = []
    for e in elems:
        if _has(_text_of(e), NAME_NEEDLES):
            matched.append(e)

    if not title_hit and not matched:
        return None

    # Union of matched element rects + padding, clamped to the window.
    rects = [r for r in (_rect_of(e) for e in matched) if r is not None]
    fallback = False
    rect = None
    if rects:
        x0 = min(r[0] for r in rects) - GRID_PAD
        y0 = min(r[1] for r in rects) - GRID_PAD
        x1 = max(r[0] + r[2] for r in rects) + GRID_PAD
        y1 = max(r[1] + r[3] for r in rects) + GRID_PAD
        if window_rect is not None:
            wx, wy, ww, wh = (int(v) for v in window_rect[:4])
            x0, y0 = max(x0, wx), max(y0, wy)
            x1, y1 = min(x1, wx + ww), min(y1, wy + wh)
        if x1 > x0 and y1 > y0:
            rect = (x0, y0, x1 - x0, y1 - y0)
    if rect is None or rect[2] < MIN_RECT_W or rect[3] < MIN_RECT_H:
        # Too small or unknown - the widget is bigger than its text.
        if window_rect is not None:
            rect = tuple(int(v) for v in window_rect[:4])
        elif rects:
            rect = (
                min(r[0] for r in rects),
                min(r[1] for r in rects),
                max(r[0] + r[2] for r in rects) - min(r[0] for r in rects),
                max(r[1] + r[3] for r in rects) - min(r[1] for r in rects),
            )
        else:
            return None
        fallback = True

    blob = title_l + " " + " ".join(_text_of(e) for e in matched)

    if _has(blob, GRID_NEEDLES):
        kind = "grid"
    elif _has(blob, TEXT_NEEDLES) or any(
        _attr(e, "role") == "Edit" and _center_in_rect(e, rect) for e in elems
    ):
        kind = "text"
    elif _has(blob, PRESS_NEEDLES):
        kind = "press_hold"
    elif "turnstile" in blob or (
        _has(blob, ("i'm not a robot", "verify you are human"))
        and any(
            _attr(e, "role") == "CheckBox"
            and (_center_in_rect(e, rect) or e in matched)
            for e in elems
        )
    ):
        kind = "checkbox"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "rect": rect,
        "matched": [
            {"role": _attr(e, "role"), "name": _attr(e, "name")} for e in matched[:12]
        ],
        "fallback": fallback,
    }


def is_submit_name(name: str) -> bool:
    n = (name or "").lower()
    return any(w in n for w in _SUBMIT_WORDS)


# ------------------------------------------------------------- grid overlay --

def _font(size: int = 13):
    from PIL import ImageFont

    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()


def overlay_grid(img, rect, origin, rows: int, cols: int):
    """Draw a labeled cell grid over a captured challenge image.

    ``img`` is the capture of ``rect`` (x,y,w,h screen px); ``origin`` is
    the screen px of the image's top-left corner (normally == rect[:2]).
    Cells partition ``rect`` evenly: rows lettered A.., columns numbered 1...
    Thin semi-transparent lines + small labels; a faint "B3" id sits in each
    cell so the user can name any spot. Returns an RGB image (same size).
    """
    from PIL import Image, ImageDraw

    rx, ry, rw, rh = (int(v) for v in rect[:4])
    ox, oy = int(origin[0]), int(origin[1])
    rows, cols = max(1, int(rows)), max(1, int(cols))

    sx = img.width / rw if rw else 1.0
    sy = img.height / rh if rh else 1.0

    rgba = img.convert("RGBA")
    overlay = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    line = (255, 40, 40, 110)

    def ix(v):
        return (v - ox) * sx

    def iy(v):
        return (v - oy) * sy

    for c in range(cols + 1):
        x = ix(rx + c * rw / cols)
        d.line([(x, iy(ry)), (x, iy(ry + rh))], fill=line, width=1)
    for r in range(rows + 1):
        y = iy(ry + r * rh / rows)
        d.line([(ix(rx), y), (ix(rx + rw), y)], fill=line, width=1)
    rgba.alpha_composite(overlay)

    d2 = ImageDraw.Draw(rgba)
    font = _font(13)
    small = _font(10)
    # Column numbers along the top, row letters along the left.
    for c in range(cols):
        x = ix(rx + c * rw / cols) + 2
        d2.text((x, iy(ry) + 1), str(c + 1), fill=(255, 30, 30, 255), font=font)
    for r in range(rows):
        y = iy(ry + r * rh / rows) + 1
        label = chr(ord("A") + r) if r < 26 else str(r + 1)
        d2.text((ix(rx) + 2, y), label, fill=(255, 30, 30, 255), font=font)
    # Faint per-cell id so any spot can be named.
    for r in range(rows):
        rl = chr(ord("A") + r) if r < 26 else str(r + 1)
        for c in range(cols):
            cx = ix(rx + (c + 0.5) * rw / cols)
            cy = iy(ry + (r + 0.5) * rh / rows)
            d2.text(
                (cx - 7, cy - 5),
                f"{rl}{c + 1}",
                fill=(255, 120, 120, 110),
                font=small,
            )
    return rgba.convert("RGB")


def cell_label(row: int, col: int) -> str:
    rl = chr(ord("A") + row) if 0 <= row < 26 else str(row + 1)
    return f"{rl}{col + 1}"


def parse_cells(spec, rows: int, cols: int) -> list[tuple[int, int]]:
    """Parse a cell list like "b3, C4 D2" into 0-based (row, col) pairs.

    Rows are letters A.. (case-insensitive), columns are 1-based numbers.
    Raises ValueError on malformed or out-of-range cells.
    """
    rows, cols = int(rows), int(cols)
    out: list[tuple[int, int]] = []
    for tok in re.split(r"[,\s]+", str(spec or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"([A-Za-z])(\d+)", tok)
        if not m:
            raise ValueError(f'bad cell "{tok}" (use labels like B3 or C4)')
        r = ord(m.group(1).upper()) - ord("A")
        c = int(m.group(2)) - 1
        if not (0 <= r < rows and 0 <= c < cols):
            raise ValueError(f'cell "{tok}" out of range (grid is {rows}x{cols})')
        out.append((r, c))
    if not out:
        raise ValueError("no cells given")
    return out


def cell_center(grid: dict, row: int, col: int) -> tuple[int, int]:
    """Screen-px center of a grid cell. ``grid`` is the dict stored in
    challenge state: {"rect": [x,y,w,h], "rows", "cols"}."""
    x, y, w, h = (int(v) for v in grid["rect"][:4])
    rows, cols = int(grid["rows"]), int(grid["cols"])
    return (
        int(x + (col + 0.5) * w / cols),
        int(y + (row + 0.5) * h / rows),
    )


def grid_dims(rect, rows=None, cols=None):
    """(rows, cols) for a capture rect: ~DEFAULT_CELL_PX cells, overrides win."""
    w, h = int(rect[2]), int(rect[3])
    r = int(rows) if rows else max(1, round(h / DEFAULT_CELL_PX))
    c = int(cols) if cols else max(1, round(w / DEFAULT_CELL_PX))
    return max(1, min(r, MAX_ROWS)), max(1, min(c, MAX_COLS))


# ------------------------------------------------------------- webhook --

def webhook_multipart(filename: str, png_bytes: bytes, text: str):
    """Build (content_type, body) for a Discord-style webhook file upload.

    Pure - no network. Fields: ``content`` (text) + ``files[0]`` (the PNG).
    """
    boundary = "----deskctl" + secrets.token_hex(8)
    parts = []

    def field(name: str, value: str):
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )

    field("content", text)
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        + png_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)
