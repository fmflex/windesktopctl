"""Shell exec + background jobs.

``exec`` runs powershell.exe or cmd.exe with piped stdout/stderr (head+tail
capture), an optional cwd/env, a clamped timeout that kills the whole process
tree, and an optional background mode that streams output to files under
``<state_dir>/jobs/`` with a per-stream read cursor for ``job_output``.

Gated by ``config.shell_enabled()`` and ``safety.check_paused()``. exec is a
FULL shell: cwd is NOT restricted to file_roots (the roots only limit the
file tools); when omitted it defaults to the user's home directory so
relative-path writes never land in the server's own cwd.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import threading
import time
from pathlib import Path

from . import WindesktopctlError
from . import config as cfg
from . import safety
from . import state as st

HEAD_TAIL_BYTES = 32 * 1024
DEFAULT_TIMEOUT_S = 120
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 1800
JOB_PRUNE_AGE_S = 24 * 3600
CREATE_NO_WINDOW = 0x08000000


def _require_shell() -> None:
    safety.check_paused()
    if not cfg.shell_enabled():
        raise WindesktopctlError("shell tools disabled in config.toml (shell_enabled=false)")


def _cmdline(command: str, shell: str) -> list[str]:
    shell = str(shell or "powershell").lower()
    if shell == "powershell":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + str(command),
        ]
    if shell == "cmd":
        return ["cmd.exe", "/d", "/s", "/c", str(command)]
    raise WindesktopctlError(f"shell must be powershell|cmd (got {shell!r})")


def _env(env: dict | None) -> dict:
    full = dict(os.environ)
    if env:
        full.update({str(k): str(v) for k, v in env.items()})
    return full


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


class _StreamCapture:
    """Keep the first and last HEAD_TAIL_BYTES of a stream plus a total."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            self.total += len(chunk)
            room = HEAD_TAIL_BYTES - len(self.head)
            if room > 0:
                self.head += chunk[:room]
                chunk = chunk[room:]
            self.tail += chunk
            if len(self.tail) > HEAD_TAIL_BYTES:
                del self.tail[: len(self.tail) - HEAD_TAIL_BYTES]

    def text(self) -> str:
        with self._lock:
            head = bytes(self.head)
            tail = bytes(self.tail)
            total = self.total
        parts = []
        parts.append(head.decode("utf-8", errors="replace"))
        dropped = total - len(head) - len(tail)
        if dropped > 0:
            parts.append(f"\n[... {dropped} bytes truncated ...]\n")
        parts.append(tail.decode("utf-8", errors="replace"))
        return "".join(parts)


def _spawn(command: str, shell: str, cwd, env, out, err) -> subprocess.Popen:
    argv = _cmdline(command, shell)
    # No cwd -> the user's home, NOT the server's cwd: a command that writes
    # relative paths must never drop files into the repo/state dir the
    # server happens to run from.
    workdir = str(cwd) if cwd else os.path.expanduser("~")
    kw = dict(
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        cwd=workdir,
        env=_env(env),
        creationflags=CREATE_NO_WINDOW,
    )
    return subprocess.Popen(argv, **kw)


def _format_result(cap_out: _StreamCapture, cap_err: _StreamCapture,
                   code, duration: float, timed_out: bool,
                   timeout_s: int = 0) -> str:
    lines = [f"exit_code={code} duration={duration:.1f}s"]
    if timed_out:
        lines.append(f"timed out after {timeout_s}s (process tree killed)")
    so, se = cap_out.text(), cap_err.text()
    if so:
        lines += ["--- stdout ---", so.rstrip("\n")]
    if se:
        lines += ["--- stderr ---", se.rstrip("\n")]
    return "\n".join(lines)


def exec_cmd(
    command: str,
    shell: str = "powershell",
    cwd: str | None = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    env: dict | None = None,
    background: bool = False,
):
    """Run a shell command. Returns display text (foreground) or a job
    descriptor line (background=True -> job_id=… pid=…). cwd defaults to
    the user's home directory when omitted."""
    _require_shell()
    command = str(command)
    if not command.strip():
        raise WindesktopctlError("empty command")
    timeout_s = max(MIN_TIMEOUT_S, min(int(timeout_s), MAX_TIMEOUT_S))
    if cwd is not None and not os.path.isdir(str(cwd)):
        raise WindesktopctlError(f"cwd does not exist: {cwd}")
    if background:
        return start_job(command, shell, cwd, env)

    cap_out, cap_err = _StreamCapture(), _StreamCapture()

    def reader(stream, cap):
        try:
            for chunk in iter(lambda: stream.read(65536), b""):
                cap.feed(chunk)
        except Exception:
            pass

    t0 = time.monotonic()
    proc = _spawn(command, shell, cwd, env,
                  subprocess.PIPE, subprocess.PIPE)
    th_out = threading.Thread(target=reader, args=(proc.stdout, cap_out), daemon=True)
    th_err = threading.Thread(target=reader, args=(proc.stderr, cap_err), daemon=True)
    th_out.start()
    th_err.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            code = -1
    th_out.join(timeout=5)
    th_err.join(timeout=5)
    dur = time.monotonic() - t0
    return _format_result(cap_out, cap_err, code, dur, timed_out, timeout_s)


