"""windesktopctl MCP server.

Default: stdio transport (for `devin mcp add`). `--http` serves Streamable
HTTP on 127.0.0.1 and requires WINDESKTOPCTL_TOKEN (Bearer auth middleware) plus
Host-header validation against DNS rebinding (--allowed-host extends the
allowlist for tunnel hostnames).

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
import time


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
windesktopctl controls the user's Windows desktop locally (no LLM inside).

Workflow:
- Call observe() FIRST to get a compact text map of on-screen UI elements
  with numeric ids (accessibility tree; OCR fallback when UIA sees little).
  Prefer acting on element ids over raw coordinates. Element lines show
  state flags (focused/disabled/checked/expanded/collapsed/selected/
  password) and scroll containers appear as role "Scroll" with v=/h= percent.
- Password fields never expose their value. Password-manager processes
  (1Password, KeePass, Bitwarden, Enpass + WINDESKTOPCTL_EXCLUDE) are never
  observed; their rects are blacked out in screen captures.
- Call observe(dom=True) for browser pages (chrome/msedge/brave/vivaldi/
  opera/firefox) to get web-content elements instead of browser chrome.
- Call screenshot() ONLY when observe() text is not enough. It returns an
  image plus info: image WxH, scale (screen px per image px), origin, and
  ~tokens estimate. To click something visible only in the image, use
  target "i:x,y" with image pixel coords.
- act() performs click / double_click / right_click / move / drag / scroll /
  type / key. Targets: element id "12", screen coords "x,y", image coords
  "i:x,y". scroll takes amount (+up/-down, or --horizontal) and drag takes
  `to`. type aborts mid-way if you take over the PC.
- wait_for() polls a condition: changed | text | window | element | gone.
- windows() lists top-level windows; focus() raises one; window() does
  minimize/maximize/restore/close/move/resize; launch() opens a URL, file,
  or command and reports the new window.
- browser() drives Chromium windows: info (title+URL+tabs), tabs, tab
  (select by index/title), text (whole page incl. offscreen, no scrolling),
  navigate (URL).
- HUMAN CHALLENGES (CAPTCHA / "verify you are human"): windesktopctl NEVER solves
  or guesses them - it cannot verify an answer really came from the user;
  that is an agent-instruction contract. When observe() prints "!! challenge
  detected", STOP acting on that page: call challenge(), show the returned
  image to the user, wait for their reply, and pass it VERBATIM to
  challenge_answer() (tile numbers like "3 8 14" on numbered grids, or
  lettered cells like "B3 C4"; text, or both). act()/window()/
  browser tab|navigate refuse on challenge pages until it clears; only
  challenge_answer() may input there. If you want per-answer human approval,
  set a Devin permission "ask" rule on challenge_answer.
- Safety: pause() / resume() / status(); hotkey Ctrl+Alt+Shift+K toggles
  pause; slamming the cursor into the top-left screen corner failsafes;
  physical keyboard/mouse input pauses automation briefly (takeover
  detection) - retry when idle.
- Element ids go stale if the window closes, moves, or resizes - observe()
  again after acting.
- After acting, observe() again to verify the result instead of assuming.
"""


