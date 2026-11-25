"""OCR via winrt Windows.Media.Ocr (wrapped by the winocr dependency).

Runs on the core worker thread. Degrades gracefully: if the OCR engine or
language pack is unavailable, recognize() raises WindesktopctlError with a clear
message and observe() callers decide whether to fall back.
"""

from __future__ import annotations

import asyncio

from PIL import Image

from windesktopctl.core import WindesktopctlError

_engine = None
_engine_failed = False


def _get_engine():
    """Create (once) an OcrEngine: user profile languages first, then 'en',
    then any available recognizer language."""
    global _engine, _engine_failed
    if _engine is not None or _engine_failed:
        return _engine
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except Exception as ex:
        _engine_failed = True
        raise WindesktopctlError(f"OCR unavailable: winrt packages not importable ({ex})")

    try:
        eng = OcrEngine.try_create_from_user_profile_languages()
        if eng is not None:
            _engine = eng
            return _engine
    except Exception:
        pass

    try:
        avail = [str(l.language_tag) for l in OcrEngine.available_recognizer_languages]
    except Exception:
        avail = []

    candidates = ["en", "en-US"] + avail
    seen = set()
    for tag in candidates:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        try:
            if not OcrEngine.is_language_supported(Language(tag)):
                continue
            eng = OcrEngine.try_create_from_language(Language(tag))
            if eng is not None:
                _engine = eng
                return _engine
        except Exception:
            continue

    _engine_failed = True
    raise WindesktopctlError(
        "OCR unavailable: no usable recognizer language "
        f"(available: {', '.join(avail) or 'none'})"
    )


async def _recognize_async(img: Image.Image, engine):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    rgba = img.convert("RGBA")
    writer = DataWriter()
    writer.write_bytes(rgba.tobytes())
    sb = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.RGBA8, rgba.width, rgba.height
    )
    return await engine.recognize_async(sb)


def recognize(img: Image.Image) -> list[dict]:
    """Recognize text lines in a PIL image.

    Returns [{"text": str, "rect": (x, y, w, h)}] with rects in image pixels
    (caller offsets by capture origin). Raises WindesktopctlError if OCR is
    unavailable.
    """
    engine = _get_engine()
    result = asyncio.run(_recognize_async(img, engine))

    lines = []
    for line in getattr(result, "lines", []) or []:
        text = (getattr(line, "text", "") or "").strip()
        if not text:
            continue
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")
        for word in getattr(line, "words", []) or []:
            r = getattr(word, "bounding_rect", None)
            if r is None:
                continue
            x0 = min(x0, r.x)
            y0 = min(y0, r.y)
            x1 = max(x1, r.x + r.width)
            y1 = max(y1, r.y + r.height)
        if x1 <= x0 or y1 <= y0:
            continue
        lines.append(
            {
                "text": text,
                "rect": (int(x0), int(y0), int(x1 - x0), int(y1 - y0)),
            }
        )
    return lines
