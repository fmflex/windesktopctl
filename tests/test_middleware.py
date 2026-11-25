"""Unit tests for the HTTP bearer middleware (Starlette TestClient)."""

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
