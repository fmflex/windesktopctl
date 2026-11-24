"""Unit tests for the HTTP bearer middleware (Starlette TestClient)."""

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from deskctl.mcp_server import BearerAuthMiddleware


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
