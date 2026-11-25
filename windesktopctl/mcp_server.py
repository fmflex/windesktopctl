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
import concurrent.futures
import functools
import hmac
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

from mcp.server.fastmcp import Context


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
- SHELL/FILE TOOLS (when enabled - see config.toml and the strong-token
  rule for HTTP mode): exec runs powershell (default) or cmd commands -
  a FULL shell, NOT confined to file_roots, with no stdin (pass
  non-interactive flags). background=True returns job_id=jN - poll
  job_output (incremental), job_kill to stop the tree, job_list to list
  (jobs die with the server). File tools only touch absolute paths inside
  config.toml file_roots (junctions/symlinks resolved): read_file (numbered
  lines or base64 range), write_file, edit_file (exact-string replace),
  list_dir, glob, grep (regex, skips .git/.venv/...), file_info (sha256 +
  chunk count), download_chunk/upload_chunk for big files in <=4MB base64
  chunks (upload is resumable via .part offsets). All refuse while paused
  or when disabled in config.toml.
- MCP UPSTREAM TOOLS (config.toml [mcp_upstreams.*]): mcp_servers lists the
  configured upstream MCP servers (reachable/connected/tools/managed pid);
  mcp_tools(server, filter=...) shows an upstream's tools (denied tools are
  hidden unless include_denied); mcp_call(server, tool, args) forwards one
  call and returns text + structuredContent (+image/resource metadata).
  Workflow: mcp_servers -> mcp_tools with a narrow filter -> mcp_call.
  Upstreams named ida* deny py_eval/py_exec_file unless allow_unsafe;
  'ida' needs the IDA GUI open with the database; 'ida_headless' starts
  IDA headless on demand when the GUI isn't running.
- Safety: pause() / resume() / status(); hotkey Ctrl+Alt+Shift+K toggles
  pause; slamming the cursor into the top-left screen corner failsafes;
  physical keyboard/mouse input pauses automation briefly (takeover
  detection) - retry when idle.
- Element ids go stale if the window closes, moves, or resizes - observe()
  again after acting.
