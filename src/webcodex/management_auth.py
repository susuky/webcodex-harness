"""Account sessions confined to the task management APIs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from http.cookies import CookieError, SimpleCookie

from starlette.responses import JSONResponse
from starlette.routing import Route

COOKIE = "webcodex_management"
SESSION_SECONDS = 7 * 24 * 3600
PUBLIC_ROUTES = {
    ("/api/manage/session", "GET"),
    ("/api/manage/login", "POST"),
    ("/api/manage/logout", "POST"),
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


class ManagementAuth:
    def __init__(self, config):
        self.config = config
        self.sessions: dict[str, float] = {}
        self.attempts: dict[str, list[float]] = {}

    def cookie(self, scope) -> str:
        try:
            cookie = SimpleCookie()
            cookie.load(dict(scope["headers"]).get(b"cookie", b"").decode("latin-1"))
            return cookie[COOKIE].value if COOKIE in cookie else ""
        except CookieError:
            return ""

    def owner(self, scope) -> str | None:
        login = self.config.management_login
        if login and self.sessions.get(self.cookie(scope), 0) > time.time():
            return login.client_id
        return None

    def allowed_origin(self, origin: str) -> bool:
        login = self.config.management_login
        return origin in {self.config.public_url, *(login.origins if login else [])}

    def response(self, body, status=200):
        return JSONResponse(body, status, headers={"Cache-Control": "no-store"})

    async def session(self, request):
        return self.response(
            {
                "authenticated": self.owner(request.scope) is not None,
                "password_enabled": self.config.management_login is not None,
            }
        )

    async def login(self, request):
        login = self.config.management_login
        if login is None:
            return self.response({"error": "尚未設定密碼登入"}, 404)
        if len(await request.body()) > 4096:
            return self.response({"error": "登入資料過長"}, 400)
        body = await request.json()
        username = body.get("username") if isinstance(body, dict) else None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(username, str) or not 1 <= len(username) <= 128:
            return self.response({"error": "請輸入帳號"}, 400)
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            return self.response({"error": "請輸入密碼"}, 400)
        now = time.time()
        self.attempts = {
            host: [stamp for stamp in stamps if stamp > now - 60]
            for host, stamps in self.attempts.items()
            if stamps and stamps[-1] > now - 60
        }
        host = request.client.host if request.client else "local"
        attempts = self.attempts.get(host, [])
        if len(attempts) >= 5 or (host not in self.attempts and len(self.attempts) >= 1024):
            response = self.response({"error": "嘗試次數過多，請一分鐘後重試"}, 429)
            response.headers["Retry-After"] = "60"
            return response
        self.attempts[host] = [*attempts, now]
        salt = bytes.fromhex(login.password_hash.split("$")[1])
        candidate = await asyncio.to_thread(hash_password, password, salt)
        valid_password = hmac.compare_digest(candidate, login.password_hash)
        valid_username = hmac.compare_digest(
            username.encode(), (login.username or login.client_id).encode()
        )
        if not valid_password or not valid_username:
            return self.response({"error": "帳號或密碼不正確"}, 401)
        self.attempts.pop(host, None)
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        self.sessions.pop(self.cookie(request.scope), None)
        if len(self.sessions) >= 128:
            self.sessions.pop(next(iter(self.sessions)))
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_SECONDS
        response = self.response({"authenticated": True})
        response.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_SECONDS,
            path="/api/manage",
            httponly=True,
            secure=request.url.scheme == "https",
            samesite="strict",
        )
        return response

    async def logout(self, request):
        self.sessions.pop(self.cookie(request.scope), None)
        response = self.response({"authenticated": False})
        response.delete_cookie(COOKIE, path="/api/manage", httponly=True, samesite="strict")
        return response

    def routes(self):
        return [
            Route("/api/manage/session", self.session, methods=["GET"]),
            Route("/api/manage/login", self.login, methods=["POST"]),
            Route("/api/manage/logout", self.logout, methods=["POST"]),
        ]
