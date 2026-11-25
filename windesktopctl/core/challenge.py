"""Human-verification challenge ("CAPTCHA") relay - detection + grid overlay.

windesktopctl NEVER solves or guesses challenges: no OCR-to-answer, no automatic
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
CHAIN_GAP = 96  # px; tile/button elements this close to the union join it
DEFAULT_CELL_PX = 70  # target cell size in screen px
MAX_DIM = 1024  # longest side of the saved challenge image
MAX_ROWS = 40
MAX_COLS = 40

# Roles that can be image-grid tiles when detecting the challenge area.
TILE_ROLES = ("Button", "Image", "ListItem")
# Lattice auto-numbering only tries these square layouts.
LATTICE_DIMS = (3, 4)

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


def _union_rects(rects):
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


def _clamp_rect(rect, window_rect):
    x, y, w, h = (int(v) for v in rect[:4])
    if window_rect is not None:
        wx, wy, ww, wh = (int(v) for v in window_rect[:4])
        x = max(x, wx)
        y = max(y, wy)
        x1 = min(x + w, wx + ww)
        y1 = min(y + h, wy + wh)
        w, h = x1 - x, y1 - y
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _near_union(e_rect, union, gap):
    """Element rect within `gap` px of the union bounds (either axis)."""
    x, y, w, h = union
    ex, ey, ew, eh = e_rect
    return not (
        ex + ew < x - gap
        or ex > x + w + gap
        or ey + eh < y - gap
        or ey > y + h + gap
    )


def _grid_scope_rect(matched, elements, window_rect):
    """Tighter capture rect for grid challenges: union of the matched text
    elements PLUS tile/button elements inside the challenge container.

    The "container" is approximated two ways: (a) the smallest element rect
    that fully contains every matched element (a Group/Pane card), or
    (b) proximity chaining - Button/Image/ListItem elements within CHAIN_GAP
    of the growing union are absorbed (fixpoint). Page chrome far away is
    never absorbed. Returns a clamped (x,y,w,h) or None.
    """
    matched_rects = [r for r in (_rect_of(e) for e in matched) if r is not None]
    if not matched_rects:
        return None
    base = _union_rects(matched_rects)

    # (a) smallest containing non-Document element = the challenge card.
    container = None
    for e in elements or []:
        role = _attr(e, "role")
        if role in ("Document", ""):
            continue
        r = _rect_of(e)
        if r is None:
            continue
        ex, ey, ew, eh = r
        if ew * eh <= 0:
            continue
        # must contain every matched rect
        if all(
            ex <= mr[0] and ey <= mr[1]
            and ex + ew >= mr[0] + mr[2]
            and ey + eh >= mr[1] + mr[3]
            for mr in matched_rects
        ):
            if container is None or ew * eh < container[2] * container[3]:
                container = r
    if container is not None and window_rect is not None:
        wx, wy, ww, wh = (int(v) for v in window_rect[:4])
        if container[2] * container[3] > 0.98 * ww * wh:
            container = None  # it's basically the whole window - useless
    if container is not None:
        base = _union_rects([base, container])

    # (b) absorb nearby tile/button elements (fixpoint - each pass can reach
    # one more "layer", e.g. 4 tile rows + the verify button below them).
    tiles = []
    for e in elements or []:
        if _attr(e, "role") not in TILE_ROLES:
            continue
        r = _rect_of(e)
        if r is not None:
            tiles.append(r)
    for _ in range(8):
        grown = [base]
        added = False
        for tr in tiles:
            if _near_union(tr, base, CHAIN_GAP):
                grown.append(tr)
                added = True
        if not added:
            break
        new_base = _union_rects(grown)
        if new_base == base:
            break
        base = new_base

    padded = (
        base[0] - GRID_PAD,
        base[1] - GRID_PAD,
        base[2] + 2 * GRID_PAD,
        base[3] + 2 * GRID_PAD,
    )
    return _clamp_rect(padded, window_rect)


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

    if kind == "grid":
        # Tighten: matched text alone is a tiny island - pull in the tile/
        # button elements of the challenge container so the capture covers
        # the widget, not the whole window.
        scoped = _grid_scope_rect(matched, elems, window_rect)
        if scoped is not None:
            rect = scoped
            fallback = False

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


def overlay_grid(img, rect, origin, rows: int, cols: int, numbered: bool = False):
    """Draw a labeled cell grid over a captured challenge image.

    ``img`` is the capture of ``rect`` (x,y,w,h screen px); ``origin`` is
    the screen px of the image's top-left corner (normally == rect[:2]).
    Cells partition ``rect`` evenly.

    numbered=False (default): rows lettered A.., columns numbered 1..., a
    faint "B3" id in each cell. numbered=True: cells numbered 1..N row-major
    with a big clear number in each cell's corner (for image-tile grids -
    the user just says "3 8 14"). Returns an RGB image (same size).
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
    if numbered:
        big = _font(16)
        # Big tile number in each cell's top-left corner, 1..N row-major.
        for r in range(rows):
            for c in range(cols):
                n = r * cols + c + 1
                cx = ix(rx + c * rw / cols) + 3
                cy = iy(ry + r * rh / rows) + 2
                # dark outline halo so the number reads on any tile
                d2.text((cx + 1, cy + 1), str(n), fill=(0, 0, 0, 200), font=big)
                d2.text((cx, cy), str(n), fill=(255, 30, 30, 255), font=big)
        return rgba.convert("RGB")
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