class BearerAuthMiddleware:
    """Pure ASGI middleware: require `Authorization: Bearer <token>` on every
    HTTP/WebSocket request; 401 otherwise.

    ``token`` is a static string or a callable returning the current token
    (TokenProvider re-reads the token file when its mtime changes, so
    `token --rotate` locks out old clients without a restart).

    Throttle: more than FAIL_LIMIT failed auths inside FAIL_WINDOW seconds
    bans ALL requests with 429 for BAN_SECONDS. Each failure is logged to
    stderr with client address + Host header - never the presented token.
    """

    FAIL_LIMIT = 10
    FAIL_WINDOW = 60.0
    BAN_SECONDS = 60.0

    def __init__(self, app, token):
        self.app = app
        if callable(token):
            self._provider = token
        else:
            static = str(token)
            self._provider = lambda: static
        self._failures: list[float] = []
        self._banned_until = 0.0

    def _respond(self, send, status: int, body: bytes):
        return send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        if now < self._banned_until:
            body = b'{"error":"too many failed auth attempts"}'
            await self._respond(send, 429, body)
            await send({"type": "http.response.body", "body": body})
            return
        expected = ("Bearer " + self._provider()).encode("utf-8")
        auth = b""
        host = b""
        for k, v in scope.get("headers") or []:
            kl = k.lower()
            if kl == b"authorization":
                auth = v
            elif kl == b"host":
                host = v
        if not hmac.compare_digest(auth, expected):
            self._failures.append(now)
            cutoff = now - self.FAIL_WINDOW
            self._failures = [t for t in self._failures if t > cutoff]
            banned = False
            if len(self._failures) > self.FAIL_LIMIT:
                self._banned_until = now + self.BAN_SECONDS
                banned = True
            client = scope.get("client")
            addr = f"{client[0]}:{client[1]}" if client else "?"
            try:
                host_s = host.decode("utf-8", "replace")
            except Exception:
                host_s = "?"
            print(
                f"windesktopctl: auth failure from {addr} host={host_s!r}",
                file=sys.stderr,
                flush=True,
            )
            if banned:
                body = b'{"error":"too many failed auth attempts"}'
                await self._respond(send, 429, body)
            else:
                body = b'{"error":"unauthorized"}'
                await self._respond(send, 401, body)
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def _env_tool_filter() -> set[str] | None:
    """WINDESKTOPCTL_TOOLS: comma-separated allowlist of tool names (None = all)."""
    raw = os.environ.get("WINDESKTOPCTL_TOOLS", "").strip()
    if not raw:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


