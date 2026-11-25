"""Tests for core/upstreams.py - config parsing, deny logic, formatting,
audit fields, pause/disabled refusals, reconnect FSM, and live in-process
upstream integration (HTTP + stdio + jsonrpc-http)."""

import json
import os
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from windesktopctl.core import WindesktopctlError, config as cfg, upstreams as ups

TINY = os.path.join(os.path.dirname(__file__), "_tiny_mcp_server.py")


def _write_config(home, text):
    (home / "config.toml").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    home = tmp_path / "uhome"
    home.mkdir()
    monkeypatch.setenv("WINDESKTOPCTL_HOME", str(home))
    with ups._managed_lock:
        ups._managed.clear()
    ups._sessions.clear()
    ups._last_error.clear()
    yield SimpleNamespace(home=home)
    try:
        ups._run(ups._drop_all(), timeout=15)
    except Exception:
        pass


# ------------------------------------------------------------- config parse --

def test_mcp_upstreams_parse(env):
    _write_config(env.home, """
[mcp_upstreams.ida]
url = "http://127.0.0.1:13337/mcp"
enabled = true
allow_unsafe = false
deny_tools = ["extra_bad"]
headers = { "X-Test" = "1" }

[mcp_upstreams.headless]
url = "http://127.0.0.1:8745/mcp"
start_command = ["python", "-m", "srv", "db.i64"]
start_if_unreachable = "ida"
start_timeout_s = 60
transport = "jsonrpc-http"

[mcp_upstreams.local]
command = ["python", "-m", "srv"]
env = { "FOO" = "bar" }
cwd = "C:\\\\tmp"
enabled = false

[mcp_upstreams.broken]
enabled = true
""")
    u = cfg.mcp_upstreams()
    assert set(u) == {"ida", "headless", "local"}  # 'broken' skipped (no url/command)
    ida = u["ida"]
    assert ida["kind"] == "http" and ida["url"].endswith("/mcp")
    assert ida["enabled"] and not ida["allow_unsafe"]
    assert {"py_eval", "py_exec_file", "extra_bad"} <= ida["deny"]
    assert ida["headers"] == {"X-Test": "1"}
    assert ida["transport"] == "streamable-http"
    h = u["headless"]
    assert h["start_if_unreachable"] == "ida" and h["start_timeout_s"] == 60
    assert h["start_command"][:3] == ["python", "-m", "srv"]
    assert h["transport"] == "jsonrpc-http"
    loc = u["local"]
    assert loc["kind"] == "stdio" and loc["command"][0] == "python"
    assert loc["env"] == {"FOO": "bar"} and loc["cwd"].endswith("tmp")
    assert loc["enabled"] is False


def test_mcp_upstreams_shutdown_fields(env):
    _write_config(env.home, """
[mcp_upstreams.w]
url = "http://x/mcp"
shutdown_tool = "idb_open"
shutdown_args = { input_path = 'C:\\db.i64', idle_ttl_sec = 10 }
""")
    w = cfg.mcp_upstreams()["w"]
    assert w["shutdown_tool"] == "idb_open"
    assert w["shutdown_args"]["idle_ttl_sec"] == 10
    assert w["shutdown_args"]["input_path"].endswith("db.i64")
    # defaults when absent
    _write_config(env.home, '[mcp_upstreams.y]\nurl = "http://x/mcp"\n')
    y = cfg.mcp_upstreams()["y"]
    assert y["shutdown_tool"] is None and y["shutdown_args"] == {}


def test_deny_logic(env):
    _write_config(env.home, """
[mcp_upstreams.ida]
url = "http://x/mcp"
allow_unsafe = false

[mcp_upstreams.ida2]
url = "http://x/mcp"
allow_unsafe = true

[mcp_upstreams.other]
url = "http://x/mcp"
unsafe_tools = ["custom_unsafe"]
deny_tools = ["always_bad"]

[mcp_upstreams.plain]
url = "http://x/mcp"
""")
    u = cfg.mcp_upstreams()
    # ida*: builtins denied unless allow_unsafe
    assert {"py_eval", "py_exec_file"} <= u["ida"]["deny"]
    assert "py_eval" not in u["ida2"]["deny"]
    # non-ida: no builtins, but unsafe_tools + deny_tools apply
    assert "custom_unsafe" in u["other"]["deny"]
    assert "always_bad" in u["other"]["deny"]
    assert "py_eval" not in u["other"]["deny"]
    # deny_tools still applies with allow_unsafe=true
    _write_config(env.home, """
[mcp_upstreams.x]
url = "http://x/mcp"
allow_unsafe = true
deny_tools = ["still_bad"]
""")
    u = cfg.mcp_upstreams()
    assert u["x"]["deny"] == {"still_bad"}


