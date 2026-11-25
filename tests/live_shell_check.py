"""Live check for the shell/file MCP tools over HTTP on an isolated
WINDESKTOPCTL_HOME (temp dir, own token + config). Exercises EVERY new
tool once, the refusal paths (outside-roots / pause / config toggles),
and the weak-token registration gate on a second port.

Run:

    .venv\\Scripts\\python tests\\live_shell_check.py
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / ".venv" / "Scripts" / "windesktopctl.exe"
PORT = 8799
WEAK_PORT = 8798
STRONG_TOKEN = secrets.token_urlsafe(32)   # >=32 chars, >=16 distinct
WEAK_TOKEN = "tooshort"                    # fails the strong-token gate

NEW_TOOLS = {
    "exec", "job_output", "job_kill", "job_list",
    "read_file", "write_file", "edit_file", "list_dir", "glob", "grep",
    "file_info", "download_chunk", "upload_chunk",
}
OLD_TOOLS = {
    "observe", "screenshot", "act", "windows", "focus", "wait_for",
    "window", "launch", "browser", "challenge", "challenge_answer",
    "pause", "resume", "status",
}

PASS = []
FAIL = []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {extra}" if extra and not cond else ""))


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port(port: int, proc: subprocess.Popen, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def write_config(home: Path, shell: bool, files: bool, roots: list[str]) -> None:
    root_lines = ",\n  ".join(repr(r) for r in roots)
    (home / "config.toml").write_text(
        "# windesktopctl live-check config\n"
        f"shell_enabled = {'true' if shell else 'false'}\n"
        f"file_tools_enabled = {'true' if files else 'false'}\n"
        f"file_roots = [\n  {root_lines},\n]\n",
        encoding="utf-8",
    )


def start_server(home: Path, port: int, token: str, stderr_path: Path):
    (home / "token").write_text(token, encoding="utf-8")
    err = stderr_path.open("wb")
    env = dict(os.environ)
    env["WINDESKTOPCTL_HOME"] = str(home)
    env.pop("WINDESKTOPCTL_TOKEN", None)
    env.pop("WINDESKTOPCTL_TOOLS", None)
    proc = subprocess.Popen(
        [str(EXE), "serve", "--expose", "local", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=err,
    )
    proc._stderr_file = err  # keep handle alive
    return proc


async def session(url: str, token: str):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    read, write, _ = await stack.enter_async_context(
        streamablehttp_client(url, headers=headers)
    )
    sess = await stack.enter_async_context(ClientSession(read, write))
    await sess.initialize()
    return sess


import contextlib

stack = contextlib.AsyncExitStack()


async def call(sess, name: str, args: dict | None = None) -> str:
    res = await sess.call_tool(name, args or {})
    parts = []
    for c in res.content or []:
        text = getattr(c, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


async def main() -> int:
    if not EXE.exists():
        print(f"missing {EXE} - pip install -e . first")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="wdlive_"))
    home = tmp / "home"
    home.mkdir()
    roots_dir = tmp / "roots"
    (roots_dir / "sub").mkdir(parents=True)
    (roots_dir / "alpha.txt").write_text("alpha line1\nalpha line2 needle-here\n", encoding="utf-8")
    (roots_dir / "sub" / "beta.py").write_text("print('beta')\n# needle comment\n", encoding="utf-8")
    write_config(home, shell=True, files=True, roots=[str(roots_dir)])

    procs = []
    try:
        # ------------------------------------------------ strong server --
        srv = start_server(home, PORT, STRONG_TOKEN, tmp / "srv8799.err")
        procs.append(srv)
        if not wait_port(PORT, srv):
            print("server failed to start; stderr:")
            print((tmp / "srv8799.err").read_text(errors="replace")[-3000:])
            return 1
        print(f"server up on 127.0.0.1:{PORT}  home={home}")

        url = f"http://127.0.0.1:{PORT}/mcp"
        async with stack:
            sess = await session(url, STRONG_TOKEN)
            tools = await sess.list_tools()
            names = {t.name for t in tools.tools}
            print(f"tools listed: {len(names)}")
            ok("27 tools registered", len(names) == 27, f"got {sorted(names)}")
            ok("all new tools present", NEW_TOOLS <= names)
            ok("all old tools present", OLD_TOOLS <= names)

            # ---- exec: powershell ----
            out = await call(sess, "exec", {"command": "Write-Output live-ps-ok", "shell": "powershell"})
            print("exec ps:", out.splitlines()[0] if out else "")
            ok("exec powershell", "live-ps-ok" in out and "exit_code=0" in out, out[:200])

            # ---- exec: cmd ----
            out = await call(sess, "exec", {"command": "echo live-cmd-ok", "shell": "cmd"})
            ok("exec cmd", "live-cmd-ok" in out, out[:200])

            # ---- exec: timeout kills tree ----
            out = await call(sess, "exec", {
                "command": "ping -n 30 127.0.0.1", "shell": "cmd", "timeout_s": 3})
            ok("exec timeout", "timed out" in out and "process tree killed" in out, out[:200])

            # ---- exec: background job + job_output + job_kill ----
            out = await call(sess, "exec", {
                "command": "echo bg-marker-42 & ping -n 30 127.0.0.1 >nul",
                "shell": "cmd", "background": True})
            print("bg start:", out)
            jid = None
            for tok in out.split():
                if tok.startswith("job_id="):
                    jid = tok.split("=", 1)[1]
            ok("exec background returns job_id", jid is not None, out)
            await asyncio.sleep(1.0)
            out = await call(sess, "job_output", {"job_id": jid})
            print("job_output:", out.splitlines()[0] if out else "")
            ok("job_output incremental", "bg-marker-42" in out or "running" in out, out[:300])
            out = await call(sess, "job_list")
            ok("job_list", jid in out, out[:200])
            out = await call(sess, "job_kill", {"job_id": jid})
            ok("job_kill", "killed" in out or "exited" in out, out[:200])

            # ---- write_file / read_file / edit_file ----
            target = str(roots_dir / "sub" / "newdir" / "note.txt")
            out = await call(sess, "write_file", {"path": target, "text": "hello\nworld needle\n"})
            ok("write_file", "wrote" in out, out[:200])
            out = await call(sess, "read_file", {"path": target})
            ok("read_file numbered", "1\thello" in out and "world needle" in out, out[:200])
            out = await call(sess, "edit_file", {
                "path": target, "old": "world needle", "new": "edited needle"})
            ok("edit_file", "replaced 1" in out, out[:200])
            out = await call(sess, "read_file", {"path": target, "offset": 2, "limit": 1})
            ok("read_file offset", "2\tedited needle" in out, out[:200])

            # ---- list_dir / glob / grep ----
            out = await call(sess, "list_dir", {"path": str(roots_dir)})
            ok("list_dir", "alpha.txt" in out and "sub" in out, out[:300])
            out = await call(sess, "glob", {"pattern": "**/*.txt", "path": str(roots_dir)})
            ok("glob", "alpha.txt" in out and "note.txt" in out, out[:300])
            out = await call(sess, "grep", {"pattern": "needle", "path": str(roots_dir)})
            ok("grep", "alpha.txt" in out and "needle" in out, out[:300])

            # ---- file_info ----
            out = await call(sess, "file_info", {"path": str(roots_dir / "alpha.txt")})
            ok("file_info", "sha256=" in out and "chunks=" in out, out[:200])

            # ---- upload ~10MB in 4MB chunks + sha verify ----
            blob = secrets.token_bytes(10 * 1024 * 1024 + 12345)  # ~10MB, not chunk-aligned
            sha = hashlib.sha256(blob).hexdigest()
            upath = str(roots_dir / "big.bin")
            CH = 4 * 1024 * 1024
            off = 0
            while off < len(blob):
                piece = blob[off:off + CH]
                done = off + CH >= len(blob)
                out = await call(sess, "upload_chunk", {
                    "path": upath, "offset": off,
                    "data_b64": base64.b64encode(piece).decode("ascii"),
                    "done": done, "sha256": sha if done else None,
                })
                if "error" in out.lower()[:20]:
                    ok("upload_chunk", False, out[:200])
                    break
                off += len(piece)
            else:
                ok("upload_chunk 10MB/3 chunks + sha", "uploaded" in out, out[:200])
            # wrong-offset resume error
            out = await call(sess, "upload_chunk", {
                "path": str(roots_dir / "other.bin"), "offset": 5,
                "data_b64": base64.b64encode(b"x").decode("ascii")})
            ok("upload offset-mismatch error", "offset mismatch" in out or "expected" in out, out[:200])

            # ---- download back + sha verify ----
            size = len(blob)
            got = bytearray()
            off = 0
            while off < size:
                out = await call(sess, "download_chunk", {"path": upath, "offset": off, "length": CH})
                j = json.loads(out)
                got += base64.b64decode(j["data_b64"])
                off += j["length"]
                if j["eof"]:
                    break
            ok("download_chunk reassembly sha", hashlib.sha256(bytes(got)).hexdigest() == sha,
               f"got {len(got)} bytes")

            # ---- refusals ----
            out = await call(sess, "read_file", {"path": "C:\\Windows\\win.ini"})
            ok("outside-roots read refused", "error" in out.lower() and "outside" in out.lower(), out[:200])
            out = await call(sess, "write_file", {"path": "C:\\Windows\\Temp\\wd-evil.txt", "text": "x"})
            ok("outside-roots write refused", "error" in out.lower(), out[:200])
            out = await call(sess, "list_dir", {"path": str(tmp)})  # parent of roots
            ok("outside-roots list_dir refused", "error" in out.lower() and "outside" in out.lower(), out[:200])

            # pause flag -> everything refuses
            (home / "paused").write_text("test", encoding="utf-8")
            out = await call(sess, "exec", {"command": "echo x", "shell": "cmd"})
            ok("pause blocks exec", "paused" in out.lower(), out[:200])
            out = await call(sess, "read_file", {"path": str(roots_dir / "alpha.txt")})
            ok("pause blocks read_file", "paused" in out.lower(), out[:200])
            (home / "paused").unlink()

            # shell_enabled=false via config rewrite (mtime reload)
            write_config(home, shell=False, files=True, roots=[str(roots_dir)])
            time.sleep(0.05)
            out = await call(sess, "exec", {"command": "echo x", "shell": "cmd"})
            ok("shell_enabled=false refuses", "disabled in config.toml" in out, out[:200])
            # file tools still work while shell off
            out = await call(sess, "read_file", {"path": str(roots_dir / "alpha.txt"), "limit": 1})
            ok("file tools still on when shell off", "alpha line1" in out, out[:200])

            write_config(home, shell=True, files=False, roots=[str(roots_dir)])
            time.sleep(0.05)
            out = await call(sess, "read_file", {"path": str(roots_dir / "alpha.txt")})
            ok("file_tools_enabled=false refuses", "disabled in config.toml" in out, out[:200])

            write_config(home, shell=True, files=True, roots=[str(roots_dir)])
            time.sleep(0.05)
            out = await call(sess, "exec", {"command": "echo back-on", "shell": "cmd"})
            ok("config toggle back on (mtime reload)", "back-on" in out, out[:200])

        # ------------------------------------------------ audit log ----
        audit = home / "audit.log"
        lines = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
        print(f"audit.log: {len(lines)} lines")
        recs = [json.loads(l) for l in lines if l.strip()]
        tools_seen = {r.get("tool") for r in recs}
        ok("audit has exec+file tools", {"exec", "read_file", "write_file", "upload_chunk"} <= tools_seen,
           f"tools={sorted(tools_seen)}")
        clients = {r.get("client") for r in recs}
        ok("audit client IP captured", clients == {"127.0.0.1"}, f"clients={clients}")
        ok("audit never logs content", all("data_b64" not in l and "content" not in r for l, r in zip(lines, recs)))
        print("--- audit sample ---")
        for l in lines[:6]:
            print(" ", l[:220])

        # ------------------------------------------------ weak-token ----
        weak_home = tmp / "weakhome"
        weak_home.mkdir()
        write_config(weak_home, shell=True, files=True, roots=[str(roots_dir)])
        wsrv = start_server(weak_home, WEAK_PORT, WEAK_TOKEN, tmp / "srv8798.err")
        procs.append(wsrv)
        if not wait_port(WEAK_PORT, wsrv):
            print("weak-token server failed to start")
        else:
            async with stack:
                wsess = await session(f"http://127.0.0.1:{WEAK_PORT}/mcp", WEAK_TOKEN)
                wtools = await wsess.list_tools()
                wnames = {t.name for t in wtools.tools}
                print(f"weak-token tools: {len(wnames)}")
                ok("weak token -> new tools absent", not (NEW_TOOLS & wnames),
                   f"got {sorted(wnames & NEW_TOOLS)}")
                ok("weak token -> old tools present", OLD_TOOLS <= wnames)
            errtxt = (tmp / "srv8798.err").read_text(errors="replace")
            ok("weak-token stderr warning", "token too weak" in errtxt, errtxt[-300:])
            errtxt9 = (tmp / "srv8799.err").read_text(errors="replace")
            ok("strong server no weak warning", "token too weak" not in errtxt9)

    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=8)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            try:
                p._stderr_file.close()
            except Exception:
                pass
        # give children a moment, then remove temp tree
        time.sleep(0.5)
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
