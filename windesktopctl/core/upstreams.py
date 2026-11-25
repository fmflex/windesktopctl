"""MCP upstream passthrough: forward tool calls to local MCP servers.

Upstreams are declared in ``config.toml`` under ``[mcp_upstreams.<name>]``
(see ``config.mcp_upstreams``): streamable-HTTP ``url`` entries, stdio
``command`` entries, optional ``headers``/``env``/``cwd``, a deny list
(``deny_tools`` + built-in unsafe names unless ``allow_unsafe``), and
optional headless autostart (``start_command`` + ``start_if_unreachable``).

All upstream I/O runs on ONE dedicated asyncio loop thread; sync callers
(including the MCP tool threads) submit coroutines via
``run_coroutine_threadsafe``. Sessions are lazy, persistent, and rebuilt
after transport failures - a failed call is never re-sent (tools may not
be idempotent).

Managed headless processes (``start_command``) get a graceful stop on
shutdown / ``mcp_servers(stop=...)``: CTRL_C_EVENT -> SIGINT (the handler
idalib_server installs), then CTRL_BREAK_EVENT, up to 60 s, then
``taskkill /T /F`` as a last resort - never a hard kill first. Console
control events only reach children that share our console, so managed
processes are spawned without CREATE_NO_WINDOW when we have one; a
console-less parent (service/pythonw) degrades to the timed kill.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from contextlib import AsyncExitStack
from urllib.parse import urlparse

from . import WindesktopctlError
from . import config as cfg
from . import safety
from . import state as st

CONNECT_RETRIES = 2          # initial attempt + one retry
CONNECT_RETRY_DELAY_S = 0.4
MANAGED_STOP_GRACE_S = 60.0
RESULT_MAX_BYTES = 256 * 1024
TOOLS_MAX_BYTES = 64 * 1024
DESC_MAX_CHARS = 300
STOP_POLL_S = 2.0

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def _has_console() -> bool:
    """True when this process has a console window (serve launched from a
    terminal). Managed children then share it so targeted CTRL_C/CTRL_BREAK
    events can reach them for graceful stops; without a console, NO_WINDOW
    keeps them from popping a visible console on the desktop."""
    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def _spawn_flags() -> int:
    flags = CREATE_NEW_PROCESS_GROUP
    if not _has_console():
        flags |= CREATE_NO_WINDOW
    return flags


class UpstreamCallError(Exception):
    """A call reached the upstream but failed (JSON-RPC/transport);
    message is passed through verbatim."""


# ------------------------------------------------------------- loop thread --

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or not _loop.is_running():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=_thread_main, args=(loop,),
                name="windesktopctl-upstreams", daemon=True,
            )
            thread.start()
            _loop = loop
        return _loop


def _thread_main(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _run(coro, timeout: float):
    """Submit a coroutine to the upstream loop; return its result."""
    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout)


# ------------------------------------------------------------------ state --


class _Session:
    __slots__ = ("stack", "client", "tools", "broken", "last_error")

    def __init__(self, stack, client, tools):
        self.stack = stack
        self.client = client
        self.tools = tools            # list of tool-schema dicts
        self.broken = False
        self.last_error = None


_sessions: dict[str, _Session] = {}          # loop-thread only
_last_error: dict[str, str] = {}              # loop-thread only


class _Managed:
    __slots__ = ("name", "proc", "pid", "started", "log_path", "cfg")

    def __init__(self, name, proc, log_path, upstream_cfg):
        self.name = name
        self.proc = proc
        self.pid = proc.pid
        self.started = time.time()
        self.log_path = log_path
        self.cfg = upstream_cfg


_managed: dict[str, _Managed] = {}
_managed_lock = threading.Lock()


def _upstream_cfg(name: str) -> dict:
    c = cfg.mcp_upstreams().get(str(name))
    if c is None:
        raise WindesktopctlError(f"unknown MCP upstream {name!r}")
    return c


def _require_enabled(c: dict) -> None:
    if not c["enabled"]:
        raise WindesktopctlError(f"MCP upstream {c['name']!r} is disabled in config.toml")


# ------------------------------------------------------------- connectivity --


async def _tcp_reachable(url: str, timeout: float = 1.0) -> bool:
    try:
        p = urlparse(url)
        host = p.hostname or "127.0.0.1"
        port = p.port or (443 if p.scheme == "https" else 80)
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _reachable(c: dict) -> bool:
    """Probe: TCP connect for HTTP upstreams; command lookup for stdio."""
    if c["kind"] == "http":
        return await _tcp_reachable(c["url"])
    cmd0 = c["command"][0] if c["command"] else ""
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: bool(cmd0) and (os.path.isfile(cmd0) or shutil.which(cmd0) is not None)
    )


# ------------------------------------------------------------ session mgmt --


async def _connect(c: dict) -> _Session:
    """Open transport + initialize + list_tools. Raises on failure."""
    stack = AsyncExitStack()
    try:
        transport = c.get("transport") or "streamable-http"
        if c["kind"] == "http" and transport == "jsonrpc-http":
            import httpx

            http = await stack.enter_async_context(
                httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))
            )
            client = _JsonRpcClient(http, c["url"], c["headers"])
        elif c["kind"] == "http":
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(c["url"], headers=c["headers"] or None)
            )
            client = await stack.enter_async_context(ClientSession(read, write))
        else:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            env = dict(os.environ)
            env.update(c["env"])
            params = StdioServerParameters(
                command=c["command"][0],
                args=list(c["command"][1:]),
                env=env,
                cwd=c["cwd"],
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            client = await stack.enter_async_context(ClientSession(read, write))
        await client.initialize()
        res = await client.list_tools()
        tools = [t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in res.tools]
    except Exception:
        await stack.aclose()
        raise
    return _Session(stack, client, tools)


async def _teardown(name: str, sess: _Session) -> None:
    try:
        await sess.stack.aclose()
    except Exception:
        pass
    _sessions.pop(name, None)


async def _ensure(name: str):
    """Return (cfg, session), connecting lazily / reconnecting as needed.

    Connect-stage failures retry once. Raises WindesktopctlError when the
    upstream can't be reached (after autostart attempt where configured).
    """
    c = _upstream_cfg(name)
    _require_enabled(c)
    sess = _sessions.get(name)
    if sess is not None and not sess.broken:
        return c, sess
    if sess is not None:
        await _teardown(name, sess)
    await _maybe_autostart(c)
    last: Exception | None = None
    for attempt in range(CONNECT_RETRIES):
        try:
            sess = await _connect(c)
            _sessions[name] = sess
            _last_error.pop(name, None)
            return c, sess
        except Exception as e:  # connect-stage failure -> retry once
            last = e
            if attempt + 1 < CONNECT_RETRIES:
                await asyncio.sleep(CONNECT_RETRY_DELAY_S)
    _last_error[name] = str(last)
    where = c["url"] or " ".join(c["command"] or [])
    raise WindesktopctlError(
        f"cannot connect to MCP upstream {name!r} ({where}): {last}"
        + (f" (log: {_managed[name].log_path})" if name in _managed else "")
    )


async def _drop_session(name: str) -> None:
    sess = _sessions.pop(name, None)
    if sess is not None:
        try:
            await sess.stack.aclose()
        except Exception:
            pass


async def _drop_all() -> None:
    for n in list(_sessions):
        await _drop_session(n)


SHUTDOWN_CALL_WAIT_S = 45.0


async def _shutdown_call(name: str, c: dict) -> bool:
    """Best-effort graceful stop via a configured upstream tool call on the
    live session (never triggers autostart). Returns True if the call was
    sent; the caller then waits for the process to exit on its own."""
    sess = _sessions.get(name)
    if sess is None or sess.broken:
        return False
    try:
        await asyncio.wait_for(
            sess.client.call_tool(
                str(c["shutdown_tool"]), dict(c.get("shutdown_args") or {})
            ),
            timeout=20,
        )
    except Exception:
        return False
    return True


# ------------------------------------------------------------ headless mgmt --


def _db_dir(start_command: list[str]) -> str | None:
    """cwd for a managed headless process: the folder of the DB file, taken
    as the last existing-file argument (idalib_server's positional input)."""
    for arg in reversed(start_command):
        if os.path.isfile(arg):
            return os.path.dirname(os.path.abspath(arg))
    return None


def _spawn_managed(c: dict) -> _Managed:
    log_dir = st.state_dir() / "upstreams"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{c['name']}.log"
    log_f = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            list(c["start_command"]),
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=_db_dir(c["start_command"]),
            creationflags=_spawn_flags(),
        )
    finally:
        log_f.close()  # child holds its own handle
    with _managed_lock:
        old = _managed.pop(c["name"], None)
        if old is not None:
            try:
                old.proc.kill()
            except Exception:
                pass
        mp = _Managed(c["name"], proc, str(log_path), c)
        _managed[c["name"]] = mp
    return mp


