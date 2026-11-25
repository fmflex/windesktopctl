"""Unit tests for mcp_server.py - WINDESKTOPCTL_TOOLS filter + host allowlist."""

import asyncio

import pytest

from windesktopctl.mcp_server import _http_allowed_hosts, create_server


def _tool_names(server) -> set[str]:
    async def go():
        tools = await server.list_tools()
        return {t.name for t in tools}
    return asyncio.run(go())


_BASE_TOOLS = {
    "observe", "screenshot", "act", "windows", "focus", "wait_for",
    "window", "launch", "browser", "challenge", "challenge_answer",
    "pause", "resume", "status",
}
_NEW_TOOLS = {
    "exec", "job_output", "job_kill", "job_list", "read_file", "write_file",
    "edit_file", "list_dir", "glob", "grep", "file_info", "download_chunk",
    "upload_chunk", "mcp_servers", "mcp_tools", "mcp_call",
}
_WEAK_TOKEN = "a" * 16 + "b" * 16  # 32 chars but only 2 distinct -> weak
_GOOD_TOKEN = "0123456789abcdef" * 2  # 32 chars, 16 distinct -> strong


def test_all_tools_by_default(monkeypatch):
    monkeypatch.delenv("WINDESKTOPCTL_TOOLS", raising=False)
    names = _tool_names(create_server())
    assert names == _BASE_TOOLS | _NEW_TOOLS


def test_http_weak_token_hides_new_tools(capsys):
    srv = create_server(allowed_hosts=["127.0.0.1"], token_provider=lambda: _WEAK_TOKEN)
    names = _tool_names(srv)
    assert names == _BASE_TOOLS
    assert names.isdisjoint(_NEW_TOOLS)
    assert "token too weak" in capsys.readouterr().err


def test_http_missing_token_provider_hides_new_tools(capsys):
    srv = create_server(allowed_hosts=["127.0.0.1"], token_provider=None)
    assert _tool_names(srv) == _BASE_TOOLS
    assert "token too weak" in capsys.readouterr().err


def test_http_strong_token_registers_new_tools():
    srv = create_server(allowed_hosts=["127.0.0.1"], token_provider=lambda: _GOOD_TOKEN)
    names = _tool_names(srv)
    assert names == _BASE_TOOLS | _NEW_TOOLS


def test_stdio_registers_new_tools_without_token():
    """stdio mode (local Devin) has no bearer token - new tools register;
    config.toml + pause still gate every call."""
    names = _tool_names(create_server())
    assert _NEW_TOOLS <= names


def test_new_tools_respect_allowlist():
    names = _tool_names(create_server(tool_names={"exec", "read_file", "status"}))
    assert names == {"exec", "read_file", "status"}


def test_tools_allowlist_arg():
    names = _tool_names(create_server(tool_names={"windows", "status"}))
    assert names == {"windows", "status"}


def test_tools_env_filter(monkeypatch):
    monkeypatch.setenv("WINDESKTOPCTL_TOOLS", "observe, act ,bogus")
    names = _tool_names(create_server())
    assert names == {"observe", "act"}


def test_http_allowed_hosts_defaults():
    hosts = _http_allowed_hosts("127.0.0.1", [])
    assert "127.0.0.1" in hosts and "127.0.0.1:*" in hosts
    assert "localhost" in hosts and "localhost:*" in hosts
    # no duplicates
    assert len(hosts) == len(set(hosts))


def test_http_allowed_hosts_bind_and_extra():
    hosts = _http_allowed_hosts("0.0.0.0", ["tunnel.example.com:*"])
    assert "0.0.0.0:*" in hosts
    assert "tunnel.example.com:*" in hosts


def _req(host: str, origin: str | None = None):
    from starlette.requests import Request

    headers = [(b"host", host.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8765),
        }
    )


def test_transport_security_host_validation():
    """mcp's TransportSecurityMiddleware (used by streamable_http_app via
    transport_security=) rejects evil Host headers with 421."""
    from mcp.server.transport_security import (
        TransportSecurityMiddleware,
        TransportSecuritySettings,
    )

    mw = TransportSecurityMiddleware(
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=_http_allowed_hosts("127.0.0.1", []),
        )
    )

    def check(host):
        return asyncio.run(mw.validate_request(_req(host)))

    assert check("127.0.0.1:8765") is None
    assert check("localhost:8765") is None
    assert check("localhost") is None
    r = check("evil.example.com")
    assert r is not None and r.status_code == 421
    r = check("127.0.0.1.evil.com")
    assert r is not None and r.status_code == 421


# ------------------------------------------------------------- audit log --


def _fake_ctx(ip):
    class Addr:
        host = ip

    class Req:
        client = Addr()

    class RC:
        request = Req()

    class Ctx:
        request_context = RC()

    return Ctx()


def test_client_ip_from_ctx():
    from windesktopctl.mcp_server import _client_ip

    assert _client_ip(_fake_ctx("203.0.113.9")) == "203.0.113.9"
    assert _client_ip(None) == "stdio"

    class Empty:
        request_context = None

    assert _client_ip(Empty()) == "stdio"


def test_audit_log_fields_and_no_content(tmp_path, monkeypatch):
    import json as _json

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(home))

    from windesktopctl.mcp_server import _audit

    _audit(_fake_ctx("198.51.100.7"), "exec",
           command="echo secret-content", shell="cmd", exit_code=0)
    _audit(None, "read_file", path="C:/roots/f.txt")

    lines = (home / "audit.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    r1 = _json.loads(lines[0])
    assert r1["client"] == "198.51.100.7"
    assert r1["tool"] == "exec"
    assert r1["command"] == "echo secret-content"
    assert r1["exit_code"] == 0
    assert "ts" in r1
    r2 = _json.loads(lines[1])
    assert r2["client"] == "stdio"
    assert r2["tool"] == "read_file"
    assert r2["path"] == "C:/roots/f.txt"
    # never file contents / env / token
    assert "content" not in r1 and "env" not in r1 and "token" not in r1