def create_server(tool_names: set[str] | None = None, allowed_hosts: list[str] | None = None):
    _silence_uiautomation()
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    from windesktopctl.core import WindesktopctlError
    from windesktopctl.core import api
    from windesktopctl.core import worker

    ts = None
    if allowed_hosts is not None:
        ts = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        )

    mcp = FastMCP("windesktopctl", instructions=INSTRUCTIONS, transport_security=ts)

    def _call(fn, *args):
        return worker.submit(fn, *args).result()

    async def _acall(fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(_call, fn, *args))

    def _err(e: WindesktopctlError) -> str:
        return f"error: {e}"

    async def observe(
        window: str = "focused",
        mode: str = "auto",
        max_elements: int = 150,
        dom: bool = False,
    ) -> str:
        """Observe the desktop as compact text: numbered UI elements with
        roles, names, values, state flags, and screen-pixel centers. Call this
        FIRST before acting. window: "focused" | hwnd | title | "screen".
        mode: auto | uia | ocr | both. max_elements caps the list (rest are
        reported as truncated). dom=True observes only web content in
        supported browsers. Returns element ids usable as act() targets."""
        try:
            return await _acall(api.observe, window, mode, max_elements, dom)
        except WindesktopctlError as e:
            return _err(e)

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
        target: "focused" | "screen" | hwnd | title | "x,y,w,h";
        fmt: png | jpeg."""
        from mcp.server.fastmcp import Image

        try:
            data, info = await _acall(api.screenshot, target, max_width, marks, fmt)
        except WindesktopctlError as e:
            return _err(e)
        return [Image(data=data, format=fmt), info]

    async def act(
        action: str,
        target: str | None = None,
        text: str | None = None,
        keys: str | None = None,
        amount: float | None = None,
        to: str | None = None,
        horizontal: bool = False,
    ) -> str:
        """Perform a desktop action and report whether the screen changed.
        action: click | double_click | right_click | move | drag | scroll |
        type | key. target: element id "12" | screen coords "x,y" | image
        coords "i:x,y" (from last screenshot). drag also needs `to`.
        scroll needs `amount` (+up/-down notches; horizontal=True for
        left/right), optional target. type needs `text` (aborts if the user
        takes over); key needs `keys` (one combo, e.g. "ctrl+s",
        "alt+tab", "enter", "f5", "win+r")."""
        try:
            return await _acall(
                api.act, action, target, text, keys, amount, to, horizontal
            )
        except WindesktopctlError as e:
            return _err(e)

    async def windows() -> str:
        """List top-level visible windows: hwnd, title, process, rect,
        [focused] marker. Use with focus(hwnd_or_title)."""
        try:
            return await _acall(api.windows)
        except WindesktopctlError as e:
            return _err(e)

    async def focus(window: str) -> str:
        """Focus a window by hwnd or title (exact match wins, else unique
        substring)."""
        try:
            return await _acall(api.focus, window)
        except WindesktopctlError as e:
            return _err(e)

    async def wait_for(
        cond: str = "changed",
        value: str | None = None,
        timeout: float = 10.0,
        window: str = "focused",
    ) -> str:
        """Poll until a condition is met (~300ms). cond: changed (screen
        diff), text (substring in element name/value), window (foreground
        title contains value), element ("Role:name-sub" or "name-sub"),
        gone (text/element no longer present). text/element success refreshes
        the element map and returns matching lines. Timeout is not an error:
        "timeout after Ns: <cond> not met"."""
        try:
            return await _acall(api.wait_for, cond, value, timeout, window)
        except WindesktopctlError as e:
            return _err(e)

    async def window(
        action: str,
        window: str = "focused",
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> str:
        """Window management: minimize | maximize | restore | close (WM_CLOSE)
        | move (needs x,y) | resize (needs width,height). Subject to the same
        safety gate as act()."""
        try:
            return await _acall(api.window, action, window, x, y, width, height)
        except WindesktopctlError as e:
            return _err(e)

    async def launch(target: str) -> str:
        """Launch a URL, existing file path, or command (shlex-split, no
        shell). Waits up to 10s for a new top-level window and returns its
        windows() line, or "launched; no new window detected"."""
        try:
            return await _acall(api.launch, target)
        except WindesktopctlError as e:
            return _err(e)

    async def browser(
        action: str,
        value: str | None = None,
        window: str = "focused",
        max_chars: int = 4000,
    ) -> str:
        """Browser helpers for Chromium windows (firefox best-effort).
        action: info | tabs | tab | text | navigate. info returns title,
        current URL (omnibox value) and the tab list ('[tN] "Title"
        selected'); tabs returns just the tab list; tab selects a tab -
        value is 't2'/'2' or a title substring (exact > unique > error);
        text returns readable page text from the WHOLE page including
        offscreen content (headings '# ', links '[...]', dupes collapsed,
        truncated at max_chars) - no scrolling needed; navigate takes a
        URL, focuses the window, ctrl+l, types it, presses enter, waits for
        the change and returns fresh info. tab/navigate are gated like act()."""
        try:
            return await _acall(api.browser, action, value, window, max_chars)
        except WindesktopctlError as e:
            return _err(e)

    async def challenge(
        window: str = "focused",
        rows: int | None = None,
        cols: int | None = None,
        region: str | None = None,
        grid: str | None = None,
    ):
        """Capture a human-verification challenge (CAPTCHA / 'verify you are
        human') as an image with a labeled grid overlay. Re-detects on a
        fresh observe; if nothing is detected it still captures the content
        area (kind=unknown). Returns the PNG plus an info line with the
        challenge id.

        Grid kinds with a regular tile lattice auto-switch to NUMBERED mode
        (cells 1..N, user answers "3 8 14"). Otherwise cells are lettered
        (A1-style). region="C4:I10" re-captures a sub-area of the PREVIOUS
        capture (zoom into tiles - choosing region/grid is layout work and
        is allowed). grid="4x4" forces an exact numbered RxC grid over the
        region or current rect. YOU MUST: show this image to the user, wait
        for their reply, and pass it verbatim to challenge_answer().
        windesktopctl NEVER solves challenges itself."""
        from mcp.server.fastmcp import Image

        try:
            data, info = await _acall(
                api.challenge, window, rows, cols, region, grid
            )
        except WindesktopctlError as e:
            return _err(e)
        return [Image(data=data, format="png"), info]

    async def challenge_answer(
        id: int,
        cells: str | None = None,
        text: str | None = None,
        submit: bool = True,
        enter: bool = False,
    ) -> str:
        """Relay the user's EXACT answer to the active challenge - the ONLY
        input path allowed on challenge pages (act/window/browser tab|
        navigate refuse until it clears). id must match the last challenge()
        call. cells: grid cells to click in order, e.g. "B3 C4" or "b3,c4"
        (with text, the FIRST cell is the input box); text: characters to
        type (into the first cell, or the Edit inside the challenge area
        when no cells are given); submit clicks a verify/next/submit button
        when present; enter presses Enter after typing. The answer text is
        never logged. Pass the user's reply VERBATIM - never answer
        challenges yourself."""
        try:
            return await _acall(api.challenge_answer, id, cells, text, submit, enter)
        except WindesktopctlError as e:
            return _err(e)

    async def pause() -> str:
        """Pause all windesktopctl actions (also toggled by Ctrl+Alt+Shift+K)."""
        try:
            return await _acall(api.pause)
        except WindesktopctlError as e:
            return _err(e)

    async def resume() -> str:
        """Resume actions after pause()."""
        try:
            return await _acall(api.resume)
        except WindesktopctlError as e:
            return _err(e)

    async def status() -> str:
        """Safety/status info: paused state, hotkey, takeover detection,
        state freshness."""
        try:
            return await _acall(api.status)
        except WindesktopctlError as e:
            return _err(e)

    tool_fns = {
        "observe": observe,
        "screenshot": screenshot,
        "act": act,
        "windows": windows,
        "focus": focus,
        "wait_for": wait_for,
        "window": window,
        "launch": launch,
        "browser": browser,
        "challenge": challenge,
        "challenge_answer": challenge_answer,
        "pause": pause,
        "resume": resume,
        "status": status,
    }

    allow = tool_names if tool_names is not None else _env_tool_filter()
    known = set(tool_fns)
    for name, fn in tool_fns.items():
        if allow is not None and name not in allow:
            continue
        mcp.tool()(fn)
    if allow is not None:
        unknown = allow - known
        if unknown:
            print(
                f"windesktopctl-mcp: WINDESKTOPCTL_TOOLS names unknown tools, ignored: {sorted(unknown)}",
                file=sys.stderr,
                flush=True,
            )

    return mcp


def _http_allowed_hosts(host: str, extra: list[str]) -> list[str]:
    """Host-header allowlist: 127.0.0.1, localhost, the bind host - each with
    and without a port wildcard - plus any --allowed-host entries."""
    bases = ["127.0.0.1", "localhost"]
    if host and host not in bases:
        bases.append(host)
    out: list[str] = []
    for b in bases:
        for entry in (b, f"{b}:*"):
            if entry not in out:
                out.append(entry)
    for e in extra:
        if e not in out:
            out.append(e)
    return out


def build_http_app(allowed_hosts: list[str] | None, token_provider) -> object:
    """FastMCP streamable-HTTP app wrapped in bearer auth. Shared by
    `windesktopctl-mcp --http` and `windesktopctl serve`."""
    server = create_server(allowed_hosts=allowed_hosts)
    return BearerAuthMiddleware(server.streamable_http_app(), token_provider)


def main(argv=None) -> int:
    _silence_uiautomation()

    ap = argparse.ArgumentParser(prog="windesktopctl-mcp")
    ap.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        metavar="HOST[:PORT|*]",
        help="extra allowed Host header (repeatable; for tunnel hostnames)",
    )
    args = ap.parse_args(argv)

    from windesktopctl.core import safety
    from windesktopctl.core import token as tok

    safety.start_hotkey_guard()

    allowed_hosts = _http_allowed_hosts(args.host, args.allowed_host) if args.http else None

    if args.http:
        app = build_http_app(allowed_hosts, tok.TokenProvider())
        import uvicorn

        uvicorn.run(app, host=args.host, port=args.port)
        return 0

    server = create_server(allowed_hosts=allowed_hosts)
    server.run()  # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
