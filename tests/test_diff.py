"""Unit tests for diff.py - change detection on synthetic PIL images."""

from PIL import Image

from deskctl.core import diff
from deskctl.core import state as st


def test_thumb_size_and_mode():
    img = Image.new("RGB", (400, 300), "white")
    t = diff.thumb(img)
    assert t.size == (diff.THUMB_W, diff.THUMB_H)
    assert t.mode == "L"


def test_identical_images_not_changed():
    a = diff.thumb(Image.new("RGB", (200, 100), (200, 200, 200)))
    b = diff.thumb(Image.new("RGB", (200, 100), (200, 200, 200)))
    assert diff.mean_abs_diff(a, b) == 0.0
    assert not diff.changed(a, b)


def test_black_vs_white_changed():
    a = diff.thumb(Image.new("RGB", (200, 100), "white"))
    b = diff.thumb(Image.new("RGB", (200, 100), "black"))
    assert diff.mean_abs_diff(a, b) > 200
    assert diff.changed(a, b)


def test_subtle_change_below_threshold():
    a = Image.new("RGB", (200, 100), "white")
    b = Image.new("RGB", (200, 100), "white")
    # flip a handful of pixels
    px = b.load()
    for i in range(5):
        px[i * 10, 0] = (0, 0, 0)
    ta, tb = diff.thumb(a), diff.thumb(b)
    assert not diff.changed(ta, tb)


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
    assert diff.from_bytes(st.get_baseline()).size == (64, 36)