# ----------------------------------------------------------- tools_list fmt --

def _fake_tools_list_run(monkeypatch, tools, cfg_dict):
    sess = SimpleNamespace(tools=tools, broken=False, last_error=None)

    def fake_run(coro, timeout):
        coro.close()  # _tools_raw never executes; avoid "never awaited"
        return (cfg_dict, sess)

    monkeypatch.setattr(ups, "_run", fake_run)
    return sess


def test_tools_list_filter_and_denied(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
deny_tools = ["bad_tool"]
""")
    c = cfg.mcp_upstreams()["srv"]
    tools = [
        {"name": "good_one", "description": "does good things",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "bad_tool", "description": "denied", "inputSchema": {}},
        {"name": "other_good", "description": "unrelated", "inputSchema": {}},
    ]
    _fake_tools_list_run(monkeypatch, tools, c)
    out = ups.tools_list("srv")
    assert "good_one" in out and "other_good" in out
    assert "bad_tool" not in out                      # hidden by default
    out = ups.tools_list("srv", include_denied=True)
    assert "bad_tool [denied]" in out
    out = ups.tools_list("srv", filter="GOOD")
    assert "good_one" in out and "other_good" in out and "bad_tool" not in out
    out = ups.tools_list("srv", filter="does good")
    assert "good_one" in out and "other_good" not in out
    assert "schema=" in out


def test_tools_list_schema_and_desc_cap(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
""")
    c = cfg.mcp_upstreams()["srv"]
    tools = [{"name": "t", "description": "x" * 500, "inputSchema": {"a": 1}}]
    _fake_tools_list_run(monkeypatch, tools, c)
    out = ups.tools_list("srv")
    assert "x" * 305 not in out and "..." in out
    assert '"a":1' in out  # compact json


# --------------------------------------------------------- result formatting --

def test_format_call_result_full(env):
    res = SimpleNamespace(
        isError=False,
        content=[
            {"type": "text", "text": "hello"},
            {"type": "image", "data": "aGk=", "mimeType": "image/png"},
            {"type": "resource", "resource": {
                "uri": "file:///x.txt", "mimeType": "text/plain", "text": "res-body"}},
            {"type": "resource", "resource": {
                "uri": "file:///b.bin", "mimeType": "application/octet-stream",
                "blob": "AAAA"}},
        ],
        structuredContent={"k": [1, 2]},
    )
    out, nxt = ups.format_call_result(res)
    assert "hello" in out
    assert "structuredContent" in out and '"k":[1,2]' in out
    assert "image/png" in out and "saved:" in out and nxt == 2
    assert "res-body" in out and "resource file:///x.txt" in out
    assert "b.bin" in out and "binary" in out
    # saved image exists under state_dir/upstreams
    import re
    m = re.search(r"saved: (.+)", out)
    assert m and os.path.exists(m.group(1).strip())


def test_format_call_result_iserror_and_empty(env):
    res = SimpleNamespace(isError=True, content=[{"type": "text", "text": "boom"}],
                          structuredContent=None)
    out, _ = ups.format_call_result(res)
    assert out.startswith("upstream error:") and "boom" in out
    out, _ = ups.format_call_result(SimpleNamespace(content=[], isError=False))
    assert "empty result" in out


# ------------------------------------------------------------- call + audit --

def _fake_call_raw(monkeypatch, result=None, exc=None):
    """Patch _call_raw with a real coroutine so call_tool's _run executes it
    on the loop thread (exceptions propagate like real upstream failures)."""
    async def fake_raw(name, tool, args, timeout_s):
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(ups, "_call_raw", fake_raw)


def test_call_tool_audit_fields(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
""")
    res = SimpleNamespace(isError=False, content=[{"type": "text", "text": "ok"}],
                          structuredContent=None)
    _fake_call_raw(monkeypatch, result=res)
    text, meta = ups.call_tool("srv", "my_tool", {"secret": "arg-value"})
    assert "ok" in text
    assert meta["server"] == "srv" and meta["upstream_tool"] == "my_tool"
    assert meta["is_error"] is False and "duration_s" in meta
    assert meta["arg_bytes"] > 0
    assert "secret" not in json.dumps(meta) and "arg-value" not in json.dumps(meta)


def test_call_tool_iserror_flag(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
""")
    res = SimpleNamespace(isError=True, content=[{"type": "text", "text": "nope"}],
                          structuredContent=None)
    _fake_call_raw(monkeypatch, result=res)
    text, meta = ups.call_tool("srv", "t", {})
    assert meta["is_error"] is True


