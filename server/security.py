from __future__ import annotations

import hashlib
import secrets
import time
from urllib.parse import urlparse

from fastapi import Header, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from server.storage.database import Database, now, uid

COOKIE = "xbuddy_session"
LIFETIME = 30 * 86400


class SessionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, db: Database, secure: bool = False):
        super().__init__(app)
        self.db, self.secure = db, secure

    async def dispatch(self, request: Request, call_next):
        token = request.cookies.get(COOKIE, "")
        session = self.db.one("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
                              (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))) if token else None
        new_token = None
        if not session and request.method == "GET" and request.url.path in {"/", "/api/session"}:
            new_token = secrets.token_urlsafe(32)
            session = {"id": uid(), "csrf_token": secrets.token_urlsafe(32), "expires_at": int(time.time()) + LIFETIME}
            with self.db.connect() as connection:
                connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (
                    session["id"], hashlib.sha256(new_token.encode()).hexdigest(),
                    session["csrf_token"], session["expires_at"], now()))
        request.state.session = session
        response = await call_next(request)
        if new_token:
            response.set_cookie(COOKIE, new_token, max_age=LIFETIME, httponly=True,
                                secure=self.secure, samesite="lax", path="/")
        if request.url.path.startswith("/api/") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response


def session_owner(request: Request) -> str:
    session = getattr(request.state, "session", None)
    if not session:
        raise HTTPException(401, "请先打开工作台或获取会话")
    return session["id"]


def write_owner(request: Request, csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> str:
    owner = session_owner(request)
    token = csrf_token or ""
    if not secrets.compare_digest(token, request.state.session["csrf_token"]):
        raise HTTPException(403, "会话校验失败，请刷新页面")
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
            raise HTTPException(403, "请求来源不匹配")
    return owner
