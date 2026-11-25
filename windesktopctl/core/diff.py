"""Cheap change detection: downscale to 256x144 grayscale, count cells whose
abs diff exceeds CELL_THRESHOLD; >= MIN_CELLS counts as "changed".

Cell-level counting (instead of mean abs diff) ignores a blinking caret
(1-2 changed cells) and global brightness flicker, while still catching a
few typed characters.
"""

from __future__ import annotations

from PIL import Image, ImageChops

THUMB_W = 256
THUMB_H = 144

CELL_THRESHOLD = 32  # per-cell abs diff (0..255) that counts as different
MIN_CELLS = 2        # differing cells required to report changed


def thumb(img: Image.Image) -> Image.Image:
    return img.convert("L").resize((THUMB_W, THUMB_H), Image.BOX)


def to_bytes(t: Image.Image) -> bytes:
    return t.tobytes()


def from_bytes(data: bytes) -> Image.Image | None:
    """Decode a stored baseline thumbnail. Returns None when the payload
    doesn't match the current thumb size (e.g. an old 64x36 baseline) so the
    caller treats it as 'no baseline' instead of crashing."""
    if not data or len(data) != THUMB_W * THUMB_H:
        return None
    try:
        return Image.frombytes("L", (THUMB_W, THUMB_H), data)
    except Exception:
        return None


def mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    """Diagnostic only: mean abs diff (0..255) over the thumbnails."""
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    hist = diff.histogram()
    n = a.width * a.height
    if n == 0:
        return 0.0
    return sum(i * c for i, c in enumerate(hist)) / n


def changed(a: Image.Image, b: Image.Image) -> bool:
    """True when >= MIN_CELLS thumbnail pixels differ by > CELL_THRESHOLD."""
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    hist = diff.histogram()
    # pixel values CELL_THRESHOLD+1..255 count as different cells
    return sum(hist[CELL_THRESHOLD + 1 :]) >= MIN_CELLS