# --------------------------------------------------------- background jobs --

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_job_seq = itertools.count(1)


def _jobs_dir() -> Path:
    d = st.state_dir() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_jobs(now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _jobs_lock:
        for jid, j in list(_jobs.items()):
            if j["status"] != "running" and now - j["finished"] > JOB_PRUNE_AGE_S:
                del _jobs[jid]
                for suffix in (".out", ".err"):
                    try:
                        Path(j["dir"], jid + suffix).unlink()
                    except OSError:
                        pass


def start_job(command: str, shell: str = "powershell", cwd=None, env=None) -> str:
    """Start a background job; output streams to <state_dir>/jobs/<id>.out|.err."""
    _require_shell()
    command = str(command)
    if not command.strip():
        raise WindesktopctlError("empty command")
    if cwd is not None and not os.path.isdir(str(cwd)):
        raise WindesktopctlError(f"cwd does not exist: {cwd}")
    _prune_jobs()
    jid = f"j{next(_job_seq)}"
    jdir = _jobs_dir()
    out_path = jdir / f"{jid}.out"
    err_path = jdir / f"{jid}.err"
    out_f = out_path.open("wb")
    err_f = err_path.open("wb")
    try:
        proc = _spawn(command, shell, cwd, env, out_f, err_f)
    finally:
        out_f.close()
        err_f.close()
    with _jobs_lock:
        _jobs[jid] = {
            "id": jid,
            "pid": proc.pid,
            "proc": proc,
            "command": command,
            "shell": shell,
            "started": time.time(),
            "finished": None,
            "status": "running",
            "exit_code": None,
            "dir": str(jdir),
            "cursors": {"out": 0, "err": 0},
        }
    # watcher thread: record exit status
    def _watch(j):
        code = j["proc"].wait()
        with _jobs_lock:
            j["status"] = "exited"
            j["exit_code"] = code
            j["finished"] = time.time()

    threading.Thread(target=_watch, args=(_jobs[jid],), daemon=True).start()
    return f"job_id={jid} pid={proc.pid} shell={shell}"


def _get_job(job_id: str) -> dict:
    jid = str(job_id).strip()
    with _jobs_lock:
        j = _jobs.get(jid)
    if j is None:
        raise WindesktopctlError(
            f"unknown job {job_id!r} (jobs do not survive a server restart)"
        )
    return j


def _job_status(j: dict) -> str:
    if j["status"] == "running":
        return "running"
    return f"exited code={j['exit_code']}"


def job_output(job_id: str, max_bytes: int = 65536) -> str:
    """New output since the last job_output call for this job (per-stream
    server-side cursor), plus status and elapsed time."""
    _require_shell()
    j = _get_job(job_id)
    max_bytes = max(256, min(int(max_bytes), 4 * 1024 * 1024))
    budget = max_bytes
    sections = []
    new_cursors = dict(j["cursors"])
    for suffix, ckey, label in ((".out", "out", "stdout"), (".err", "err", "stderr")):
        path = Path(j["dir"], j["id"] + suffix)
        cur = j["cursors"][ckey]
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size <= cur:
            continue
        take = min(budget, size - cur)
        if take <= 0:
            break
        with path.open("rb") as f:
            f.seek(cur)
            data = f.read(take)
        new_cursors[ckey] = cur + len(data)
        budget -= len(data)
        text = data.decode("utf-8", errors="replace")
        sections.append(f"--- {label} (new {len(data)} bytes) ---\n{text}")
    with _jobs_lock:
        j["cursors"].update(new_cursors)
    elapsed = (j["finished"] or time.time()) - j["started"]
    header = (
        f"job {j['id']} status={_job_status(j)} elapsed={elapsed:.1f}s "
        f"offsets out={new_cursors['out']} err={new_cursors['err']}"
    )
    body = "\n".join(sections) if sections else "(no new output)"
    return header + "\n" + body


def job_kill(job_id: str) -> str:
    """Kill a job's whole process tree; returns the final status."""
    _require_shell()
    j = _get_job(job_id)
    if j["status"] == "running":
        _kill_tree(j["pid"])
        try:
            j["proc"].wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass
    with _jobs_lock:
        if j["status"] == "running":
            j["status"] = "exited"
            j["exit_code"] = j["proc"].poll()
            j["finished"] = time.time()
    return f"job {j['id']}: killed; final status {_job_status(j)}"


def job_list() -> str:
    """One line per known job: id, status, started, command (80 chars)."""
    _require_shell()
    _prune_jobs()
    with _jobs_lock:
        jobs = list(_jobs.values())
    if not jobs:
        return "(no jobs; jobs do not survive a server restart)"
    lines = []
    for j in sorted(jobs, key=lambda x: x["id"]):
        started = time.strftime("%H:%M:%S", time.localtime(j["started"]))
        cmd = " ".join(str(j["command"]).split())[:80]
        lines.append(
            f"{j['id']} {_job_status(j):<18} started={started} cmd={cmd}"
        )
    return "\n".join(lines)


def job_meta(job_id: str) -> dict | None:
    """Internal: registry entry for audit logging."""
    try:
        return _get_job(job_id)
    except WindesktopctlError:
        return None