def test_call_tool_transport_error_verbatim(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
""")
    _fake_call_raw(monkeypatch, exc=ups.UpstreamCallError("connection reset by peer"))
    text, meta = ups.call_tool("srv", "t", {})
    assert "connection reset by peer" in text and meta["is_error"] is True


def test_call_tool_denied_and_disabled(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
deny_tools = ["py_eval"]

[mcp_upstreams.off]
url = "http://x/mcp"
enabled = false
""")
    connects = []

    async def spy_connect(c):
        connects.append(c["name"])
        raise AssertionError("must not reach connect stage")

    monkeypatch.setattr(ups, "_connect", spy_connect)
    # real _call_raw runs on the loop: denials raise before _ensure/_connect
    with pytest.raises(WindesktopctlError, match="denied"):
        ups.call_tool("srv", "py_eval", {})
    with pytest.raises(WindesktopctlError, match="disabled"):
        ups.call_tool("off", "anything", {})
    with pytest.raises(WindesktopctlError, match="unknown MCP upstream"):
        ups.call_tool("nope", "x", {})
    assert connects == []  # refused before any upstream I/O


def test_pause_blocks(env):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://x/mcp"
""")
    from windesktopctl.core import safety
    safety.pause()
    try:
        with pytest.raises(WindesktopctlError, match="paused"):
            ups.servers_status()
        with pytest.raises(WindesktopctlError, match="paused"):
            ups.call_tool("srv", "x", {})
    finally:
        safety.resume()


# ------------------------------------------------------------- connect FSM --

def test_ensure_retries_connect_once(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://127.0.0.1:1/mcp"
""")
    calls = []

    async def fake_connect(c):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(ups, "_connect", fake_connect)
    with pytest.raises(WindesktopctlError, match="cannot connect"):
        ups._run(ups._ensure("srv"), timeout=15)
    assert len(calls) == 2  # initial + exactly one retry
    assert "srv" in ups._last_error


def test_ensure_broken_session_reconnects(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.srv]
url = "http://127.0.0.1:1/mcp"
""")
    connects = []

    async def fake_connect(c):
        connects.append(1)
        return SimpleNamespace(stack=None, client=object(), tools=[], broken=False,
                               last_error=None)

    monkeypatch.setattr(ups, "_connect", fake_connect)
    c, s1 = ups._run(ups._ensure("srv"), timeout=15)
    assert len(connects) == 1
    # healthy session is reused - no reconnect
    c, s2 = ups._run(ups._ensure("srv"), timeout=15)
    assert s2 is s1 and len(connects) == 1
    # broken session -> torn down + reconnected
    s1.broken = True
    c, s3 = ups._run(ups._ensure("srv"), timeout=15)
    assert s3 is not s1 and len(connects) == 2


def test_spawn_flags_console_dependent(env, monkeypatch):
    # with a console: child shares it (no NO_WINDOW) so CTRL_C can reach it
    monkeypatch.setattr(ups, "_has_console", lambda: True)
    assert ups._spawn_flags() == ups.CREATE_NEW_PROCESS_GROUP
    # console-less: NO_WINDOW so no visible console pops up; graceful stop
    # then degrades to the timed taskkill fallback
    monkeypatch.setattr(ups, "_has_console", lambda: False)
    assert ups._spawn_flags() == (
        ups.CREATE_NEW_PROCESS_GROUP | ups.CREATE_NO_WINDOW
    )


def test_autostart_guard_refuses_when_gui_up(env, monkeypatch):
    _write_config(env.home, """
[mcp_upstreams.ida]
url = "http://127.0.0.1:13337/mcp"

