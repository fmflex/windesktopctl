"""Persistent state under %LOCALAPPDATA%\\deskctl (DESKCTL_HOME overrides).

Holds the last observe element map (so element ids survive across CLI/agent
processes), the last screenshot mapping (for "i:x,y" image coords), and the
diff baseline thumbnail.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path

from deskctl.core import DeskctlError
from deskctl.core import elements as el
from deskctl.core import windows as win

OBSERVE_MAX_AGE = 600.0  # seconds; element ids older than this are stale
SHOT_MAX_AGE = 600.0


def state_dir() -> Path:
    env = os.environ.get("DESKCTL_HOME")
    if env:
        d = Path(env)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "deskctl"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return state_dir() / "state.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(state: dict) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _update(key: str, value: dict) -> None:
    st = load()
    st[key] = value
    save(st)


# ---------------------------------------------------------------- observe --

def record_observe(
    *,
    hwnd: int,
    window_key,
    title: str,
    proc: str,
    rect: tuple[int, int, int, int],
    source: str,
    elements: list[el.Element],
    truncated_count: int,
    walk_truncated: bool,
) -> None:
    _update(
        "observe",
        {
            "ts": time.time(),
            "hwnd": int(hwnd),
            "window": window_key,
            "title": title,
            "proc": proc,
            "rect": list(rect),
            "source": source,
            "truncated": truncated_count,
            "walk_truncated": walk_truncated,
            "elements": {str(e.id): e.to_dict() for e in elements},
        },
    )


def get_observe() -> dict | None:
    return load().get("observe")


def observe_age() -> float | None:
    obs = get_observe()
    if not obs:
        return None
    return time.time() - float(obs.get("ts", 0))


def get_element(eid: int) -> el.Element | None:
    obs = get_observe()
    if not obs:
        return None
    d = obs.get("elements", {}).get(str(eid))
    return el.Element.from_dict(d) if d else None


# -------------------------------------------------------------- screenshot --

def record_screenshot(
    *,
    origin_x: int,
    origin_y: int,
    scale: float,
    width: int,
    height: int,
    window_key,
) -> None:
    _update(
        "screenshot",
        {
            "ts": time.time(),
            "origin_x": int(origin_x),
            "origin_y": int(origin_y),
            "scale": float(scale),
            "width": int(width),
            "height": int(height),
            "window": window_key,
        },
    )


def get_screenshot() -> dict | None:
    return load().get("screenshot")


def screenshot_age() -> float | None:
    shot = get_screenshot()
    if not shot:
        return None
    return time.time() - float(shot.get("ts", 0))


# ------------------------------------------------------------- diff baseline --

def set_baseline(png_bytes: bytes) -> None:
    """Store a small grayscale thumbnail (raw 'L' bytes, 64x36) base64'd."""
    _update("diff_baseline", {"ts": time.time(), "data": base64.b64encode(png_bytes).decode()})


def get_baseline() -> bytes | None:
    st = load()
    d = st.get("diff_baseline")
    if not d or "data" not in d:
        return None
    try:
        return base64.b64decode(d["data"])
    except Exception:
        return None


# -------------------------------------------------------------- act targets --

def _check_observe_window_fresh(obs: dict) -> None:
    """Stale-id protection for window observes (screen observes have hwnd 0):
    window gone -> error; not foreground -> focus it; rect changed -> error."""
    hwnd = int(obs.get("hwnd") or 0)
    if not hwnd or obs.get("window") == "screen":
        return
    info = win.window_info(hwnd)
    if info is None:
        raise DeskctlError("window closed; observe again")
    if win.focused_hwnd() != hwnd:
        win.set_foreground(hwnd)
        info = win.window_info(hwnd)
        if info is None:
            raise DeskctlError("window closed; observe again")
    want = obs.get("rect")
    if want is not None and [int(v) for v in info.rect] != [int(v) for v in want]:
        raise DeskctlError("window moved/resized since observe; observe again")


def resolve_target(spec: str) -> tuple[int, int, str]:
    """Resolve an act() target spec to screen coords.

    Accepted forms: "12" (element id from last observe), "x,y" (screen px),
    "i:x,y" (coords on last screenshot image). Returns (x, y, label).
    """
    s = str(spec).strip()
    now = time.time()

    m = re.fullmatch(r"(\d+)", s)
    if m:
        eid = int(m.group(1))
        obs = get_observe()
        if not obs or now - float(obs.get("ts", 0)) > OBSERVE_MAX_AGE:
            raise DeskctlError("element map is stale (>10 min); run observe again")
        e = get_element(eid)
        if e is None:
            raise DeskctlError(f"unknown element id {eid} (not in last observe)")
        _check_observe_window_fresh(obs)
        x, y = e.center()
        label = f'[{eid}] {e.role} "{e.name}"'.rstrip()
        return int(x), int(y), label

    m = re.fullmatch(r"i:\s*(-?\d+)\s*,\s*(-?\d+)", s, re.IGNORECASE)
    if m:
        shot = get_screenshot()
        if not shot or now - float(shot.get("ts", 0)) > SHOT_MAX_AGE:
            raise DeskctlError("screenshot mapping is stale (>10 min); take a new screenshot")
        ix, iy = int(m.group(1)), int(m.group(2))
        x = shot["origin_x"] + ix * shot["scale"]
        y = shot["origin_y"] + iy * shot["scale"]
        return int(round(x)), int(round(y)), f"i:{ix},{iy}"

    m = re.fullmatch(r"(-?\d+)\s*,\s*(-?\d+)\s*$", s)
    if m:
        return int(m.group(1)), int(m.group(2)), f"{m.group(1)},{m.group(2)}"

    raise DeskctlError(
        f'unrecognized target "{spec}" (use element id "12", screen coords "x,y", or image coords "i:x,y")'
    )


# -------------------------------------------------------------- challenge --

CHALLENGE_MAX_AGE = 600.0  # seconds; challenge state older than this is stale


def _finish(st: dict, ch: dict) -> dict:
    ch = dict(ch)
    ch["ts"] = time.time()
    st["challenge"] = ch
    save(st)
    return st["challenge"]


def set_challenge(ch: dict) -> dict:
    """Record a detected challenge. Keeps the existing id when the same
    window still has a fresh challenge (detection runs on every observe);
    otherwise allocates a new id."""
    st = load()
    old = st.get("challenge")
    if (
        old
        and int(old.get("hwnd") or 0) == int(ch.get("hwnd") or 0)
        and time.time() - float(old.get("ts", 0)) <= CHALLENGE_MAX_AGE
    ):
        ch = dict(ch)
        ch["id"] = int(old["id"])
        return _finish(st, ch)
    seq = int(st.get("challenge_seq", 0)) + 1
    st["challenge_seq"] = seq
    ch = dict(ch)
    ch["id"] = seq
    return _finish(st, ch)


def record_challenge(ch: dict) -> dict:
    """Record a challenge() capture - always a fresh id (new grid/image)."""
    st = load()
    seq = int(st.get("challenge_seq", 0)) + 1
    st["challenge_seq"] = seq
    ch = dict(ch)
    ch["id"] = seq
    return _finish(st, ch)


def get_challenge() -> dict | None:
    return load().get("challenge")


def active_challenge() -> dict | None:
    """The challenge state if present and younger than CHALLENGE_MAX_AGE."""
    ch = get_challenge()
    if not ch:
        return None
    if time.time() - float(ch.get("ts", 0)) > CHALLENGE_MAX_AGE:
        return None
    return ch


def clear_challenge() -> None:
    st = load()
    if "challenge" in st:
        del st["challenge"]
        save(st)
