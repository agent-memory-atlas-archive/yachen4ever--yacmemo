"""Tests for webui/auth.py: admin authentication middleware."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from yacmemo.webui.auth import AdminAuthMiddleware


def _create_app(token: str) -> FastAPI:
    """Create a minimal FastAPI app with auth middleware."""
    app = FastAPI()
    auth = AdminAuthMiddleware(app, token)
    app.middleware("http")(auth.dispatch)

    @app.get("/admin/")
    async def home():
        return JSONResponse({"ok": True})

    @app.get("/admin/api/data")
    async def api_data():
        return JSONResponse({"data": "secret"})

    @app.get("/admin/login")
    async def login():
        return JSONResponse({"page": "login"})

    @app.post("/admin/api/login")
    async def do_login():
        return JSONResponse({"token": "session"})

    return app


class TestAuthDisabled:
    def test_no_token_allows_all(self):
        app = _create_app("")
        client = TestClient(app)
        resp = client.get("/admin/")
        assert resp.status_code == 200
        resp = client.get("/admin/api/data")
        assert resp.status_code == 200


class TestAuthEnabled:
    def test_redirects_unauthenticated(self):
        app = _create_app("secret-token")
        client = TestClient(app)
        resp = client.get("/admin/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/admin/login" in resp.headers["location"]

    def test_api_returns_401_unauthenticated(self):
        app = _create_app("secret-token")
        client = TestClient(app)
        resp = client.get("/admin/api/data")
        assert resp.status_code == 401

    def test_login_page_accessible(self):
        app = _create_app("secret-token")
        client = TestClient(app)
        resp = client.get("/admin/login")
        assert resp.status_code == 200

    def test_bearer_token_auth(self):
        app = _create_app("secret-token")
        client = TestClient(app)
        resp = client.get("/admin/api/data", headers={
            "Authorization": "Bearer secret-token",
        })
        assert resp.status_code == 200

    def test_wrong_bearer_token_rejected(self):
        app = _create_app("secret-token")
        client = TestClient(app)
        resp = client.get("/admin/api/data", headers={
            "Authorization": "Bearer wrong-token",
        })
        assert resp.status_code == 401


class TestSessionManagement:
    def test_create_and_validate_session(self):
        auth = AdminAuthMiddleware(app=FastAPI(), admin_token="secret")
        session = auth.create_session()
        assert auth.validate_session(session) is True

    def test_invalid_session(self):
        auth = AdminAuthMiddleware(app=FastAPI(), admin_token="secret")
        assert auth.validate_session("nonexistent") is False
        assert auth.validate_session(None) is False

    def test_auth_disabled_no_session_needed(self):
        auth = AdminAuthMiddleware(app=FastAPI(), admin_token="")
        assert auth.validate_session(None) is True
        assert auth.is_auth_enabled() is False

    def test_auth_enabled(self):
        auth = AdminAuthMiddleware(app=FastAPI(), admin_token="secret")
        assert auth.is_auth_enabled() is True
