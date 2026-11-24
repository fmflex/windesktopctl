"""Mouse/keyboard control via ctypes SendInput (no pyautogui).

All functions take/return physical virtual-screen pixels. Keyboard combos
are single chords like "ctrl+shift+s"; unicode typing uses KEYEVENTF_UNICODE.
"""

from __future__ import annotations

import ctypes
import re
import struct
import time
from ctypes import wintypes as wt

from deskctl.core import dpi
from deskctl.core import DeskctlError

user32 = ctypes.windll.user32

# ------------------------------------------------------------------ ctypes --

PVOID = ctypes.c_void_p  # ULONG_PTR-sized dwExtraInfo


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", PVOID),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", PVOID),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wt.DWORD),
        ("wParamL", wt.WORD),
        ("wParamH", wt.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT
_INPUT_SIZE = ctypes.sizeof(INPUT)

user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL

user32.VkKeyScanW.argtypes = [wt.LPCWSTR]
user32.VkKeyScanW.restype = ctypes.c_short


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, _INPUT_SIZE)
    if sent != len(inputs):
        raise DeskctlError(f"SendInput delivered {sent}/{len(inputs)} events")


def _mi(dx=0, dy=0, data=0, flags=0) -> INPUT:
    i = INPUT()
    i.type = INPUT_MOUSE
    m = i.u.mi
    m.dx = int(dx)
    m.dy = int(dy)
    m.mouseData = int(data) & 0xFFFFFFFF
    m.dwFlags = int(flags)
    m.time = 0
    m.dwExtraInfo = None
    return i


def _ki(vk=0, scan=0, flags=0) -> INPUT:
    i = INPUT()
    i.type = INPUT_KEYBOARD
    k = i.u.ki
    k.wVk = int(vk) & 0xFFFF
    k.wScan = int(scan) & 0xFFFF
    k.dwFlags = int(flags)
    k.time = 0
    k.dwExtraInfo = None
    return i


# ------------------------------------------------------------------- mouse --

def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def move_to(x: int, y: int) -> None:
    """Move cursor to absolute screen coords via SendInput absolute move."""
    vx, vy, vw, vh = dpi.virtual_screen()
    span_x = max(1, vw - 1)
    span_y = max(1, vh - 1)
    nx = int(round((int(x) - vx) * 65535 / span_x))
    ny = int(round((int(y) - vy) * 65535 / span_y))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    _send(
        [
            _mi(
                nx,
                ny,
                0,
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            )
        ]
    )


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    if button not in _BUTTONS:
        raise DeskctlError(f"unknown mouse button {button!r}")
    down, up = _BUTTONS[button]
    move_to(x, y)
    time.sleep(0.02)
    _send([_mi(0, 0, 0, down), _mi(0, 0, 0, up)])
    if double:
        time.sleep(0.1)
        _send([_mi(0, 0, 0, down), _mi(0, 0, 0, up)])


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> None:
    """Press left button at (x1,y1), drag to (x2,y2), release."""
    move_to(x1, y1)
    time.sleep(0.02)
    _send([_mi(0, 0, 0, MOUSEEVENTF_LEFTDOWN)])
    try:
        for i in range(1, steps + 1):
            ix = int(round(x1 + (x2 - x1) * i / steps))
            iy = int(round(y1 + (y2 - y1) * i / steps))
            move_to(ix, iy)
            time.sleep(0.012)
    finally:
        _send([_mi(0, 0, 0, MOUSEEVENTF_LEFTUP)])


def scroll(
    amount: float,
    x: int | None = None,
    y: int | None = None,
    horizontal: bool = False,
) -> None:
    """Scroll wheel; positive amount = up/away (or right when horizontal),
    negative = down/toward (or left). amount is in wheel notches (~120 delta
    units each)."""
    if x is not None and y is not None:
        move_to(int(x), int(y))
        time.sleep(0.02)
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    remaining = int(round(float(amount) * WHEEL_DELTA))
    if remaining == 0:
        raise DeskctlError("scroll amount too small")
    sign = 1 if remaining > 0 else -1
    while remaining:
        d = sign * min(120, abs(remaining))
        _send([_mi(0, 0, d, flag)])
        remaining -= d


# ----------------------------------------------------------------- keyboard --

# Keys that need KEYEVENTF_EXTENDEDKEY for correct behavior.
EXTENDED_VKS = frozenset(
    {
        0x13,  # VK_PAUSE
        0x21,  # VK_PRIOR (PageUp)
        0x22,  # VK_NEXT (PageDown)
        0x23,  # VK_END
        0x24,  # VK_HOME
        0x25,  # VK_LEFT
        0x26,  # VK_UP
        0x27,  # VK_RIGHT
        0x28,  # VK_DOWN
        0x2C,  # VK_SNAPSHOT (PrintScreen)
        0x2D,  # VK_INSERT
        0x2E,  # VK_DELETE
        0x5B,  # VK_LWIN
        0x5C,  # VK_RWIN
        0x5D,  # VK_APPS
        0x6F,  # VK_DIVIDE (numpad /)
        0x90,  # VK_NUMLOCK
        0x91,  # VK_SCROLL
        0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC,  # browser keys
        0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3,  # media keys
    }
)

MODIFIER_VKS = {
    "ctrl": 0x11,  # VK_CONTROL
    "control": 0x11,
    "ctl": 0x11,
    "shift": 0x10,  # VK_SHIFT
    "alt": 0x12,  # VK_MENU
    "win": 0x5B,  # VK_LWIN
    "windows": 0x5B,
    "super": 0x5B,
    "meta": 0x5B,
    "cmd": 0x5B,
    "command": 0x5B,
}