[mcp_upstreams.headless]
url = "http://127.0.0.1:9/mcp"
start_command = ["python", "-c", "pass"]
start_if_unreachable = "ida"
""")
    async def fake_reach(c):
        return c["name"] == "ida"  # GUI reachable, headless not

    monkeypatch.setattr(ups, "_reachable", fake_reach)
    with pytest.raises(WindesktopctlError, match="ida.*running"):
        ups._run(ups._ensure("headless"), timeout=15)
    assert not ups._managed  # nothing was spawned


# --------------------------------------------------------- jsonrpc stub http --


class _JsonRpcStub:
    """Minimal plain-JSON-RPC /mcp endpoint (zeromcp-style, no SSE)."""

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Mcp-Session-Id", "stub-sess")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                method = req.get("method")
                if method == "initialize":
                    self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                                     "result": {"protocolVersion": "2025-06-18",
                                                "capabilities": {}, "serverInfo": {"name": "stub", "version": "0"}}})
                elif method and method.startswith("notifications/"):
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                elif method == "tools/list":
                    self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                                     "result": {"tools": [
                                         {"name": "echo", "description": "echo it",
                                          "inputSchema": {"type": "object"}},
                                         {"name": "py_eval", "description": "unsafe",
                                          "inputSchema": {"type": "object"}},
                                     ]}})
                elif method == "tools/call":
                    p = req.get("params") or {}
                    if p.get("name") == "py_eval":
                        res = {"content": [{"type": "text", "text": "eval denied upstream"}],
                               "isError": True}
                    else:
                        res = {"content": [{"type": "text",
                                            "text": "echo:" + str((p.get("arguments") or {}).get("text", ""))}],
                               "structuredContent": {"ok": True}, "isError": False}
                    self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                                     "result": res})
                else:
                    self._send(200, {"jsonrpc": "2.0", "id": req.get("id"),
                                     "error": {"code": -32601, "message": "method not found"}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ------------------------------------------------------------- integration --

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start_tiny_http(port):
    return subprocess.Popen(
        [sys.executable, TINY, "--http", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_port(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def test_integration_http_upstream(env):
    port = _free_port()
    proc = _start_tiny_http(port)
    try:
        assert _wait_port(port), "tiny server did not start"
        _write_config(env.home, f"""
[mcp_upstreams.tiny]
url = "http://127.0.0.1:{port}/mcp"
""")
        out = ups.tools_list("tiny")
        assert "echo" in out and "make_image" in out and "add" in out
        text, meta = ups.call_tool("tiny", "echo", {"text": "hi"})
        assert "echo:hi" in text and meta["is_error"] is False
        # persistent session reuse: second call works
        text2, _ = ups.call_tool("tiny", "add", {"a": 2, "b": 3})
        assert "5" in text2
        # image tool -> saved file metadata
        text3, _ = ups.call_tool("tiny", "make_image", {})
        assert "image/png" in text3 and "saved:" in text3
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_integration_reconnect_after_kill(env):
    port = _free_port()
    proc = _start_tiny_http(port)
    try:
        assert _wait_port(port)
        _write_config(env.home, f"""
[mcp_upstreams.tiny]
url = "http://127.0.0.1:{port}/mcp"
""")
        ups.call_tool("tiny", "echo", {"text": "one"})
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(0.5)
        # first call after kill: transport error, session marked broken
        text, meta = ups.call_tool("tiny", "echo", {"text": "two"})
        assert meta["is_error"] is True or "error" in text.lower()
        # restart upstream on same port -> next call reconnects
        proc2 = _start_tiny_http(port)
        try:
            assert _wait_port(port)
            text, meta = ups.call_tool("tiny", "echo", {"text": "three"})
            assert "echo:three" in text and meta["is_error"] is False
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=10)
            except Exception:
                proc2.kill()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_integration_stdio_upstream(env):
    _write_config(env.home, f"""
[mcp_upstreams.tinystdio]
command = [{json.dumps(sys.executable)}, {json.dumps(TINY)}]
""")
    out = ups.tools_list("tinystdio")
    assert "echo" in out and "add" in out
    text, meta = ups.call_tool("tinystdio", "echo", {"text": "stdio-ok"})
    assert "echo:stdio-ok" in text


def test_integration_jsonrpc_http(env):
    stub = _JsonRpcStub()
    try:
        _write_config(env.home, f"""
[mcp_upstreams.stub]
url = "http://127.0.0.1:{stub.port}/mcp"
transport = "jsonrpc-http"
""")
        out = ups.tools_list("stub")
        assert "echo" in out and "py_eval" in out
        text, meta = ups.call_tool("stub", "echo", {"text": "jr"})
        assert "echo:jr" in text and '"ok":true' in text
        # upstream isError passes through with prefix
        text, meta = ups.call_tool("stub", "py_eval", {"code": "1"})
        assert "eval denied upstream" in text
    finally:
        stub.close()