async def _maybe_autostart(c: dict) -> None:
    """Start a managed headless process when the upstream is unreachable and
    its start_if_unreachable guard is also down."""
    if not c["start_command"]:
        return
    if await _reachable(c):
        return
    with _managed_lock:
        mp = _managed.get(c["name"])
    if mp is not None and mp.proc.poll() is None:
        # already starting/running but port not up yet -> just wait for it
        pass
    else:
        guard = c["start_if_unreachable"]
        if guard:
            gcfg = cfg.mcp_upstreams().get(guard)
            if gcfg is not None and gcfg["enabled"] and await _reachable(gcfg):
                raise WindesktopctlError(
                    f"{guard} is running; use server {guard!r} instead of "
                    f"starting {c['name']!r} headless (one IDA per database)"
                )
        mp = _spawn_managed(c)
    deadline = time.monotonic() + max(1, int(c["start_timeout_s"]))
    while time.monotonic() < deadline:
        if await _reachable(c):
            return
        if mp.proc.poll() is not None:
            raise WindesktopctlError(
                f"{c['name']} exited during startup (code {mp.proc.returncode}); "
                f"see {mp.log_path}"
            )
        await asyncio.sleep(STOP_POLL_S)
    raise WindesktopctlError(
        f"{c['name']} did not become reachable within {c['start_timeout_s']}s; "
        f"see {mp.log_path}"
    )


