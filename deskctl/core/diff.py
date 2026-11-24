"""Cheap change detection: downscale to ~64x36 grayscale, mean abs diff."""

from __future__ import annotations

from PIL import Image, ImageChops

THUMB_W = 64
THUMB_H = 36

# mean abs diff (0..255) above this counts as "changed"
DEFAULT_THRESHOLD = 2.0


def thumb(img: Image.Image) -> Image.Image:
    return img.convert("L").resize((THUMB_W, THUMB_H), Image.LANCZOS)


def to_bytes(t: Image.Image) -> bytes:
    return t.tobytes()


def from_bytes(data: bytes) -> Image.Image:
    return Image.frombytes("L", (THUMB_W, THUMB_H), data)


def mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a.convert("L"), b.convert("L"))
    hist = diff.histogram()
    n = a.width * a.height
    if n == 0:
        return 0.0
    return sum(i * c for i, c in enumerate(hist)) / n


def changed(a: Image.Image, b: Image.Image, threshold: float = DEFAULT_THRESHOLD) -> bool:
    return mean_abs_diff(a, b) > threshold
