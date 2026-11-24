"""deskctl CLI - same operations as the MCP server, for shell-driven agents.

Exit code 0 on success, 1 on error. Plain text output on stdout.
"""

from __future__ import annotations

import argparse
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="deskctl",
        description="Observe and control the Windows desktop (UIA text first, OCR fallback, screenshots on demand).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("observe", help="dump the UI as numbered text elements")
    s.add_argument("--window", default="focused", help="'focused' | hwnd | title substring | 'screen'")
    s.add_argument("--mode", default="auto", choices=["auto", "uia", "ocr", "both"])
    s.add_argument("--max", dest="max_elements", type=int, default=150)

    s = sub.add_parser("screenshot", help="capture a (downscaled, optionally marked) screenshot")
    s.add_argument("--target", default="focused", help="'focused' | 'screen' | hwnd | 'x,y,w,h'")
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

    s = sub.add_parser("type", help="type TEXT (unicode-safe)")
    s.add_argument("text")

    s = sub.add_parser("key", help="press one key combo, e.g. ctrl+s, alt+tab, enter, f5, win+r")
    s.add_argument("combo")

    sub.add_parser("windows", help="list top-level windows")

    s = sub.add_parser("focus", help="focus a window by hwnd or title substring")
    s.add_argument("window")

    s = sub.add_parser("wait", help="wait for a screen change")
    s.add_argument("--timeout", type=float, default=10.0)

    sub.add_parser("pause", help="pause all actions")
    sub.add_parser("resume", help="resume actions")
    sub.add_parser("status", help="show pause/state status")
    sub.add_parser("guard", help="run the Ctrl+Alt+Shift+K pause hotkey in the foreground")

    return p


def _dispatch(args) -> int:
    cmd = args.cmd

    if cmd == "observe":
        print(_run(api.observe, args.window, args.mode, args.max_elements))
    elif cmd == "screenshot":
        fmt = "jpeg" if args.jpeg else "png"
        data, info = _run(api.screenshot, args.target, args.max_width, not args.no_marks, fmt)
        out = args.out or str(st.state_dir() / ("last.jpg" if args.jpeg else "last.png"))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(data)
        print(info)
        print(f"saved: {out}")
    elif cmd in ("click", "double-click", "right-click", "move"):
        print(_run(api.act, cmd.replace("-", "_"), args.target))
    elif cmd == "drag":
        print(_run(api.act, "drag", args.target, None, None, None, args.to))
    elif cmd == "scroll":
        print(_run(api.act, "scroll", args.target, None, None, args.amount, None))
    elif cmd == "type":
        print(_run(api.act, "type", None, args.text))
    elif cmd == "key":
        print(_run(api.act, "key", None, None, args.combo))
    elif cmd == "windows":
        print(_run(api.windows))
    elif cmd == "focus":
        print(_run(api.focus, args.window))
    elif cmd == "wait":
        print(_run(api.wait_for_change, args.timeout, "focused"))
    elif cmd == "pause":
        print(_run(api.pause))
    elif cmd == "resume":
        print(_run(api.resume))
    elif cmd == "status":
        print(_run(api.status))
    elif cmd == "guard":
        safety.start_hotkey_guard()
        print("deskctl guard active: Ctrl+Alt+Shift+K toggles pause; Ctrl+C to exit", flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        raise DeskctlError(f"unknown command {cmd}")
    return 0


def main(argv=None) -> int:
    # Element/OCR text can contain arbitrary Unicode; never crash on encode.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except DeskctlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # unexpected - still exit 1 cleanly
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