# ------------------------------------------------------- numbered grids --

def cell_number(row: int, col: int, cols: int) -> int:
    """1-based row-major tile number for a cell."""
    return row * int(cols) + col + 1


def parse_grid_spec(spec) -> tuple[int, int]:
    """Parse a grid spec like "4x4" / "4X4" / " 3 x 3 " -> (rows, cols)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[x×]\s*(\d{1,2})\s*", str(spec or ""), re.I)
    if not m:
        raise ValueError(f'bad grid spec {spec!r} (use "RxC", e.g. "4x4")')
    r, c = int(m.group(1)), int(m.group(2))
    if not (1 <= r <= MAX_ROWS and 1 <= c <= MAX_COLS):
        raise ValueError(f'grid {r}x{c} out of range (max {MAX_ROWS}x{MAX_COLS})')
    return r, c


def parse_numbered_cells(spec, rows: int, cols: int) -> list[tuple[int, int]]:
    """Parse numbered-mode cells: "3 8 14" / "3,8,14" -> 0-based (row, col).

    Numbers are 1..rows*cols row-major. Raises ValueError on malformed or
    out-of-range input (including lettered cells - mode mismatch).
    """
    rows, cols = int(rows), int(cols)
    total = rows * cols
    out: list[tuple[int, int]] = []
    for tok in re.split(r"[,\s]+", str(spec or "").strip()):
        if not tok:
            continue
        if not re.fullmatch(r"\d+", tok):
            raise ValueError(
                f'bad tile "{tok}" - this is a NUMBERED grid; '
                f'use numbers 1-{total} (e.g. "3 8 14")'
            )
        n = int(tok)
        if not (1 <= n <= total):
            raise ValueError(f'tile "{tok}" out of range (grid is {rows}x{cols} = 1-{total})')
        out.append(((n - 1) // cols, (n - 1) % cols))
    if not out:
        raise ValueError("no cells given")
    return out


_CELL_TOKEN = re.compile(r"^\s*([A-Za-z])(\d{1,2})\s*$")


def _letter_cell(tok: str, rows: int, cols: int) -> tuple[int, int]:
    m = _CELL_TOKEN.match(tok)
    if not m:
        raise ValueError(f'bad cell "{tok}" (use labels like B3 or C4)')
    r = ord(m.group(1).upper()) - ord("A")
    c = int(m.group(2)) - 1
    if not (0 <= r < rows and 0 <= c < cols):
        raise ValueError(f'cell "{tok}" out of range (grid is {rows}x{cols})')
    return r, c


def parse_region(spec, grid: dict):
    """Parse a region like "C4:I10" (inclusive lettered cells of the STORED
    grid geometry) -> screen rect (x,y,w,h). Raises ValueError."""
    m = re.fullmatch(
        r"\s*([A-Za-z]\d{1,2})\s*:\s*([A-Za-z]\d{1,2})\s*", str(spec or "")
    )
    if not m:
        raise ValueError(f'bad region {spec!r} (use "C4:I10" - cells of the last capture)')
    rows = int(grid.get("rows") or 0)
    cols = int(grid.get("cols") or 0)
    if not rows or not cols:
        raise ValueError("no grid geometry stored; call challenge() first")
    r1, c1 = _letter_cell(m.group(1), rows, cols)
    r2, c2 = _letter_cell(m.group(2), rows, cols)
    if r1 > r2:
        r1, r2 = r2, r1
    if c1 > c2:
        c1, c2 = c2, c1
    return cells_rect(grid, r1, c1, r2, c2)


def cells_rect(grid: dict, r1: int, c1: int, r2: int, c2: int):
    """Screen rect covering cells (r1,c1)-(r2,c2) inclusive, through the
    stored grid geometry."""
    x, y, w, h = (int(v) for v in grid["rect"][:4])
    rows, cols = int(grid["rows"]), int(grid["cols"])
    x0 = x + c1 * w / cols
    y0 = y + r1 * h / rows
    x1 = x + (c2 + 1) * w / cols
    y1 = y + (r2 + 1) * h / rows
    return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))


# ------------------------------------------------------------- lattice --

def _cluster_1d(vals, tol):
    """Group sorted values into clusters; gap > tol starts a new cluster.
    Returns list of clusters (each a list of original values)."""
    clusters = []
    for v in sorted(vals):
        if clusters and v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return clusters


def infer_lattice(elements, rect):
    """Detect a regular square lattice (3x3 or 4x4) of similar-size
    Button/Image/ListItem elements inside ``rect``.

    Returns (rows, cols, bbox_rect) or None. Similar-size = w/h within 30%
    of the median. Lattice = every row/col cluster cell occupied exactly
    once. Used to auto-switch image grids to numbered mode.
    """
    rx, ry, rw, rh = (int(v) for v in rect[:4])
    cands = []
    for e in elements or []:
        if _attr(e, "role") not in TILE_ROLES:
            continue
        r = _rect_of(e)
        if r is None:
            continue
        cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
        if not (rx <= cx < rx + rw and ry <= cy < ry + rh):
            continue
        cands.append((r, cx, cy))
    if len(cands) < 9:
        return None
    ws = sorted(r[2] for r, _, _ in cands)
    hs = sorted(r[3] for r, _, _ in cands)
    mw = ws[len(ws) // 2]
    mh = hs[len(hs) // 2]
    if mw <= 0 or mh <= 0:
        return None
    lat = [
        (r, cx, cy)
        for r, cx, cy in cands
        if abs(r[2] - mw) <= mw * 0.3 and abs(r[3] - mh) <= mh * 0.3
    ]
    if len(lat) < 9:
        return None
    tol = max(mw, mh) * 0.45
    xcl = _cluster_1d([cx for _, cx, _ in lat], tol)
    ycl = _cluster_1d([cy for _, _, cy in lat], tol)
    ncols, nrows = len(xcl), len(ycl)
    if ncols not in LATTICE_DIMS or nrows not in LATTICE_DIMS:
        return None
    if len(lat) != nrows * ncols:
        return None
    # assign each element to (row-cluster, col-cluster); every cell once
    def _cell_idx(clusters, v):
        for i, cl in enumerate(clusters):
            if v in cl:
                return i
        return -1

    cells = set()
    for _, cx, cy in lat:
        cells.add((_cell_idx(ycl, cy), _cell_idx(xcl, cx)))
    if len(cells) != nrows * ncols or any(r < 0 or c < 0 for r, c in cells):
        return None
    bbox = _union_rects([r for r, _, _ in lat])
    return nrows, ncols, bbox


# ------------------------------------------------------------- webhook --

def webhook_multipart(filename: str, png_bytes: bytes, text: str):
    """Build (content_type, body) for a Discord-style webhook file upload.

    Pure - no network. Fields: ``content`` (text) + ``files[0]`` (the PNG).
    """
    boundary = "----windesktopctl" + secrets.token_hex(8)
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
