"""Live check for the MCP upstream passthrough against a REAL idalib
headless server - on a COPY of the .i64 only, never the original.

Drives the actual MCP server (isolated WINDESKTOPCTL_HOME, own token) and
exercises: tool registration (30), mcp_servers, autostart of ida_headless,
mcp_tools filter, harmless read calls, py_eval denial, streamable-HTTP vs
jsonrpc-http transport probe, kill->reconnect, graceful stop.

Run:

    .venv\\Scripts\\python tests\\live_ida_check.py --i64 <copy.i64>

The copy is deleted at the end unless --keep-copy is passed. The idalib
worker needs a Python with ida_pro_mcp + idapro installed - set
WINDESKTOPCTL_IDA_PYTHON to it (defaults to this interpreter).
"""

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "windesktopctl.exe"
SRV_PORT = 8797
IDALIB_PORT = 18745
IDA_GUI_PORT = 13337
IDA_PY = os.environ.get("WINDESKTOPCTL_IDA_PYTHON") or sys.executable
LONG = timedelta(seconds=900)   # MCP read timeout for autostart waits

PASS = []
FAIL = []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {extra}" if extra and not cond else ""), flush=True)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.5)
    return False


def write_config(home: Path, i64: Path) -> None:
    cmd = [
        IDA_PY, "-m", "ida_pro_mcp.idalib_server",
        "--host", "127.0.0.1", "--port", str(IDALIB_PORT),
        str(i64),
    ]
    # TOML literal strings (single quotes) keep backslashes verbatim
    cmd_toml = ", ".join("'" + c + "'" for c in cmd)
    (home / "config.toml").write_text(f"""\
# live IDA upstream check
shell_enabled = false
file_tools_enabled = false
file_roots = []

[mcp_upstreams.ida]
url = "http://127.0.0.1:{IDA_GUI_PORT}/mcp"
enabled = true
allow_unsafe = false
transport = "jsonrpc-http"

[mcp_upstreams.ida_headless]
url = "http://127.0.0.1:{IDALIB_PORT}/mcp"
enabled = true
allow_unsafe = false
transport = "jsonrpc-http"
start_command = [{cmd_toml}]
start_if_unreachable = "ida"
start_timeout_s = 300
shutdown_tool = "idb_open"
shutdown_args = {{ input_path = '{i64}', run_auto_analysis = false, build_caches = false, init_hexrays = false, idle_ttl_sec = 10 }}

# same server, default streamable-http transport - probe whether the
# official mcp client can talk to zeromcp (no start_command: attaches to
# the already-running headless instance)
[mcp_upstreams.ida_sh]
url = "http://127.0.0.1:{IDALIB_PORT}/mcp"
enabled = true
allow_unsafe = false
""", encoding="utf-8")