NAMED_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "bksp": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "capslock": 0x14,
    "numlock": 0x90,
    "scrolllock": 0x91,
    "printscreen": 0x2C,
    "prtsc": 0x2C,
    "pause": 0x13,
    "break": 0x13,
    "apps": 0x5D,
    "menu": 0x5D,
    "volumemute": 0xAD,
    "volumedown": 0xAE,
    "volumeup": 0xAF,
    "medianext": 0xB0,
    "mediaprev": 0xB1,
    "mediastop": 0xB2,
    "mediaplaypause": 0xB3,
    "browserback": 0xA6,
    "browserforward": 0xA7,
    "browserrefresh": 0xA8,
    "browserhome": 0xAC,
    "browsersearch": 0xAA,
    "browserfavorites": 0xAB,
}

_FKEY_RE = re.compile(r"^f(\d{1,2})$")

# US-layout punctuation virtual-key codes (VkKeyScanW is unreliable on some
# systems, so shifted/unshifted punctuation is mapped explicitly).
_US_PUNCT_VK = {
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, "`": 0xC0, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
# shifted punctuation char -> (unshifted base char, needs_shift=True)
_US_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
    "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
    "|": "\\", ":": ";", '"': "'", "~": "`", "<": ",", ">": ".", "?": "/",
}


def _base_vk(c: str) -> int | None:
    if "a" <= c <= "z":
        return ord(c.upper())
    if "A" <= c <= "Z":
        return ord(c)
    if "0" <= c <= "9":
        return ord(c)
    return _US_PUNCT_VK.get(c)


def _key_vk(token: str) -> tuple[int, bool]:
    """Map one non-modifier key token to (vk, needs_shift). US layout assumed
    for punctuation."""
    t = token.lower()
    if t in NAMED_KEYS:
        return NAMED_KEYS[t], False
    m = _FKEY_RE.match(t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 24:
            return 0x6F + n, False
        raise DeskctlError(f'unknown key "{token}"')
    if len(token) == 1:
        c = token
        if c in _US_SHIFTED:
            base = _US_SHIFTED[c]
            vk = _base_vk(base)
            if vk is not None:
                return vk, True
        vk = _base_vk(c)
        if vk is not None:
            return vk, False
        # last resort: ask Windows (may fail on some systems/layouts)
        try:
            ret = user32.VkKeyScanW(c) & 0xFFFF
            if ret != 0xFFFF:
                vk2 = ret & 0xFF
                if vk2:
                    return vk2, bool((ret >> 8) & 1)
        except Exception:
            pass
        raise DeskctlError(f'unknown key "{token}" (US keyboard layout assumed)')
    raise DeskctlError(f'unknown key "{token}"')


def parse_combo(combo: str) -> tuple[list[int], int, bool]:
    """Parse "ctrl+shift+s" -> (modifier_vks, key_vk, extra_shift_needed)."""
    parts = [p for p in str(combo).replace(" ", "").split("+") if p != ""]
    if not parts:
        raise DeskctlError("empty key combo")
    mods: list[int] = []
    key_vk = 0
    extra_shift = False
    for tok in parts:
        tl = tok.lower()
        if tl in MODIFIER_VKS:
            vk = MODIFIER_VKS[tl]
            if vk not in mods:
                mods.append(vk)
            continue
        if key_vk:
            raise DeskctlError(f'ambiguous combo "{combo}": more than one non-modifier key')
        key_vk, extra_shift = _key_vk(tok)
    if not key_vk:
        raise DeskctlError(f'combo "{combo}" has no non-modifier key')
    if extra_shift and 0x10 not in mods:
        mods.append(0x10)
    return mods, key_vk, extra_shift


def _key_flags(vk: int) -> int:
    return KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0


def press_combo(combo: str) -> None:
    """Press one key combo, e.g. "ctrl+s", "alt+tab", "enter", "f5", "win+r"."""
    mods, key_vk, _ = parse_combo(combo)
    seq: list[INPUT] = []
    for m in mods:
        seq.append(_ki(m, 0, _key_flags(m)))
    f = _key_flags(key_vk)
    seq.append(_ki(key_vk, 0, f))
    seq.append(_ki(key_vk, 0, f | KEYEVENTF_KEYUP))
    for m in reversed(mods):
        seq.append(_ki(m, 0, _key_flags(m) | KEYEVENTF_KEYUP))
    _send(seq)


def type_text(text: str, user_active=None) -> None:
    """Type arbitrary text via KEYEVENTF_UNICODE (handles any Unicode).

    Types in ~20-char chunks; if ``user_active`` (a callable returning bool)
    reports the user took over between chunks, aborts with
    "stopped after N/M chars: user took over".
    """
    try:
        data = str(text).encode("utf-16-le")
    except UnicodeEncodeError:
        data = str(text).encode("utf-16-le", "replace")
    units = struct.unpack(f"<{len(data) // 2}H", data)
    total = len(units)
    chunk = 20  # chars per SendInput call
    sent = 0
    for i in range(0, total, chunk):
        seq: list[INPUT] = []
        for u in units[i : i + chunk]:
            seq.append(_ki(0, u, KEYEVENTF_UNICODE))
            seq.append(_ki(0, u, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        _send(seq)
        sent = min(total, i + chunk)
        if user_active is not None and sent < total and user_active():
            raise DeskctlError(f"stopped after {sent}/{total} chars: user took over")
