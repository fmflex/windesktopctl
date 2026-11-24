"""deskctl MCP server.

Default: stdio transport (for `devin mcp add`). `--http` serves Streamable
HTTP on 127.0.0.1 and requires DESKCTL_TOKEN (Bearer auth middleware).

CRITICAL for stdio: nothing but the MCP protocol may touch stdout. The
uiautomation package writes an @AutomationLog.txt and can print; we disable
its file log and neuter Logger.Write at startup. Our own diagnostics go to
stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hmac
import os
import sys


def _silence_uiautomation() -> None:
    try:
        import uiautomation as auto

        auto.Logger.SetLogFile("")  # no @AutomationLog.txt

        def _noop(*_a, **_k):
            return None

        auto.Logger.Write = staticmethod(_noop)
    except Exception:
        pass


INSTRUCTIONS = """\
deskctl controls the user's Windows desktop locally (no LLM inside).

Workflow:
- Call observe() FIRST to get a compact text map of on-screen UI elements
  with numeric ids (accessibility tree; falls back to OCR when UIA sees
  little). Prefer acting on element ids over raw coordinates.
- Call screenshot() ONLY when observe() text is not enough. It returns an
  image plus info: image WxH, scale (screen px per image px), origin, and
  ~tokens estimate. To click something visible only in the image, use
  target "i:x,y" with image pixel coords.
- act() performs click / double_click / right_click / move / drag / scroll /
  type / key. Targets: element id "12", screen coords "x,y", image coords
  "i:x,y". All coordinates are physical screen pixels.
- windows() lists top-level windows; focus() raises one; wait_for_change()
  polls until the screen changes.
- Safety: pause() / resume() / status(); hotkey Ctrl+Alt+Shift+K toggles
  pause; slamming the cursor into the top-left screen corner failsafes.
- After acting, observe() again to verify the result instead of assuming.
"""


class BearerAuthMiddleware:
    """Pure ASGI middleware: require `Authorization: Bearer <token>` on every
    HTTP/WebSocket request; 401 otherwise."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = ("Bearer " + token).encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        auth = b""
        for k, v in scope.get("headers") or []:
            if k.lower() == b"authorization":
                auth = v
                break
        if not hmac.compare_digest(auth, self._expected):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def create_server():
    _silence_uiautomation()
    from mcp.server.fastmcp import FastMCP

    from deskctl.core import DeskctlError
    from deskctl.core import api
    from deskctl.core import worker

    mcp = FastMCP("deskctl", instructions=INSTRUCTIONS)

    def _call(fn, *args):
        return worker.submit(fn, *args).result()

    async def _acall(fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(_call, fn, *args))

    def _err(e: DeskctlError) -> str:
        return f"error: {e}"

    @mcp.tool()
    async def observe(window: str = "focused", mode: str = "auto", max_elements: int = 150) -> str:
        """Observe the desktop as compact text: numbered UI elements with
        roles, names, values, and screen-pixel centers. Call this FIRST before
        acting. window: "focused" | hwnd | title substring | "screen".
        mode: auto | uia | ocr | both. max_elements caps the list (rest are
        reported as truncated). Returns element ids usable as act() targets."""
        try:
            return await _acall(api.observe, window, mode, max_elements)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def screenshot(
        target: str = "focused",
        max_width: int = 1280,
        marks: bool = True,
        fmt: str = "png",
    ):
        """Capture a screenshot (downscaled to max_width, optional numbered
        set-of-marks boxes for last observe()'s elements). Returns the image
        plus an info line with scale/origin/~tokens. Use ONLY when observe()
        text is insufficient; then act on image coords via "i:x,y" targets.
        target: "focused" | "screen" | hwnd | "x,y,w,h"; fmt: png | jpeg."""
        from mcp.server.fastmcp import Image

        try:
            data, info = await _acall(api.screenshot, target, max_width, marks, fmt)
        except DeskctlError as e:
            return _err(e)
        return [Image(data=data, format=fmt), info]

    @mcp.tool()
    async def act(
        action: str,
        target: str | None = None,
        text: str | None = None,
        keys: str | None = None,
        amount: float | None = None,
        to: str | None = None,
    ) -> str:
        """Perform a desktop action and report whether the screen changed.
        action: click | double_click | right_click | move | drag | scroll |
        type | key. target: element id "12" | screen coords "x,y" | image
        coords "i:x,y" (from last screenshot). drag also needs `to`.
        scroll needs `amount` (+up/-down notches), optional target.
        type needs `text`; key needs `keys` (one combo, e.g. "ctrl+s",
        "alt+tab", "enter", "f5", "win+r")."""
        try:
            return await _acall(api.act, action, target, text, keys, amount, to)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def windows() -> str:
        """List top-level visible windows: hwnd, title, process, rect,
        [focused] marker. Use with focus(hwnd_or_title)."""
        try:
            return await _acall(api.windows)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def focus(window: str) -> str:
        """Focus a window by hwnd or title substring."""
        try:
            return await _acall(api.focus, window)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def wait_for_change(timeout: float = 10.0, target: str = "focused") -> str:
        """Poll until the captured region changes or timeout expires.
        Returns e.g. 'changed after 1.2s' or 'no change in 10s'.
        target: "focused" | "screen" | hwnd | "x,y,w,h"."""
        try:
            return await _acall(api.wait_for_change, timeout, target)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def pause() -> str:
        """Pause all deskctl actions (also toggled by Ctrl+Alt+Shift+K)."""
        try:
            return await _acall(api.pause)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def resume() -> str:
        """Resume actions after pause()."""
        try:
            return await _acall(api.resume)
        except DeskctlError as e:
            return _err(e)

    @mcp.tool()
    async def status() -> str:
        """Safety/status info: paused state, hotkey, state freshness."""
        try:
            return await _acall(api.status)
        except DeskctlError as e:
            return _err(e)

    return mcp


def main(argv=None) -> int:
    _silence_uiautomation()

    ap = argparse.ArgumentParser(prog="deskctl-mcp")
    ap.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)

    from deskctl.core import safety

    safety.start_hotkey_guard()

    server = create_server()

    if args.http:
        token = os.environ.get("DESKCTL_TOKEN", "").strip()
        if not token:
            print("deskctl-mcp: --http requires DESKCTL_TOKEN env var", file=sys.stderr)
            return 2
        import uvicorn

        app = BearerAuthMiddleware(server.streamable_http_app(), token)
        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    server.run()  # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
