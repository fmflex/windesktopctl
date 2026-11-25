"""Bearer-token management for HTTP serving.

Source order: WINDESKTOPCTL_TOKEN env var wins (static); otherwise the token
lives in ``<state_dir>/token`` - created on first need with
``secrets.token_urlsafe(32)`` and ACL-restricted to the current user so other
accounts on the machine can't read it. ``token --rotate`` rewrites the file;
the bearer middleware stats the file per request, so rotation takes effect
without a restart.
"""

from __future__ import annotations

import getpass
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import state as st

ENV_VAR = "WINDESKTOPCTL_TOKEN"


def token_path() -> Path:
    return st.state_dir() / "token"


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
    if domain and user:
        return f"{domain}\\{user}"
    try:
        out = subprocess.run(
            ["whoami"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0 and "\\" in out.stdout:
            return out.stdout.strip()
    except Exception:
        pass
    return user or "SYSTEM"


def _restrict_acl(path: Path) -> str | None:
    """icacls: drop inheritance, grant only the current user. Returns a
    warning string on failure, None on success."""
    who = _current_user()
    try:
        r = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{who}:F",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0:
            return f"icacls failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:200]}"
        # verify the ACL actually took
        v = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True, timeout=15
            )
        if v.returncode != 0 or who.lower() not in v.stdout.lower():
            return "icacls ran but the expected user grant was not found"
        return None
    except Exception as ex:
        return f"icacls error: {type(ex).__name__}: {ex}"


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="ascii")
    warn = _restrict_acl(path)
    if warn:
        print(f"windesktopctl: warning - token file ACL not restricted: {warn}",
              file=sys.stderr, flush=True)


def ensure_token() -> tuple[str, Path, bool]:
    """Return (token, path, created). Env var wins; file is created on first
    need. ``created`` is True only when this call generated the file token."""
    env = os.environ.get(ENV_VAR, "").strip()
    path = token_path()
    if env:
        return env, path, False
    try:
        tok = path.read_text(encoding="ascii").strip()
        if tok:
            return tok, path, False
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    _write_token(path, tok)
    return tok, path, True


def rotate_token() -> tuple[str, Path]:
    """Generate a fresh file token (env var still overrides at runtime)."""
    path = token_path()
    tok = secrets.token_urlsafe(32)
    _write_token(path, tok)
    return tok, path


def masked(token: str) -> str:
    t = token or ""
    return f"{t[:4]}…" if len(t) > 4 else "…"


class TokenProvider:
    """Callable returning the current bearer token. Re-stats the token file
    on every call so `token --rotate` takes effect without a server restart.
    WINDESKTOPCTL_TOKEN (when set) is static and skips the file entirely."""

    def __init__(self, env_var: str = ENV_VAR):
        self._env_var = env_var
        self._token: str | None = None
        self._mtime_ns: int | None = None

    def __call__(self) -> str:
        return self.token()

    def token(self) -> str:
        env = os.environ.get(self._env_var, "").strip()
        if env:
            return env
        p = token_path()
        try:
            mtime = p.stat().st_mtime_ns
        except OSError:
            mtime = None
        if self._token is None or mtime != self._mtime_ns:
            tok, _path, _created = ensure_token()
            self._token = tok
            try:
                self._mtime_ns = p.stat().st_mtime_ns
            except OSError:
                self._mtime_ns = None
        return self._token or ""


def token_age_seconds() -> float | None:
    try:
        return time.time() - token_path().stat().st_mtime
    except OSError:
        return None
