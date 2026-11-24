"""Unit tests for diff.py - change detection on synthetic PIL images."""

from PIL import Image, ImageDraw, ImageFont

from deskctl.core import diff
from deskctl.core import state as st

SCREEN = (2880, 1758)  # this machine's virtual screen size


def _white(w=SCREEN[0], h=SCREEN[1]):
    return Image.new("RGB", (w, h), "white")


def _cells_changed(a: Image.Image, b: Image.Image) -> int:
    """Number of thumbnail cells with abs diff > CELL_THRESHOLD."""
    from PIL import ImageChops

    ta, tb = diff.thumb(a), diff.thumb(b)
    hist = ImageChops.difference(ta, tb).histogram()
    return sum(hist[diff.CELL_THRESHOLD + 1 :])


def test_thumb_size_and_mode():
    img = Image.new("RGB", (400, 300), "white")
    t = diff.thumb(img)
    assert t.size == (diff.THUMB_W, diff.THUMB_H)
    assert t.mode == "L"


def test_identical_images_not_changed():
    a = _white()
    b = _white()
    assert diff.mean_abs_diff(diff.thumb(a), diff.thumb(b)) == 0.0
    assert not diff.changed(diff.thumb(a), diff.thumb(b))


def test_caret_toggle_ignored():
    """A 2px-wide x 20px caret line toggling on a full-screen white image
    must NOT count as a change (blinking cursor)."""
    a = _white()
    b = _white()
    d = ImageDraw.Draw(b)
    d.rectangle([500, 500, 501, 519], fill="black")  # 2x20 caret
    assert _cells_changed(a, b) < diff.MIN_CELLS
    assert not diff.changed(diff.thumb(a), diff.thumb(b))


def test_typed_text_detected():
    """'hello' drawn at normal readable size on a full-screen white image
    must count as a change."""
    a = _white()
    b = _white()
    d = ImageDraw.Draw(b)
    font = ImageFont.load_default(size=24)
    d.text((200, 200), "hello", font=font, fill="black")
    assert _cells_changed(a, b) >= diff.MIN_CELLS
    assert diff.changed(diff.thumb(a), diff.thumb(b))


def test_global_brightness_shift_ignored():
    """A uniform +5 brightness shift must NOT count as a change."""
    a = Image.new("RGB", SCREEN, (128, 128, 128))
    b = Image.eval(a.convert("L"), lambda p: min(255, p + 5)).convert("RGB")
    assert _cells_changed(a, b) == 0
    assert not diff.changed(diff.thumb(a), diff.thumb(b))


def test_black_vs_white_changed():
    a = diff.thumb(Image.new("RGB", (200, 100), "white"))
    b = diff.thumb(Image.new("RGB", (200, 100), "black"))
    assert diff.mean_abs_diff(a, b) > 200
    assert diff.changed(a, b)


def test_mean_abs_diff_range():
    a = diff.thumb(Image.new("RGB", (100, 100), "black"))
    b = diff.thumb(Image.new("RGB", (100, 100), "white"))
    d = diff.mean_abs_diff(a, b)
    assert 200 < d <= 255


def test_baseline_bytes_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKCTL_HOME", str(tmp_path / "d"))
    t = diff.thumb(Image.new("RGB", (50, 50), "red"))
    data = diff.to_bytes(t)
    st.set_baseline(data)
    assert st.get_baseline() == data
    assert diff.from_bytes(st.get_baseline()).size == (diff.THUMB_W, diff.THUMB_H)


def test_from_bytes_rejects_wrong_size():
    # an old 64x36 baseline (2304 bytes) must decode to None, not crash
    assert diff.from_bytes(bytes(64 * 36)) is None
    assert diff.from_bytes(b"") is None
    assert diff.from_bytes(None) is None
