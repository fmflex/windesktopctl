"""Unit tests for bearer-token management (file, ACL, rotate, live reload)."""

from __future__ import annotations

import os
import time

import pytest

from windesktopctl.core import token as tok


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "windesktopctl"
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(d))
    monkeypatch.delenv(tok.ENV_VAR, raising=False)
    return d


def test_ensure_token_creates_file(home):
    t, path, created = tok.ensure_token()
    assert created is True
    assert path == home / "token"
    assert path.read_text() == t
    assert len(t) >= 32  # token_urlsafe(32) -> ~43 chars


def test_ensure_token_reuses_file(home):
    t1, _, c1 = tok.ensure_token()
    t2, _, c2 = tok.ensure_token()
    assert c1 and not c2
    assert t1 == t2


def test_env_var_overrides_file(home, monkeypatch):
    monkeypatch.setenv(tok.ENV_VAR, "env-token-123")
    t, path, created = tok.ensure_token()
    assert t == "env-token-123"
    assert not created
    assert not path.exists()  # file never touched


def test_rotate_changes_token(home):
    t1, _ = tok.rotate_token()
    t2, path = tok.rotate_token()
    assert t1 != t2
    assert path.read_text() == t2


def test_masked():
    assert tok.masked("abcdef123456") == "abcd…"
    assert tok.masked("ab") == "…"


def test_acl_restricted_to_current_user(home, monkeypatch):
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "icacls" and "/grant:r" in cmd:
            return R(0)
        if cmd[0] == "icacls":
            return R(0, out="BUILTIN\\Administrators:(F)\n")
        return R(1)

    # make _current_user deterministic
    monkeypatch.setattr(tok.os, "environ", {**os.environ, "USERDOMAIN": "PC", "USERNAME": "me"})
    monkeypatch.setattr(tok.subprocess, "run", fake_run)
    t, path, _ = tok.ensure_token()
    assert t
    grant = [c for c in calls if "/grant:r" in c]
    assert grant and "PC\\me:F" in grant[0]
    assert any("/inheritance:r" in c for c in calls)


def test_provider_static_with_env(home, monkeypatch):
    monkeypatch.setenv(tok.ENV_VAR, "static-env")
    p = tok.TokenProvider()
    assert p.token() == "static-env"


def test_provider_reloads_on_mtime_change(home):
    tok.ensure_token()
    p = tok.TokenProvider()
    t1 = p.token()
    # rewrite the file with a new token + bumped mtime
    path = tok.token_path()
    path.write_text("rotated-token-value")
    ns = time.time_ns() + 5_000_000
    os.utime(path, ns=(ns, ns))
    t2 = p.token()
    assert t1 != t2
    assert t2 == "rotated-token-value"


def test_provider_same_token_without_change(home):
    tok.ensure_token()
    p = tok.TokenProvider()
    assert p.token() == p.token()
