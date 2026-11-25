"""Unit tests for core/shell.py - exec capture/timeout/jobs + gates."""

import os
import time
from types import SimpleNamespace

import pytest

from windesktopctl.core import WindesktopctlError
from windesktopctl.core import shell


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "wdhome"
    home.mkdir()
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(home))

    def set_config(enabled=True):
        (home / "config.toml").write_text(
            f"shell_enabled = {'true' if enabled else 'false'}\n"
            "file_tools_enabled = false\nfile_roots = []\n",
            encoding="utf-8",
        )

    set_config()
    with shell._jobs_lock:
        shell._jobs.clear()
    return SimpleNamespace(home=home, set_config=set_config)


def _wait_job(jid, timeout=10.0):
    """Poll job_list until THIS job exits (job_list does not consume the
    job_output cursor; match the jid at line start to avoid prefix hits)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for line in shell.job_list().splitlines():
            if line.startswith(jid + " ") and "exited" in line:
                return
        time.sleep(0.1)
    raise AssertionError(f"job {jid} did not exit")


# ----------------------------------------------------------------- exec --

def test_exec_cmd_basic(env):
    out = shell.exec_cmd("echo hello-wd", shell="cmd")
    assert "exit_code=0" in out
    assert "hello-wd" in out
    assert "--- stdout ---" in out


def test_exec_powershell(env):
    out = shell.exec_cmd("Write-Output ps-hi", shell="powershell", timeout_s=60)
    assert "exit_code=0" in out and "ps-hi" in out


def test_exec_stderr_and_exit_code(env):
    out = shell.exec_cmd("echo oops 1>&2 & exit /b 3", shell="cmd")
    assert "exit_code=3" in out
    assert "oops" in out and "--- stderr ---" in out


def test_exec_env_and_cwd(env, tmp_path):
    work = tmp_path / "workdir"
    work.mkdir()
    out = shell.exec_cmd(
        "echo %WDCTL_VAR% & cd", shell="cmd", cwd=str(work),
        env={"WDCTL_VAR": "env-ok"},
    )
    assert "env-ok" in out
    assert str(work).lower() in out.lower()


def test_exec_default_cwd_is_home(env):
    # no cwd -> user's home, never the server's own cwd
    home = os.path.expanduser("~")
    out = shell.exec_cmd("cd", shell="cmd")
    assert "exit_code=0" in out
    assert os.path.normcase(home).lower() in out.lower()


def test_exec_missing_cwd(env):
    with pytest.raises(WindesktopctlError, match="cwd does not exist"):
        shell.exec_cmd("echo x", shell="cmd", cwd=str(env.home / "nope"))


def test_exec_bad_shell(env):
    with pytest.raises(WindesktopctlError, match=r"powershell\|cmd"):
        shell.exec_cmd("echo x", shell="bash")


def test_exec_timeout_kills(env):
    t0 = time.monotonic()
    out = shell.exec_cmd("ping -n 20 127.0.0.1 >nul", shell="cmd", timeout_s=2)
    dur = time.monotonic() - t0
    assert "timed out after 2s (process tree killed)" in out
    assert dur < 15  # didn't wait for the full ping


def test_exec_output_caps(env):
    # ~180KB of output -> head+tail kept, middle elided
    out = shell.exec_cmd(
        "1..3000 | ForEach-Object { 'x' * 60 }", shell="powershell", timeout_s=60
    )
    assert "bytes truncated" in out


def test_exec_disabled_config(env):
    env.set_config(enabled=False)
    with pytest.raises(WindesktopctlError, match="disabled in config.toml"):
        shell.exec_cmd("echo x", shell="cmd")


def test_exec_paused(env):
    (env.home / "paused").write_text("x")
    with pytest.raises(WindesktopctlError, match="paused"):
        shell.exec_cmd("echo x", shell="cmd")


# ----------------------------------------------------------------- jobs --

def test_job_lifecycle(env):
    res = shell.start_job("echo job-marker-123", shell="cmd")
    assert "job_id=j" in res and "pid=" in res
    jid = res.split()[0].split("=")[1]
    out = shell.job_output(jid)
    first = out
    _wait_job(jid)
    out = shell.job_output(jid)
    assert "job-marker-123" in first + out
    assert "exited code=0" in out.splitlines()[0]
    lst = shell.job_list()
    assert jid in lst and "job-marker" in lst


def test_job_output_incremental_cursor(env):
    res = shell.start_job(
        "echo first-line & ping -n 3 127.0.0.1 >nul & echo second-line",
        shell="cmd",
    )
    jid = res.split()[0].split("=")[1]
    time.sleep(0.5)
    out1 = shell.job_output(jid)
    # first call returns whatever is available; second call must not repeat it
    time.sleep(3.0)
    out2 = shell.job_output(jid)
    assert "second-line" in out2 or "exited" in out2.splitlines()[0]
    # a third call with nothing new reports no new output (or only status)
    out3 = shell.job_output(jid)
    assert "no new output" in out3 or "bytes" in out3


def test_job_kill(env):
    res = shell.start_job("ping -n 60 127.0.0.1 >nul", shell="cmd")
    jid = res.split()[0].split("=")[1]
    time.sleep(0.4)
    out = shell.job_kill(jid)
    assert "killed" in out and "exited" in out
    # killed job stays queryable
    assert jid in shell.job_list()


def test_job_unknown(env):
    with pytest.raises(WindesktopctlError, match="unknown job"):
        shell.job_output("j999")


def test_job_output_files_written(env):
    res = shell.start_job("echo file-check", shell="cmd")
    jid = res.split()[0].split("=")[1]
    _wait_job(jid)
    jdir = env.home / "jobs"
    assert (jdir / f"{jid}.out").exists()
    assert b"file-check" in (jdir / f"{jid}.out").read_bytes()


def test_jobs_disabled_config(env):
    env.set_config(enabled=False)
    with pytest.raises(WindesktopctlError, match="disabled in config.toml"):
        shell.start_job("echo x", shell="cmd")
