"""Simple admin token authentication for yacmemo WebUI."""

from __future__ import annotations

import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """Cookie-based admin session.

    If admin_token is empty, auth is disabled (for local-only setups).
    Otherwise, user must login with the token to get a session cookie.
    API endpoints accept either cookie or Bearer token header.
    """

    def __init__(self, app, admin_token: str):
        super().__init__(app)
        self.admin_token = admin_token
        # In-memory session store (good enough for single-user admin)
        self._sessions: set[str] = set()

    def is_auth_enabled(self) -> bool:
        return bool(self.admin_token)

    def create_session(self) -> str:
        token = secrets.token_hex(24)
        self._sessions.add(token)
        return token

    def validate_session(self, token: str | None) -> bool:
        if not self.admin_token:
            return True  # Auth disabled
        if token and token in self._sessions:
            return True
        return False

    def validate_request(self, request: Request) -> bool:
        if not self.admin_token:
            return True  # Auth disabled

        # Check cookie
        cookie_token = request.cookies.get("yacmemo_admin")
        if cookie_token and self.validate_session(cookie_token):
            return True

        # Check Bearer header (for API calls)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if token == self.admin_token or self.validate_session(token):
                return True

        return False

    async def dispatch(self, request: Request, call_next):
        # Skip login page and static
        path = request.url.path
        if path in ("/admin/login", "/admin/api/login") or path.startswith("/static"):
            return await call_next(request)

        if not self.validate_request(request):
            # Redirect to login for page requests, 401 for API
            if path.startswith("/admin/api/"):
                return Response(
                    content='{"error": "unauthorized"}',
                    status_code=401,
                    media_type="application/json",
                )
            return RedirectResponse(url="/admin/login", status_code=302)

        return await call_next(request)