def _stop_managed_sync(name: str, grace_s: float = MANAGED_STOP_GRACE_S) -> str:
    """Graceful stop of a managed headless process (CTRL_C -> SIGINT first,
    then CTRL_BREAK, up to grace_s, then taskkill tree).

    Graceful order: (1) ``shutdown_tool``/``shutdown_args`` call on the live
    session when configured (e.g. idalib's idb_open idle_ttl_sec self-exit -
    the only clean DB close idalib offers, since Windows cannot deliver
    SIGINT to a process group); (2) CTRL_BREAK_EVENT -> SIGBREAK, the only
    console event that can be aimed at a process group (CTRL_C_EVENT is a
    documented no-op for groups); (3) taskkill tree."""
    with _managed_lock:
        mp = _managed.get(name)
    if mp is None:
        return f"{name}: no managed process running"
    proc = mp.proc

    # -- 1) upstream-assisted graceful stop -------------------------------
    if mp.cfg.get("shutdown_tool"):
        try:
            sent = _run(_shutdown_call(name, mp.cfg), timeout=30)
        except Exception:
            sent = False
        if sent:
            deadline = time.monotonic() + SHUTDOWN_CALL_WAIT_S
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.5)
            if proc.poll() is not None:
                with _managed_lock:
                    _managed.pop(name, None)
                try:
                    _run(_drop_session(name), timeout=15)
                except Exception:
                    pass
                return (
                    f"{name}: stopped pid={mp.pid} "
                    f"(exited code={proc.returncode} via {mp.cfg['shutdown_tool']}); "
                    f"log: {mp.log_path}"
                )

    # -- 2) console CTRL_BREAK -> SIGBREAK ---------------------------------
    with _managed_lock:
        _managed.pop(name, None)
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    except Exception:
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if proc.poll() is None:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True, timeout=15,
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception:
                pass
    status = (
        f"exited code={proc.returncode}" if proc.poll() is not None else "still running (kill failed)"
    )
    try:
        _run(_drop_session(name), timeout=15)
    except Exception:
        pass
    return f"{name}: stopped pid={mp.pid} ({status}); log: {mp.log_path}"


def _stop_all_managed() -> None:
    with _managed_lock:
        names = list(_managed)
    for n in names:
        try:
            _stop_managed_sync(n)
        except Exception:
            pass


