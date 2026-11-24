"""deskctl CLI - same operations as the MCP server, for shell-driven agents.

Exit code 0 on success, 1 on error. Plain text output on stdout; --json
switches to structured JSON ({"ok":true,"result":...} or observe's full
structured dump).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from deskctl.core import DeskctlError
from deskctl.core import api
from deskctl.core import safety
from deskctl.core import state as st
from deskctl.core import worker


def _run(fn, *args, **kwargs):
    return worker.run(fn, *args, **kwargs)


def _emit_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deskctl",
        description="Observe and control the Windows desktop (UIA text first, OCR fallback, screenshots on demand).",
    )
    p.add_argument("--json", action="store_true", help="JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("observe", help="dump the UI as numbered text elements")
    s.add_argument("--window", default="focused", help="'focused' | hwnd | title | 'screen'")
    s.add_argument("--mode", default="auto", choices=["auto", "uia", "ocr", "both"])
    s.add_argument("--max", dest="max_elements", type=int, default=150)
    s.add_argument("--dom", action="store_true", help="browser DOM mode (web content only)")

    s = sub.add_parser("screenshot", help="capture a (downscaled, optionally marked) screenshot")
    s.add_argument("--target", default="focused", help="'focused' | 'screen' | hwnd | title | 'x,y,w,h'")
    s.add_argument("--max-width", type=int, default=1280)
    s.add_argument("--no-marks", action="store_true", help="skip set-of-marks element boxes")
    s.add_argument("--jpeg", action="store_true", help="save as JPEG instead of PNG")
    s.add_argument("--out", default=None, help="output path (default %%LOCALAPPDATA%%\\deskctl\\last.png|jpg)")

    for name in ("click", "double-click", "right-click", "move"):
        s = sub.add_parser(name, help=f"{name.replace('-', ' ')} at TARGET")
        s.add_argument("target", help='element id "12", screen coords "x,y", or image coords "i:x,y"')

    s = sub.add_parser("drag", help="drag FROM target TO target")
    s.add_argument("target")
    s.add_argument("to")

    s = sub.add_parser("scroll", help="scroll AMOUNT notches (+up/-down) [at TARGET]")
    s.add_argument("amount", type=float)
    s.add_argument("target", nargs="?", default=None)
    s.add_argument("--horizontal", action="store_true", help="horizontal wheel (left/right)")

    s = sub.add_parser("type", help="type TEXT (unicode-safe; aborts on user takeover)")
    s.add_argument("text")

    s = sub.add_parser("key", help="press one key combo, e.g. ctrl+s, alt+tab, enter, f5, win+r")
    s.add_argument("combo")

    sub.add_parser("windows", help="list top-level windows")

    s = sub.add_parser("focus", help="focus a window by hwnd or title")
    s.add_argument("window")

    s = sub.add_parser("window", help="minimize|maximize|restore|close|move|resize a window")
    s.add_argument("action", choices=["minimize", "maximize", "restore", "close", "move", "resize"])
    s.add_argument("window", nargs="?", default="focused")
    s.add_argument("--x", type=int, default=None)
    s.add_argument("--y", type=int, default=None)
    s.add_argument("--width", type=int, default=None)
    s.add_argument("--height", type=int, default=None)

    s = sub.add_parser("launch", help="launch a URL, file path, or command")
    s.add_argument("target")

    s = sub.add_parser("wait", help="wait for a condition (changed|text|window|element|gone)")
    s.add_argument("--for", dest="cond", default="changed",
                   choices=["changed", "text", "window", "element", "gone"])
    s.add_argument("--value", default=None, help="substring / title / element spec")
    s.add_argument("--timeout", type=float, default=10.0)
    s.add_argument("--window", default="focused", help="window to observe (text/element/gone/changed)")

    sub.add_parser("pause", help="pause all actions")
    sub.add_parser("resume", help="resume actions")
    sub.add_parser("status", help="show pause/state status")
    sub.add_parser("guard", help="run the hotkey + input-takeover guard in the foreground")

    return p


def _dispatch(args) -> str:
    """Returns the text result (or JSON string for observe --json upstream)."""
    cmd = args.cmd

    if cmd == "observe":
        if getattr(args, "json", False):
            return _run(api.observe_json, args.window, args.mode, args.max_elements, args.dom)
        return _run(api.observe, args.window, args.mode, args.max_elements, args.dom)
    if cmd == "screenshot":
        fmt = "jpeg" if args.jpeg else "png"
        data, info = _run(api.screenshot, args.target, args.max_width, not args.no_marks, fmt)
        out = args.out or str(st.state_dir() / ("last.jpg" if args.jpeg else "last.png"))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(data)
        return f"{info}\nsaved: {out}"
    if cmd in ("click", "double-click", "right-click", "move"):
        return _run(api.act, cmd.replace("-", "_"), args.target)
    if cmd == "drag":
        return _run(api.act, "drag", args.target, None, None, None, args.to)
    if cmd == "scroll":
        return _run(api.act, "scroll", args.target, None, None, args.amount, None,
                    args.horizontal)
    if cmd == "type":
        return _run(api.act, "type", None, args.text)
    if cmd == "key":
        return _run(api.act, "key", None, None, args.combo)
    if cmd == "windows":
        return _run(api.windows)
    if cmd == "focus":
        return _run(api.focus, args.window)
    if cmd == "window":
        return _run(api.window, args.action, args.window, args.x, args.y,
                    args.width, args.height)
    if cmd == "launch":
        return _run(api.launch, args.target)
    if cmd == "wait":
        return _run(api.wait_for, args.cond, args.value, args.timeout, args.window)
    if cmd == "pause":
        return _run(api.pause)
    if cmd == "resume":
        return _run(api.resume)
    if cmd == "status":
        return _run(api.status)
    if cmd == "guard":
        safety.start_hotkey_guard()
        print("deskctl guard active: Ctrl+Alt+Shift+K toggles pause; Ctrl+C to exit", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return ""
    raise DeskctlError(f"unknown command {cmd}")


def main(argv=None) -> int:
    # Element/OCR text can contain arbitrary Unicode; never crash on encode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "guard":
            _dispatch(args)
            return 0
        result = _dispatch(args)
        if args.json:
            if args.cmd == "observe":
                # observe_json already returns a JSON document - print verbatim
                print(result)
            else:
                _emit_json({"ok": True, "result": result})
        else:
            print(result)
        return 0
    except DeskctlError as e:
        if args.json:
            _emit_json({"ok": False, "error": str(e)})
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # unexpected - still exit 1 cleanly
        if args.json:
            _emit_json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        else:
            print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