def start_server(home: Path, token: str, stderr_path: Path):
    (home / "token").write_text(token, encoding="utf-8")
    err = stderr_path.open("wb")
    env = dict(os.environ)
    env["WINDESKTOPCTL_HOME"] = str(home)
    env.pop("WINDESKTOPCTL_TOKEN", None)
    env.pop("WINDESKTOPCTL_TOOLS", None)
    proc = subprocess.Popen(
        [str(EXE), "serve", "--expose", "local", "--port", str(SRV_PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=err,
    )
    proc._stderr_file = err
    return proc


stack = contextlib.AsyncExitStack()


async def call(sess, name: str, args: dict | None = None, timeout: int = 60) -> str:
    res = await sess.call_tool(name, args or {}, read_timeout_seconds=timedelta(seconds=timeout))
    parts = []
    for c in res.content or []:
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


def managed_pid(status_text: str) -> int | None:
    m = re.search(r"managed\[pid=(\d+)", status_text or "")
    return int(m.group(1)) if m else None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--i64", required=True, help="path to the .i64 COPY")
    ap.add_argument("--keep-copy", action="store_true")
    args = ap.parse_args()
    i64 = Path(args.i64)
    if not i64.exists():
        print(f"missing {i64}")
        return 1
    if "windesktopctl_ida_test" not in str(i64):
        print("refusing: --i64 must be the %TEMP% copy, not the real database")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="wdida_"))
    home = tmp / "home"
    home.mkdir()
    write_config(home, i64)
    token = secrets.token_urlsafe(32)

    srv = None
    try:
        srv = start_server(home, token, tmp / "srv.err")
        deadline = time.time() + 40
        while time.time() < deadline and not port_open(SRV_PORT):
            if srv.poll() is not None:
                break
            time.sleep(0.25)
        if not port_open(SRV_PORT):
            print("server failed to start; stderr:")
            print((tmp / "srv.err").read_text(errors="replace")[-3000:])
            return 1
        print(f"server up on 127.0.0.1:{SRV_PORT}  home={home}", flush=True)

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with stack:
            headers = {"Authorization": f"Bearer {token}"}
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(f"http://127.0.0.1:{SRV_PORT}/mcp", headers=headers)
            )
            sess = await stack.enter_async_context(ClientSession(read, write))
            await sess.initialize()

            tools = await sess.list_tools()
            names = {t.name for t in tools.tools}
            print(f"tools listed: {len(names)}")
            ok("30 tools registered", len(names) == 30, f"got {sorted(names)}")
            ok("mcp_* tools present",
               {"mcp_servers", "mcp_tools", "mcp_call"} <= names)

            # ---- mcp_servers before any use ----
            out = await call(sess, "mcp_servers")
            print("mcp_servers:\n" + out)
            ida_line = next((l for l in out.splitlines() if l.startswith("ida ")), "")
            hl_line = next((l for l in out.splitlines() if l.startswith("ida_headless")), "")
            ok("ida listed not reachable", "reachable=down" in ida_line, ida_line)
            ok("ida_headless listed, not started",
               "reachable=down" in hl_line and "managed[pid" not in hl_line, hl_line)

            # ---- autostart + tools list (the long pole: idalib loads the DB) ----
            print("calling mcp_tools ida_headless filter=func (autostart; may take minutes)...", flush=True)
            t0 = time.time()
            out = await call(sess, "mcp_tools", {"server": "ida_headless", "filter": "func"}, timeout=700)
            print(f"mcp_tools took {time.time()-t0:.0f}s")
            print(out[:2500])
            ok("autostart+tools_list worked", "error" not in out[:12].lower() and ("func" in out or "list_funcs" in out), out[:300])
            ok("py_eval hidden (denied)", "py_eval" not in out)

            # ---- harmless read calls ----
            print("calling survey_binary (minimal)...", flush=True)
            out = await call(sess, "mcp_call",
                             {"server": "ida_headless", "tool": "survey_binary",
                              "args": {"detail_level": "minimal"}}, timeout=300)
            print(out[:1500])
            ok("survey_binary", "metadata" in out or "entrypoints" in out or "statistics" in out, out[:300])

            print("calling list_funcs count=5...", flush=True)
            out = await call(sess, "mcp_call",
                             {"server": "ida_headless", "tool": "list_funcs",
                              "args": {"queries": [{"filter": "", "offset": 0, "count": 5}]}},
                             timeout=300)
            print(out[:1200])
            ok("list_funcs", "addr" in out or "name" in out, out[:300])

            # ---- denied tool refused client-side ----
            out = await call(sess, "mcp_call",
                             {"server": "ida_headless", "tool": "py_eval",
                              "args": {"code": "1+1"}}, timeout=60)
            print("py_eval ->", out[:300])
            ok("py_eval denied", "denied" in out.lower(), out[:200])

            # ---- audit log has mcp_call entries, no arg contents ----
            audit = home / "audit.log"
            if audit.exists():
                lines = [l for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
                recs = [json.loads(l) for l in lines]
                mcalls = [r for r in recs if r.get("tool") == "mcp_call"]
                print(f"audit.log: {len(lines)} lines, {len(mcalls)} mcp_call")
                ok("audit mcp_call fields",
                   all({"server", "upstream_tool", "arg_bytes", "is_error"} <= set(r) for r in mcalls) and len(mcalls) >= 3,
                   json.dumps(mcalls[:2])[:300])
                ok("audit no arg contents",
                   all("code" not in l and "1+1" not in l for l in lines))
                for l in lines[-3:]:
                    print("  audit:", l[:200])

            # ---- streamable-http probe against the same zeromcp server ----
            print("probing streamable-http transport (ida_sh)...", flush=True)
            out = await call(sess, "mcp_tools", {"server": "ida_sh", "filter": "survey"}, timeout=120)
            print(out[:600])
            sh_ok = "error" not in out[:12].lower() and "survey_binary" in out
            print(f"STREAMABLE_HTTP_WORKS={sh_ok}")
            # full call through the streamable transport too
            out = await call(sess, "mcp_call",
                             {"server": "ida_sh", "tool": "list_funcs",
                              "args": {"queries": [{"filter": "", "offset": 0, "count": 1}]}},
                             timeout=120)
            print("ida_sh call:", out[:300])
            ok("streamable-http call_tool works", "addr" in out or "name" in out, out[:200])

            # ---- kill -> reconnect ----
            out = await call(sess, "mcp_servers")
            pid = managed_pid(out)
            print(f"managed pid={pid}")
            ok("managed pid tracked", pid is not None, out)
            if pid:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
                time.sleep(1.0)
                print("killed headless; first call should error + mark broken...", flush=True)
                out = await call(sess, "mcp_call",
                                 {"server": "ida_headless", "tool": "list_funcs",
                                  "args": {"queries": [{"filter": "", "offset": 0, "count": 1}]}},
                                 timeout=120)
                print("post-kill call 1:", out[:300])
                ok("post-kill call errors (no resend)", "error" in out.lower()[:60] or "upstream error" in out.lower(), out[:200])
                print("post-kill call 2: autostart respawn + reconnect (may take minutes)...", flush=True)
                t0 = time.time()
                out = await call(sess, "mcp_call",
                                 {"server": "ida_headless", "tool": "list_funcs",
                                  "args": {"queries": [{"filter": "", "offset": 0, "count": 3}]}},
                                 timeout=700)
                print(f"post-kill call 2 took {time.time()-t0:.0f}s: {out[:400]}")
                ok("reconnect after respawn", "addr" in out or "name" in out, out[:300])

            # ---- graceful stop ----
            print("mcp_servers(stop=ida_headless) - graceful stop...", flush=True)
            t0 = time.time()
            out = await call(sess, "mcp_servers", {"stop": "ida_headless"}, timeout=120)
            print(f"stop took {time.time()-t0:.0f}s: {out[:400]}")
            ok("graceful stop reported exit", "stopped" in out or "exited" in out, out[:300])
            time.sleep(1.0)
            ok("idalib port closed after stop", not port_open(IDALIB_PORT))

    finally:
        if srv is not None:
            try:
                srv.terminate()
                srv.wait(timeout=8)
            except Exception:
                try:
                    srv.kill()
                except Exception:
                    pass
            try:
                srv._stderr_file.close()
            except Exception:
                pass
        # last-resort: make sure no idalib on our test port survives
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {IDALIB_PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess"],
                capture_output=True, text=True, timeout=15)
            for line in out.stdout.split():
                line = line.strip()
                if line.isdigit():
                    subprocess.run(["taskkill", "/PID", line, "/F"],
                                   capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)
        if not args.keep_copy:
            try:
                i64.unlink()
                print(f"deleted copy {i64}")
            except Exception as e:
                print(f"could not delete copy: {e}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
