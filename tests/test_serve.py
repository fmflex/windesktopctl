"""Unit tests for `windesktopctl serve` - tailscale command construction,
:443 occupancy check, banner/JSON shape. No real tailscale calls (mocked)."""

from __future__ import annotations

import json
import subprocess

import pytest

from windesktopctl import serve


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_port_443_configured_empty():
    assert serve._port_443_configured({}) is False
    assert serve._port_443_configured({"TCP": {"80": {}}, "Web": {}}) is False


def test_port_443_configured_tcp():
    assert serve._port_443_configured({"TCP": {"443": {"HTTPS": True}}}) is True


def test_port_443_configured_web_key():
    st = {"Web": {"mypc.example.ts.net:443": {"Handlers": {}}}}
    assert serve._port_443_configured(st) is True


def test_port_443_configured_allow_funnel():
    st = {"AllowFunnel": {"mypc.example.ts.net:443": True}}
    assert serve._port_443_configured(st) is True


def test_port_443_configured_raw_fallback():
    assert serve._port_443_configured({"_raw": "Web host:443 -> ..."}) is True
    assert serve._port_443_configured({"_raw": "TCP 80 only"}) is False


def _capture_ts(monkeypatch, rc=0, out="{}", err=""):
    calls = []

    def fake(*args, timeout=None, **kw):
        calls.append(list(args))
        return R(rc, out, err)

    monkeypatch.setattr(serve, "_ts", fake)
    return calls


def test_enable_tailnet_command(monkeypatch):
    calls = _capture_ts(monkeypatch)
    serve.enable_expose("tailnet", 8765)
    assert calls == [["serve", "--bg", "8765"]]


def test_enable_funnel_command(monkeypatch):
    calls = _capture_ts(monkeypatch)
    serve.enable_expose("funnel", 8765)
    assert calls == [["funnel", "--bg", "--yes", "8765"]]


def test_enable_failure_raises(monkeypatch):
    _capture_ts(monkeypatch, rc=1, err="boom")
    with pytest.raises(serve.ServeError):
        serve.enable_expose("tailnet", 8765)


_NOT_ENABLED_OUT = (
    "Serve is not enabled on your tailnet.\n"
    "To enable, visit:\n\n"
    "         https://login.tailscale.com/f/serve?node=abc123\n"
)


def test_enable_not_enabled_on_timeout(monkeypatch):
    """tailscale prints the enable URL then hangs -> surface the URL."""
    def fake(*args, timeout=None, **kw):
        raise subprocess.TimeoutExpired(
            args, timeout, output=_NOT_ENABLED_OUT, stderr=""
        )

    monkeypatch.setattr(serve, "_ts", fake)
    with pytest.raises(serve.ServeError) as ei:
        serve.enable_expose("tailnet", 8765)
    assert "not enabled" in str(ei.value)
    assert "https://login.tailscale.com/f/serve?node=abc123" in str(ei.value)


def test_enable_not_enabled_on_exit(monkeypatch):
    _capture_ts(monkeypatch, rc=1, err=_NOT_ENABLED_OUT)
    with pytest.raises(serve.ServeError) as ei:
        serve.enable_expose("funnel", 8765)
    assert "not enabled" in str(ei.value)
    assert "login.tailscale.com" in str(ei.value)


def test_enable_timeout_without_marker_is_generic(monkeypatch):
    def fake(*args, timeout=None, **kw):
        raise subprocess.TimeoutExpired(args, timeout, output="", stderr="")

    monkeypatch.setattr(serve, "_ts", fake)
    with pytest.raises(serve.ServeError) as ei:
        serve.enable_expose("tailnet", 8765)
    assert "timed out" in str(ei.value)


def test_disable_tailnet_command(monkeypatch):
    calls = _capture_ts(monkeypatch)
    monkeypatch.setattr(serve, "serve_status", lambda: {})
    w = serve.disable_expose("tailnet")
    assert w is None
    assert calls == [["serve", "--https=443", "off"]]


def test_disable_funnel_command(monkeypatch):
    calls = _capture_ts(monkeypatch)
    monkeypatch.setattr(serve, "serve_status", lambda: {})
    w = serve.disable_expose("funnel")
    assert w is None
    assert calls == [["funnel", "--https=443", "off"]]


def test_disable_warns_if_still_configured(monkeypatch):
    _capture_ts(monkeypatch)
    monkeypatch.setattr(
        serve, "serve_status", lambda: {"TCP": {"443": {}}}
    )
    w = serve.disable_expose("tailnet")
    assert w and "still be active" in w


def test_check_exposable_refuses_occupied_443(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda x: "/usr/bin/tailscale")
    monkeypatch.setattr(serve, "_ts", lambda *a, **k: R(0, "{}"))
    monkeypatch.setattr(
        serve, "serve_status", lambda: {"TCP": {"443": {"HTTPS": True}}}
    )
    err = serve.check_exposable("tailnet")
    assert err and ":443" in err


def test_check_exposable_ok_when_free(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda x: "/usr/bin/tailscale")
    monkeypatch.setattr(serve, "_ts", lambda *a, **k: R(0, "{}"))
    monkeypatch.setattr(serve, "serve_status", lambda: {})
    assert serve.check_exposable("tailnet") is None


def test_check_exposable_no_tailscale(monkeypatch):
    monkeypatch.setattr(serve.shutil, "which", lambda x: None)
    assert "not found" in serve.check_exposable("tailnet")


def test_mcp_json_shape():
    j = json.loads(serve._mcp_json("https://h.ts.net/mcp", "tok123"))
    srv = j["mcpServers"]["windesktopctl"]
    assert srv["url"] == "https://h.ts.net/mcp"
    assert srv["headers"]["Authorization"] == "Bearer tok123"


def test_dns_name_strips_dot(monkeypatch):
    monkeypatch.setattr(
        serve,
        "_ts_json",
        lambda *a: {"Self": {"DNSName": "rice.tail123.ts.net."}},
    )
    assert serve.dns_name() == "rice.tail123.ts.net"


def test_banner_masks_token(monkeypatch, capsys):
    monkeypatch.setattr(serve, "dns_name", lambda: "rice.tail123.ts.net")
    serve._banner("tailnet", 8765, "secret-token-value", show_token=False, copy=False)
    out = capsys.readouterr().out
    assert "https://rice.tail123.ts.net/mcp" in out
    assert "secr…" in out
    assert "secret-token-value" not in out
    assert "Bearer secr…" in out


def test_banner_show_token(monkeypatch, capsys):
    monkeypatch.setattr(serve, "dns_name", lambda: None)
    serve._banner("local", 8765, "secret-token-value", show_token=True, copy=False)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765/mcp" in out
    assert "Bearer secret-token-value" in out


def test_banner_funnel_warns(monkeypatch, capsys):
    monkeypatch.setattr(serve, "dns_name", lambda: "rice.tail123.ts.net")
    serve._banner("funnel", 8765, "tok", show_token=False, copy=False)
    out = capsys.readouterr().out
    assert "PUBLIC on the internet" in out
