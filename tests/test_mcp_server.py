"""Unit tests for mcp_server.py - WINDESKTOPCTL_TOOLS filter + host allowlist."""

import asyncio

import pytest

from windesktopctl.mcp_server import _http_allowed_hosts, create_server


def _tool_names(server) -> set[str]:
    async def go():
        tools = await server.list_tools()
        return {t.name for t in tools}
    return asyncio.run(go())


def test_all_tools_by_default(monkeypatch):
    monkeypatch.delenv("WINDESKTOPCTL_TOOLS", raising=False)
    names = _tool_names(create_server())
    assert names == {
        "observe", "screenshot", "act", "windows", "focus", "wait_for",
        "window", "launch", "browser", "challenge", "challenge_answer",
        "pause", "resume", "status",
    }


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
