"""Unit tests for the HTTP bearer middleware (Starlette TestClient + direct
ASGI scope calls for per-IP throttle tests)."""

import asyncio

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from windesktopctl.mcp_server import BearerAuthMiddleware


def _client(token="s3cret"):
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    return TestClient(BearerAuthMiddleware(app, token))


def test_401_without_token():
    r = _client().get("/")
    assert r.status_code == 401


def test_401_wrong_token():
    r = _client().get("/", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_401_malformed_header():
    r = _client().get("/", headers={"Authorization": "s3cret"})
    assert r.status_code == 401
    r = _client().get("/", headers={"Authorization": "Basic s3cret"})
    assert r.status_code == 401


def test_200_with_right_token():
    r = _client().get("/", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_live_reload_provider():
    """Token from a callable is re-read per request: rotate -> old 401,
    new 200, no restart."""
    box = {"tok": "old-token"}
    async def ok(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("ok")

    from starlette.applications import Starlette
    from starlette.routing import Route

    app = Starlette(routes=[Route("/", ok)])
    client = TestClient(BearerAuthMiddleware(app, lambda: box["tok"]))
    assert client.get("/", headers={"Authorization": "Bearer old-token"}).status_code == 200
    box["tok"] = "new-token"
    assert client.get("/", headers={"Authorization": "Bearer old-token"}).status_code == 401
    assert client.get("/", headers={"Authorization": "Bearer new-token"}).status_code == 200


def test_throttle_429_after_10_failures():
    client = _client()
    for _ in range(10):
        assert client.get("/").status_code == 401
    # 11th failure trips the ban -> everything gets 429, even good tokens
    assert client.get("/").status_code == 429
    r = client.get("/", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 429


def test_throttle_logs_client_and_host_not_token(capsys):
    client = _client()
    client.get("/", headers={"Host": "example.ts.net", "Authorization": "Bearer wrong-token"})
    err = capsys.readouterr().err
    assert "auth failure" in err
    assert "example.ts.net" in err
    assert "wrong-token" not in err


# ---- per-IP throttle (direct ASGI calls with a fake clock) ----


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(ip="1.2.3.4", auth="Bearer tok", path="/mcp", method="POST",
           ua="TestUA/1.0", host="h.ts.net"):
    headers = [(b"host", host.encode()), (b"user-agent", ua.encode())]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "headers": headers,
        "client": (ip, 50000),
        "query_string": b"",
        "scheme": "http",
        "server": ("127.0.0.1", 8765),
        "root_path": "",
    }


async def _status(mw, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _run(coro):
    return asyncio.run(coro)


def _fails(mw, ip, n):
    async def go():
        return [await _status(mw, _scope(ip=ip, auth="Bearer bad")) for _ in range(n)]

    return _run(go())


def test_per_ip_ban_does_not_affect_other_ip():
    """IP A banned -> IP B with the RIGHT token still gets 200."""
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        for _ in range(10):
            assert await _status(mw, _scope(ip="6.6.6.6", auth="Bearer bad")) == 401
        # 11th failure trips the ban for 6.6.6.6 only
        assert await _status(mw, _scope(ip="6.6.6.6", auth="Bearer bad")) == 429
        # banned IP refused even with a valid token
        assert await _status(mw, _scope(ip="6.6.6.6")) == 429
        # another IP: good token works, bad token still just 401
        assert await _status(mw, _scope(ip="7.7.7.7")) == 200
        assert await _status(mw, _scope(ip="7.7.7.7", auth="Bearer bad")) == 401

    _run(go())


def test_ban_escalates_and_caps():
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    _fails(mw, "9.9.9.9", 11)
    st = mw._ips["9.9.9.9"]
    assert st.banned_until - clk.t == mw.BAN_BASE_SECONDS  # 600

    clk.advance(mw.BAN_BASE_SECONDS + 1)  # first ban expires
    _fails(mw, "9.9.9.9", 11)
    st = mw._ips["9.9.9.9"]
    assert st.banned_until - clk.t == 1200  # doubled

    st.offenses = 20  # force the cap path
    clk.advance(1201)
    _fails(mw, "9.9.9.9", 11)
    st = mw._ips["9.9.9.9"]
    assert st.banned_until - clk.t == mw.BAN_MAX_SECONDS  # 86400


def test_tracked_ips_capped_oldest_evicted(monkeypatch):
    monkeypatch.setattr(BearerAuthMiddleware, "MAX_TRACKED_IPS", 5)
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        for i in range(1, 8):
            assert await _status(mw, _scope(ip=f"10.0.0.{i}")) == 200

    _run(go())
    assert len(mw._ips) == 5
    assert "10.0.0.1" not in mw._ips and "10.0.0.2" not in mw._ips
    assert "10.0.0.7" in mw._ips


def test_stale_state_pruned():
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        assert await _status(mw, _scope(ip="8.8.8.8", auth="Bearer bad")) == 401
        assert "8.8.8.8" in mw._ips
        clk.advance(120)  # fails + fail-log both stale now
        # any request triggers _prune (>1s elapsed)
        assert await _status(mw, _scope(ip="8.8.4.4")) == 200
        assert "8.8.8.8" not in mw._ips

    _run(go())


def test_failure_log_rate_limited_per_ip(capsys):
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        for _ in range(3):
            await _status(mw, _scope(ip="2.2.2.2", auth="Bearer bad"))
        await _status(mw, _scope(ip="3.3.3.3", auth="Bearer bad"))

    _run(go())
    err = capsys.readouterr().err
    assert err.count("auth failure") == 2  # one per IP
    assert "2.2.2.2" in err and "3.3.3.3" in err

    clk.advance(61)  # outside the 60s log window

    async def go2():
        await _status(mw, _scope(ip="2.2.2.2", auth="Bearer bad"))

    _run(go2())
    err = capsys.readouterr().err
    assert err.count("auth failure") == 1


def test_failure_log_fields(capsys):
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)
    ua = "EvilScanner/9.9 " + "x" * 200

    async def go():
        await _status(
            mw,
            _scope(ip="5.6.7.8", auth="Bearer wrong-token",
                   path="/mcp", method="POST", ua=ua, host="funnel.example.com"),
        )

    _run(go())
    err = capsys.readouterr().err
    assert "auth failure from 5.6.7.8" in err
    assert "path=/mcp" in err and "method=POST" in err
    assert "funnel.example.com" in err
    assert "wrong-token" not in err
    # UA logged but truncated to 80 chars
    assert "EvilScanner/9.9" in err
    assert "x" * 100 not in err


def test_ban_logged_once(capsys):
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        for _ in range(13):
            await _status(mw, _scope(ip="5.5.5.5", auth="Bearer bad"))

    _run(go())
    err = capsys.readouterr().err
    assert err.count("banned 5.5.5.5") == 1
    assert "for 10m" in err
    assert "after 11 failures" in err


def test_auth_ok_logged_once_per_10min(capsys):
    clk = _Clock()
    mw = BearerAuthMiddleware(_ok_app, "tok", clock=clk)

    async def go():
        await _status(mw, _scope(ip="4.4.4.4", ua="Devin/1.0"))
        await _status(mw, _scope(ip="4.4.4.4", ua="Devin/1.0"))

    _run(go())
    err = capsys.readouterr().err
    assert err.count("auth ok from 4.4.4.4") == 1
    assert "Devin/1.0" in err

    clk.advance(601)

    async def go2():
        await _status(mw, _scope(ip="4.4.4.4", ua="Devin/1.0"))

    _run(go2())
    err = capsys.readouterr().err
    assert err.count("auth ok from 4.4.4.4") == 1