atexit.register(_stop_all_managed)


# ------------------------------------------------------------ public sync API --


def servers_status() -> str:
    """mcp_servers tool body (no stop)."""
    safety.check_paused()
    ups = cfg.mcp_upstreams()
    if not ups:
        return "(no [mcp_upstreams.*] entries in config.toml)"

    async def gather():
        rows = []
        for name, c in ups.items():
            reachable = await _reachable(c) if c["enabled"] else False
            sess = _sessions.get(name)
            connected = sess is not None and not sess.broken
            tools_n = len(sess.tools) if sess is not None and sess.tools else 0
            with _managed_lock:
                mp = _managed.get(name)
            managed = (
                f"pid={mp.pid} {'running' if mp.proc.poll() is None else 'exited'}"
                if mp is not None else "-"
            )
            err = _last_error.get(name) or (sess.last_error if sess else None)
            rows.append({
                "name": name, "kind": c["kind"], "enabled": c["enabled"],
                "reachable": reachable, "connected": connected,
                "tools": tools_n, "managed": managed,
                "error": (err or "")[:120],
                "deny": sorted(c["deny"]),
            })
        return rows

    try:
        rows = _run(gather(), timeout=30)
    except Exception as e:
        return f"error: {e}"
    lines = []
    for r in rows:
        state = "up" if r["reachable"] else "down"
        conn = "connected" if r["connected"] else "-"
        extra = f" tools={r['tools']}" if r["tools"] else ""
        mg = f" managed[{r['managed']}]" if r["managed"] != "-" else ""
        err = f" error={r['error']!r}" if r["error"] else ""
        deny = f" deny={','.join(r['deny'])}" if r["deny"] else ""
        lines.append(
            f"{r['name']} kind={r['kind']} enabled={r['enabled']} "
            f"reachable={state} {conn}{extra}{mg}{deny}{err}"
        )
    return "\n".join(lines)


def stop_server(name: str) -> str:
    safety.check_paused()
    c = _upstream_cfg(name)
    if c["start_command"] is None:
        raise WindesktopctlError(f"{name!r} has no start_command (nothing managed to stop)")
    return _stop_managed_sync(name)


def _connect_budget_s(name: str) -> float:
    """Extra ``_run`` headroom so a first call can wait out headless
    autostart (``start_timeout_s``) before the operation timeout applies."""
    try:
        c = cfg.mcp_upstreams().get(str(name)) or {}
    except Exception:
        return 0.0
    if not c.get("start_command"):
        return 0.0
    try:
        return float(c.get("start_timeout_s") or 0)
    except Exception:
        return 0.0


async def _tools_raw(name: str, refresh: bool):
    c, sess = await _ensure(name)
    if refresh or sess.tools is None:
        res = await sess.client.list_tools()
        sess.tools = [
            t.model_dump() if hasattr(t, "model_dump") else dict(t) for t in res.tools
        ]
    return c, sess


def tools_list(name: str, filter: str | None = None, include_denied: bool = False,
               refresh: bool = False) -> str:
    safety.check_paused()
    try:
        c, sess = _run(
            _tools_raw(name, refresh),
            timeout=60 + _connect_budget_s(name),
        )
    except WindesktopctlError as e:
        return f"error: {e}"
    except Exception as e:
        return f"error: cannot reach {name!r}: {e}"
    denied = set(c["deny"])
    flt = (filter or "").strip().lower()
    out_lines = []
    shown = 0
    total_size = 0
    truncated = False
    for t in sess.tools or []:
        tname = str(t.get("name", ""))
        desc = str(t.get("description") or "")
        is_denied = tname in denied
        if is_denied and not include_denied:
            continue
        if flt and flt not in tname.lower() and flt not in desc.lower():
            continue
        schema = json.dumps(t.get("inputSchema") or {}, separators=(",", ":"))
        d = desc[:DESC_MAX_CHARS] + ("..." if len(desc) > DESC_MAX_CHARS else "")
        tag = " [denied]" if is_denied else ""
        line = f"{tname}{tag}: {d}\n  schema={schema}"
        if total_size + len(line) > TOOLS_MAX_BYTES:
            truncated = True
            break
        out_lines.append(line)
        total_size += len(line)
        shown += 1
    if not out_lines:
        return "(no tools match)"
    out = "\n".join(out_lines)
    if truncated:
        out += f"\n[... truncated at {TOOLS_MAX_BYTES} bytes - narrow the filter ...]"
    return out