- After acting, observe() again to verify the result instead of assuming.
"""


class _IPState:
    """Throttle/log state for one client IP."""

    __slots__ = (
        "fails",
        "banned_until",
        "offenses",
        "offense_until",
        "last_fail_log",
        "last_ok_log",
    )

    def __init__(self) -> None:
        self.fails: list[float] = []
        self.banned_until = 0.0
        self.offenses = 0
        self.offense_until = 0.0
        self.last_fail_log = -float("inf")
        self.last_ok_log = -float("inf")


def _fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    if s >= 3600:
        h = s / 3600
        return f"{int(h)}h" if h == int(h) else f"{h:.1f}h"
    if s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


class BearerAuthMiddleware:
    """Pure ASGI middleware: require `Authorization: Bearer <token>` on every
    HTTP/WebSocket request; 401 otherwise.

    ``token`` is a static string or a callable returning the current token
    (TokenProvider re-reads the token file when its mtime changes, so
    `token --rotate` locks out old clients without a restart).

    Throttle is PER CLIENT IP (``scope["client"][0]``; uvicorn rewrites it
    from X-Forwarded-For only for trusted proxies - ``forwarded_allow_ips``
    stays at the 127.0.0.1 default, so direct clients cannot spoof it):
    more than FAIL_LIMIT failed auths from one IP inside FAIL_WINDOW seconds
    bans THAT IP with 429 for BAN_BASE_SECONDS; each further offense doubles
    the ban up to BAN_MAX_SECONDS (offenses are remembered for
    OFFENSE_MEMORY_SECONDS). Bans apply to the IP, so a banned client
    is refused even with a valid token - but other IPs are unaffected, so a
    scanner cannot lock out the legitimate client. Tracked IPs are capped at
    MAX_TRACKED_IPS; the least-recently-active entry is evicted (an evicted
    ban is simply re-earned by failing again).

    Logging goes to stderr and NEVER includes presented tokens: the first
    auth failure per IP per FAIL_LOG_WINDOW (path/method/UA<=80 chars/Host),
    each new ban once, and the first successful auth per IP per
    AUTH_OK_LOG_WINDOW with the UA.
    """

    FAIL_LIMIT = 10
    FAIL_WINDOW = 60.0
    BAN_BASE_SECONDS = 600.0
    BAN_MAX_SECONDS = 24 * 3600.0
    OFFENSE_MEMORY_SECONDS = 24 * 3600.0
    MAX_TRACKED_IPS = 10_000
    FAIL_LOG_WINDOW = 60.0
    AUTH_OK_LOG_WINDOW = 600.0
    _PRUNE_INTERVAL = 1.0

    def __init__(self, app, token, clock=time.monotonic):
        self.app = app
        if callable(token):
            self._provider = token
        else:
            static = str(token)
            self._provider = lambda: static
        self._clock = clock
        self._ips: "OrderedDict[str, _IPState]" = OrderedDict()
        self._last_prune = -float("inf")

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

    @staticmethod
    def _client_ip(scope) -> str:
        client = scope.get("client")
        return str(client[0]) if client else "?"

    @staticmethod
    def _ua(headers) -> str:
        for k, v in headers or []:
            if k.lower() == b"user-agent":
                return v.decode("utf-8", "replace")[:80]
        return "-"

    def _state(self, ip: str) -> _IPState:
        st = self._ips.get(ip)
        if st is None:
            while len(self._ips) >= self.MAX_TRACKED_IPS:
                self._ips.popitem(last=False)
            st = _IPState()
            self._ips[ip] = st
        else:
            self._ips.move_to_end(ip)
        return st

    def _prune(self, now: float) -> None:
        """Drop stale entries and empty states; bounded by MAX_TRACKED_IPS."""
        if now - self._last_prune < self._PRUNE_INTERVAL:
            return
        self._last_prune = now
        fw = now - self.FAIL_WINDOW
        for st in self._ips.values():
            if st.fails:
                st.fails = [t for t in st.fails if t > fw]
        fl = now - self.FAIL_LOG_WINDOW
        ol = now - self.AUTH_OK_LOG_WINDOW
        for ip in [
            i
            for i, st in self._ips.items()
            if not st.fails
            and st.banned_until <= now
            and st.offense_until <= now
            and st.last_fail_log <= fl
            and st.last_ok_log <= ol
        ]:
            del self._ips[ip]

    def _log_fail(self, ip: str, scope, host: bytes, ua: str) -> None:
        try:
            host_s = host.decode("utf-8", "replace")
        except Exception:
            host_s = "?"
        print(
            f"windesktopctl: auth failure from {ip} "
            f"path={scope.get('path', '?')} method={scope.get('method', '-')} "
            f"host={host_s!r} ua={ua!r}",
            file=sys.stderr,
            flush=True,
        )

    def _log_ban(self, ip: str, seconds: float, failures: int) -> None:
        print(
            f"windesktopctl: banned {ip} for {_fmt_duration(seconds)} "
            f"after {failures} failures",
            file=sys.stderr,
            flush=True,
        )

    def _log_ok(self, ip: str, ua: str) -> None:
        print(
            f"windesktopctl: auth ok from {ip} ua={ua!r}",
            file=sys.stderr,
            flush=True,
        )

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        now = self._clock()
        ip = self._client_ip(scope)
        self._prune(now)
        st = self._state(ip)
        if st.banned_until > now:
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
        if hmac.compare_digest(auth, expected):
            if now - st.last_ok_log >= self.AUTH_OK_LOG_WINDOW:
                st.last_ok_log = now
                self._log_ok(ip, self._ua(scope.get("headers")))
            await self.app(scope, receive, send)
            return
        st.fails = [t for t in st.fails if t > now - self.FAIL_WINDOW]
        st.fails.append(now)
        if len(st.fails) > self.FAIL_LIMIT:
            st.offenses += 1
            st.offense_until = now + self.OFFENSE_MEMORY_SECONDS
            ban = min(
                self.BAN_BASE_SECONDS * (2 ** (st.offenses - 1)),
                self.BAN_MAX_SECONDS,
            )
            st.banned_until = now + ban
            self._log_ban(ip, ban, len(st.fails))
            status = 429
        else:
            if now - st.last_fail_log >= self.FAIL_LOG_WINDOW:
                st.last_fail_log = now
                self._log_fail(ip, scope, host, self._ua(scope.get("headers")))
            status = 401
        body = (
            b'{"error":"too many failed auth attempts"}'
            if status == 429
            else b'{"error":"unauthorized"}'
        )
        await self._respond(send, status, body)
        await send({"type": "http.response.body", "body": body})


def _env_tool_filter() -> set[str] | None:
    """WINDESKTOPCTL_TOOLS: comma-separated allowlist of tool names (None = all)."""
    raw = os.environ.get("WINDESKTOPCTL_TOOLS", "").strip()
    if not raw:
        return None
    return {p.strip() for p in raw.split(",") if p.strip()}


# ------------------------------------------------- shell/file tools (new) --

# Dedicated pool for shell/file tools so a long exec never blocks the
# single COM worker that serves the GUI tools.
_IO_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="windesktopctl-io"
)

_NEW_TOOL_NAMES = {
    "exec",
    "job_output",
    "job_kill",
    "job_list",
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "glob",
    "grep",
    "file_info",
    "download_chunk",
    "upload_chunk",
    "mcp_servers",
    "mcp_tools",
    "mcp_call",
}

_audit_lock = threading.Lock()


def _client_ip(ctx) -> str:
    """Client IP for the audit log: streamable-HTTP request's client host
    (uvicorn already rewrote it from trusted XFF), else 'stdio'."""
    try:
        req = ctx.request_context.request
        client = getattr(req, "client", None)
        if client is not None:
            return str(client.host)
    except Exception:
        pass
    return "stdio"


def _audit(ctx, tool: str, **fields) -> None:
    """Append one JSONL line to <state_dir>/audit.log. Never logs file
    contents, env values, or tokens; audit failures never break a tool."""
    try:
        from windesktopctl.core import state as st

        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "client": _client_ip(ctx),
            "tool": tool,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        p = st.state_dir() / "audit.log"
        with _audit_lock:
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def _token_strong(provider) -> bool:
    """HTTP-mode gate for shell/file tools: token must be >=32 chars with
    >=16 distinct characters (a urlsafe-32 token passes; 'password' doesn't)."""
    try:
        tok = str(provider() or "")
    except Exception:
        return False
    return len(tok) >= 32 and len(set(tok)) >= 16


def _num(text: str, pattern: str):
    m = re.search(pattern, text or "")
    return m.group(1) if m else None


def create_server(tool_names: set[str] | None = None, allowed_hosts: list[str] | None = None,
                  token_provider=None):
    _silence_uiautomation()
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    from windesktopctl.core import WindesktopctlError
    from windesktopctl.core import api
    from windesktopctl.core import files as files_core
    from windesktopctl.core import shell as shell_core
    from windesktopctl.core import worker

    ts = None
    if allowed_hosts is not None:
        ts = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(allowed_hosts),
        )

    mcp = FastMCP(
        "windesktopctl",
        instructions=INSTRUCTIONS,
        transport_security=ts,
        # upload/download chunks are <=4MB raw -> ~5.6MB base64 inside the
        # JSON-RPC body; the 4MiB default would 413 them.
        max_request_body_size=8 * 1024 * 1024,
    )

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

    # ------------------------------------------------ shell / file tools --
    # Run on _IO_POOL (not the COM worker) so a long exec can't block GUI
    # tools. Each refuses while paused or when disabled in config.toml.

    async def _aiocall(fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_IO_POOL, functools.partial(fn, *args))

    async def exec(
        command: str,
        shell: str = "powershell",
        cwd: str | None = None,
        timeout_s: int = 120,
        env: dict | None = None,
        background: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Run a shell command on this PC - powershell (default) or cmd.
        FULL shell access, NOT confined to file_roots; cwd is likewise
        unrestricted and defaults to the user's home directory when
        omitted. There is NO stdin: pass non-interactive flags
        (-NonInteractive is already set for powershell). timeout_s is
        clamped to 1..1800 (default 120); on timeout the whole process tree
        is killed. env merges over the process environment. background=True
        returns job_id=jN immediately - poll job_output for incremental
        output, job_kill to stop the tree, job_list to list (jobs do NOT
        survive a server restart). Refuses while paused or when
        shell_enabled=false in config.toml."""
        try:
            res = await _aiocall(
                shell_core.exec_cmd, command, shell, cwd, timeout_s, env, background
            )
        except WindesktopctlError as e:
            return _err(e)
        meta = {"command": str(command)[:500], "shell": shell}
        ec = _num(res, r"exit_code=(-?\d+)")
        if ec is not None:
            meta["exit_code"] = int(ec)
        jid = _num(res, r"job_id=(j\d+)")
        if jid:
            meta["job_id"] = jid
        _audit(ctx, "exec", **meta)
        return res

    async def job_output(
        job_id: str, max_bytes: int = 65536, ctx: Context | None = None
    ) -> str:
        """New output since your last job_output call for this job (the
        server keeps a read cursor per stream), plus running/exited status
        and elapsed time. max_bytes caps the total returned (default 64KB,
        max 4MB)."""
        try:
            return await _aiocall(shell_core.job_output, job_id, max_bytes)
        except WindesktopctlError as e:
            return _err(e)

    async def job_kill(job_id: str, ctx: Context | None = None) -> str:
        """Kill a background job's whole process tree; returns the final
        status (exited code=N)."""
        try:
            res = await _aiocall(shell_core.job_kill, job_id)
        except WindesktopctlError as e:
            return _err(e)
        _audit(ctx, "job_kill", job_id=job_id, result=res[:200])
        return res

    async def job_list(ctx: Context | None = None) -> str:
        """List known background jobs: id, status, started time, command
        (truncated). Jobs are in-memory only - a server restart forgets them."""
        try:
            return await _aiocall(shell_core.job_list)
        except WindesktopctlError as e:
            return _err(e)

    async def read_file(
        path: str,
        offset: int = 1,
        limit: int = 2000,
        numbered: bool = True,
        mode: str = "text",
        byte_offset: int = 0,
        byte_length: int | None = None,
        ctx: Context | None = None,
    ) -> str:
        """Read a file inside config.toml file_roots (absolute path only;
        junctions/symlinks resolved - escapes are refused). Text mode
        (default): numbered 'N\\tline' output, offset is 1-based, capped at
        ~256KB - use offset/limit for more. mode='base64' returns a JSON
        byte range {offset,length,eof,size,data_b64} (<=1MB raw per call)."""
        try:
            res = await _aiocall(
                files_core.read_file, path, offset, limit, numbered,
                mode, byte_offset, byte_length,
            )
        except WindesktopctlError as e:
            return _err(e)
        _audit(ctx, "read_file", path=str(path))
        return res

    async def write_file(
        path: str,
        text: str | None = None,
        content_b64: str | None = None,
        append: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Write a file inside file_roots (parent dirs are created).
        Exactly one of text= (utf-8) or content_b64= (binary). Non-append
        writes are atomic (temp file + rename); append=true adds bytes."""
        try:
            res = await _aiocall(
                files_core.write_file, path, text, content_b64, append
            )
        except WindesktopctlError as e:
            return _err(e)
        _audit(
            ctx, "write_file", path=str(path), append=bool(append),
            bytes_written=_num(res, r"wrote (\d+)"),
        )
        return res

    async def edit_file(
        path: str,
        old: str,
        new: str,
        replace_all: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Exact-string replace in a utf-8 file inside file_roots. `old`
        must occur exactly once unless replace_all=true (error lists the
        count + first line numbers). On CRLF files a bare-LF old/new is
        retried normalized to CRLF. Non-utf8 files are refused (use
        write_file/content_b64). Returns count + line numbers."""
        try:
            res = await _aiocall(files_core.edit_file, path, old, new, replace_all)
        except WindesktopctlError as e:
            return _err(e)
        _audit(
            ctx, "edit_file", path=str(path), replace_all=bool(replace_all),
            replacements=_num(res, r"replaced (\d+)"),
        )
        return res

    async def list_dir(
        path: str, max_entries: int = 1000, ctx: Context | None = None
    ) -> str:
        """List a directory inside file_roots: 'd|f  size  mtime  name',
        directories first, truncated at max_entries (default 1000)."""
        try:
            return await _aiocall(files_core.list_dir, path, max_entries)
        except WindesktopctlError as e:
            return _err(e)

    async def glob(
        pattern: str, path: str, max_results: int = 1000,
        ctx: Context | None = None,
    ) -> str:
        """pathlib-style glob (supports '**') under a root-checked directory.
        Skips .git/node_modules/__pycache__/.venv unless the pattern names
        them; matches escaping the roots via junctions are dropped. Sorted,
        truncated at max_results."""
        try:
            return await _aiocall(files_core.glob_files, pattern, path, max_results)
        except WindesktopctlError as e:
            return _err(e)

    async def grep(
        pattern: str,
        path: str,
        glob: str | None = None,
        max_results: int = 200,
        case_insensitive: bool = False,
        context: int = 0,
        ctx: Context | None = None,
    ) -> str:
        """Regex search over files under a root-checked directory:
        'path:line: text' for matches ('path-line- text' for context
        lines). Skips binary files (NUL in first 8KB), files >20MB, and
        .git/node_modules/__pycache__/.venv dirs; `glob` filters file paths
        (pathlib match syntax). 30s budget -> partial results + note."""
        try:
            return await _aiocall(
                files_core.grep_files, pattern, path, glob, max_results,
                case_insensitive, context,
            )
        except WindesktopctlError as e:
            return _err(e)

    async def file_info(path: str, ctx: Context | None = None) -> str:
        """size, mtime, sha256 (streamed) and 4MB-chunk count for a file
        inside file_roots - call before download_chunk on big files."""
        try:
            return await _aiocall(files_core.file_info, path)
        except WindesktopctlError as e:
            return _err(e)

    async def download_chunk(
        path: str,
        offset: int = 0,
        length: int = 4194304,
        ctx: Context | None = None,
    ) -> str:
        """Fetch one <=4MB base64 chunk of a file inside file_roots as JSON
        {path,offset,length,eof,size,data_b64}. Reassemble chunks at their
        offsets to pull big files through the agent."""
        try:
            res = await _aiocall(files_core.download_chunk, path, offset, length)
        except WindesktopctlError as e:
            return _err(e)
        _audit(ctx, "download_chunk", path=str(path))
        return res

    async def upload_chunk(
        path: str,
        offset: int,
        data_b64: str,
        done: bool = False,
        sha256: str | None = None,
        overwrite: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """Upload a file in <=4MB base64 chunks (resumable). offset must
        equal the current <path>.part size - 0 restarts, anything else is
        an error telling you the expected offset. done=true verifies
        sha256 when given, refuses to clobber an existing file unless
        overwrite=true, then renames .part into place."""
        try:
            res = await _aiocall(
                files_core.upload_chunk, path, offset, data_b64,
                done, sha256, overwrite,
            )
        except WindesktopctlError as e:
            return _err(e)
        if done or int(offset) == 0:
            _audit(
                ctx, "upload_chunk", path=str(path), offset=int(offset),
                done=bool(done), bytes=_num(res, r"(\d+) bytes"),
            )
        return res

    # --------------------------------------------- MCP upstream passthrough --

    async def mcp_servers(
        stop: str | None = None, ctx: Context | None = None
    ) -> str:
        """List the MCP upstream servers configured in config.toml
        ([mcp_upstreams.*]): name, kind (http/stdio), enabled, reachable,
        connected, cached tool count, managed headless pid/status, last
        error, denied tools. stop=<name> gracefully stops that upstream's
        managed headless process (CTRL_C/SIGINT -> clean DB close, up to
        60s, then kill) - only for upstreams with start_command."""
        from windesktopctl.core import safety as _safety
        from windesktopctl.core import upstreams as ups

        _safety.check_paused()
        try:
            if stop:
                res = await _aiocall(ups.stop_server, stop)
                _audit(ctx, "mcp_servers", server=str(stop), result=res[:200])
                return res
            return await _aiocall(ups.servers_status)
        except WindesktopctlError as e:
            return _err(e)

    async def mcp_tools(
        server: str,
        filter: str | None = None,
        include_denied: bool = False,
        refresh: bool = False,
        ctx: Context | None = None,
    ) -> str:
        """List an upstream MCP server's tools: name, description (<=300
        chars), inputSchema. filter = case-insensitive substring on name or
        description. Denied tools (unsafe/deny_tools, allow_unsafe=false)
        are hidden unless include_denied=true (tagged [denied]). refresh=true
        reconnects and re-lists. Output caps ~64KB - narrow the filter.
        Refuses while paused or when the upstream is disabled."""
        from windesktopctl.core import safety as _safety
        from windesktopctl.core import upstreams as ups

        _safety.check_paused()
        try:
            return await _aiocall(
                ups.tools_list, server, filter, include_denied, refresh
            )
        except WindesktopctlError as e:
            return _err(e)

    async def mcp_call(
        server: str,
        tool: str,
        args: dict | str | None = None,
        timeout_s: int = 120,
        ctx: Context | None = None,
    ) -> str:
        """Forward one tool call to an upstream MCP server and return the
        result: text content, structuredContent as compact JSON, images as
        metadata + a saved copy under <state_dir>/upstreams/, embedded
        resources as text (or metadata if binary). isError results are
        prefixed 'upstream error:'; JSON-RPC/transport errors pass through
        verbatim. args is a JSON object (a JSON string is also accepted).
        timeout_s clamped to 1..1800. Denied tools and disabled upstreams
        are refused; a failed call marks the session for reconnect on the
        NEXT call (the failed call itself is never re-sent)."""
        from windesktopctl.core import safety as _safety
        from windesktopctl.core import upstreams as ups

        _safety.check_paused()
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return "error: args must be a JSON object or JSON object string"
        if args is not None and not isinstance(args, dict):
            return "error: args must be a JSON object"
        timeout_s = max(1, min(int(timeout_s), 1800))
        try:
            arg_bytes = len(json.dumps(args if args is not None else {}, default=str))
        except Exception:
            arg_bytes = -1
        t0 = time.monotonic()
        try:
            text, meta = await _aiocall(
                ups.call_tool, server, tool, args, timeout_s
            )
        except WindesktopctlError as e:
            _audit(ctx, "mcp_call", server=str(server),
                   upstream_tool=str(tool), arg_bytes=arg_bytes,
                   is_error=True, duration_s=round(time.monotonic() - t0, 2))
            return _err(e)
        _audit(ctx, "mcp_call", **meta)
        return text

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
        "exec": exec,
        "job_output": job_output,
        "job_kill": job_kill,
        "job_list": job_list,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "list_dir": list_dir,
        "glob": glob,
        "grep": grep,
        "file_info": file_info,
        "download_chunk": download_chunk,
        "upload_chunk": upload_chunk,
        "mcp_servers": mcp_servers,
        "mcp_tools": mcp_tools,
        "mcp_call": mcp_call,
    }

    # HTTP mode requires a strong bearer token before shell/file tools are
    # exposed; stdio (local Devin) always registers them - config.toml and
    # pause still gate every call.
    register_new = True
    if allowed_hosts is not None:
        register_new = token_provider is not None and _token_strong(token_provider)
        if not register_new:
            print(
                "windesktopctl: shell/file tools NOT enabled: token too weak "
                "(<32 random chars)",
                file=sys.stderr,
                flush=True,
            )

    allow = tool_names if tool_names is not None else _env_tool_filter()
    known = set(tool_fns)
    for name, fn in tool_fns.items():
        if allow is not None and name not in allow:
            continue
        if name in _NEW_TOOL_NAMES and not register_new:
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
    server = create_server(
        allowed_hosts=allowed_hosts, token_provider=token_provider
    )
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

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
        return 0

    server = create_server(allowed_hosts=allowed_hosts)
    server.run()  # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