# ------------------------------------------------------------- call + format --


def _save_image(data_b64: str, mime: str, idx: int) -> str:
    ext = {
        "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    }.get((mime or "").lower(), "bin")
    d = st.state_dir() / "upstreams"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"img_{int(time.time() * 1000)}_{idx}.{ext}"
    try:
        path.write_bytes(__import__("base64").b64decode(data_b64))
    except Exception:
        pass
    return str(path)


def format_call_result(res, image_start: int = 1) -> tuple[str, int]:
    """Format a CallToolResult-ish object into display text. Returns
    (text, next_image_index). Pure-ish (image saving hits state_dir)."""
    is_err = bool(getattr(res, "isError", False) or (res.get("isError") if isinstance(res, dict) else False))
    content = getattr(res, "content", None)
    if content is None and isinstance(res, dict):
        content = res.get("content") or []
    structured = getattr(res, "structuredContent", None)
    if structured is None and isinstance(res, dict):
        structured = res.get("structuredContent")

    parts: list[str] = []
    if is_err:
        parts.append("upstream error:")
    img_idx = image_start
    for item in content or []:
        if isinstance(item, dict):
            itype = item.get("type")
            itext = item.get("text")
            idata = item.get("data")
            imime = item.get("mimeType") or item.get("mime_type")
            ires = item.get("resource")
        else:
            itype = getattr(item, "type", None)
            itext = getattr(item, "text", None)
            idata = getattr(item, "data", None)
            imime = getattr(item, "mimeType", None) or getattr(item, "mime_type", None)
            ires = getattr(item, "resource", None)
        if itype == "text" or (itype is None and itext is not None):
            parts.append(str(itext))
        elif itype == "image" and idata:
            path = _save_image(str(idata), str(imime or ""), img_idx)
            img_idx += 1
            try:
                nbytes = len(__import__("base64").b64decode(str(idata)))
            except Exception:
                nbytes = -1
            parts.append(f"[image {imime or '?'} {nbytes} bytes] saved: {path}")
        elif itype in ("resource", "resource_link") or ires is not None:
            ruri = None
            rtext = None
            rmime = None
            rblob = None
            if isinstance(ires, dict):
                ruri = ires.get("uri"); rtext = ires.get("text")
                rmime = ires.get("mimeType"); rblob = ires.get("blob")
            else:
                ruri = getattr(ires, "uri", None)
                rtext = getattr(ires, "text", None)
                rmime = getattr(ires, "mimeType", None)
                rblob = getattr(ires, "blob", None)
            if rtext is not None:
                parts.append(f"--- resource {ruri} ---\n{rtext}")
            elif rblob:
                parts.append(f"[resource {ruri} {rmime or '?'} binary] {len(str(rblob))} b64 chars")
            else:
                parts.append(f"[resource {ruri or '?'}]")
        elif itype == "audio" and idata:
            parts.append(f"[audio {imime or '?'}] {len(str(idata))} b64 chars")
        else:
            parts.append(json.dumps(item if isinstance(item, dict) else getattr(item, "model_dump", lambda: str(item))(), default=str)[:2000])
    if structured is not None:
        try:
            parts.append("--- structuredContent ---\n" + json.dumps(structured, separators=(",", ":"), default=str))
        except Exception:
            parts.append("--- structuredContent ---\n" + str(structured))
    out = "\n".join(p for p in parts if p)
    if not out:
        out = "(empty result)"
    if len(out.encode("utf-8")) > RESULT_MAX_BYTES:
        out = out.encode("utf-8")[:RESULT_MAX_BYTES].decode("utf-8", errors="replace")
        out += f"\n[... truncated at {RESULT_MAX_BYTES} bytes ...]"
    return out, img_idx


async def _call_raw(name: str, tool: str, args: dict, timeout_s: float):
    c = _upstream_cfg(name)
    _require_enabled(c)
    if tool in c["deny"]:
        raise WindesktopctlError(
            f"tool {tool!r} is denied on upstream {name!r} "
            f"(allow_unsafe/deny_tools in config.toml)"
        )
    _c, sess = await _ensure(name)
    try:
        return await asyncio.wait_for(
            sess.client.call_tool(tool, dict(args or {})), timeout_s
        )
    except asyncio.TimeoutError:
        sess.broken = True
        sess.last_error = f"timeout after {timeout_s}s"
        raise UpstreamCallError(f"upstream call timed out after {timeout_s}s")
    except UpstreamCallError:
        raise
    except Exception as e:
        sess.broken = True
        sess.last_error = str(e)
        raise UpstreamCallError(str(e))


def call_tool(name: str, tool: str, args, timeout_s: float = 120) -> tuple[str, dict]:
    """Returns (display_text, audit_fields). Raises WindesktopctlError for
    refusals; UpstreamCallError text is returned verbatim instead."""
    safety.check_paused()
    try:
        arg_bytes = len(json.dumps(args if args is not None else {}, default=str))
    except Exception:
        arg_bytes = -1
    t0 = time.monotonic()
    try:
        res = _run(
            _call_raw(name, tool, args or {}, timeout_s),
            timeout=timeout_s + 30 + _connect_budget_s(name),
        )
    except UpstreamCallError as e:
        dur = time.monotonic() - t0
        return f"upstream error: {e}", {
            "server": name, "upstream_tool": tool, "arg_bytes": arg_bytes,
            "is_error": True, "duration_s": round(dur, 2),
        }
    except WindesktopctlError:
        raise
    except Exception as e:
        dur = time.monotonic() - t0
        return f"upstream error: {e}", {
            "server": name, "upstream_tool": tool, "arg_bytes": arg_bytes,
            "is_error": True, "duration_s": round(dur, 2),
        }
    dur = time.monotonic() - t0
    is_err = bool(getattr(res, "isError", False) or (res.get("isError") if isinstance(res, dict) else False))
    text, _idx = format_call_result(res)
    return text, {
        "server": name, "upstream_tool": tool, "arg_bytes": arg_bytes,
        "is_error": is_err, "duration_s": round(dur, 2),
    }


# ------------------------------------------------------- jsonrpc-http fallback --


class JsonRpcError(Exception):
    """JSON-RPC error response from an upstream."""


class _JsonRpcClient:
    """Minimal MCP client for upstreams whose /mcp endpoint speaks plain
    JSON-RPC over HTTP POST (application/json responses, optional
    Mcp-Session-Id) instead of the streamable-HTTP transport - e.g.
    zeromcp-style servers. Exposes the subset of ClientSession we use."""

    def __init__(self, http, url: str, headers: dict | None = None):
        self._http = http
        self.url = url
        self._headers = dict(headers or {})
        self._session_id = None
        self._next_id = 0

    def _hdrs(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        h.update(self._headers)
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, payload: dict, expect_response: bool = True):
        r = await self._http.post(self.url, json=payload, headers=self._hdrs())
        sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if r.status_code == 202 or not expect_response:
            return None
        if r.status_code >= 400:
            raise JsonRpcError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except Exception:
            raise JsonRpcError(f"non-JSON response ({r.status_code}): {r.text[:300]}")
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise JsonRpcError(str(msg))
        return data.get("result") if isinstance(data, dict) else data

    async def initialize(self):
        self._next_id += 1
        await self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "windesktopctl", "version": "1"},
            },
        })
        # initialized notification (202 expected, response ignored)
        try:
            await self._post({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }, expect_response=False)
        except Exception:
            pass  # some servers don't implement the notification

    async def list_tools(self):
        self._next_id += 1
        res = await self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/list",
            "params": {},
        })
        tools = (res or {}).get("tools") or []
        from types import SimpleNamespace

        return SimpleNamespace(tools=[dict(t) for t in tools])

    async def call_tool(self, name: str, arguments: dict):
        from types import SimpleNamespace

        self._next_id += 1
        res = await self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments or {})},
        })
        res = res or {}
        return SimpleNamespace(
            content=res.get("content") or [],
            structuredContent=res.get("structuredContent"),
            isError=bool(res.get("isError")),
        )
