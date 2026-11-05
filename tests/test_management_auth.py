from __future__ import annotations

import time

import httpx
import pytest
from pydantic import ValidationError

from webcodex.config import ManagementLogin, ServerConfig
from webcodex.management_auth import COOKIE, hash_password

pytest_plugins = ["test_harness"]


def enable(harness):
    harness.config.management_login = ManagementLogin(
        client_id="owner",
        username="ping",
        password_hash=hash_password("test-password"),
        origins=[harness.url, "http://ll1:8790"],
    )


async def test_password_session_reads_only_management_and_logout_revokes(harness):
    enable(harness)
    async with httpx.AsyncClient(base_url=harness.url) as browser:
        assert (await browser.get("/api/manage/session")).json() == {
            "authenticated": False,
            "password_enabled": True,
        }
        assert (await browser.get("/api/manage/state")).status_code == 401
        assert (
            await browser.post(
                "/api/manage/login",
                json={"password": "test-password"},
                headers={"Origin": harness.url},
            )
        ).status_code == 400
        assert (
            await browser.post(
                "/api/manage/login",
                json={"username": "wrong", "password": "test-password"},
                headers={"Origin": harness.url},
            )
        ).status_code == 401
        response = await browser.post(
            "/api/manage/login",
            json={"username": "ping", "password": "test-password"},
            headers={"Origin": harness.url},
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Path=/api/manage" in cookie
        assert "test-password" not in response.text
        assert (await browser.get("/api/manage/session")).json()["authenticated"]
        assert (await browser.get("/api/manage/state")).status_code == 200
        # A forged Cookie header outside the browser's cookie path still grants no tool access.
        secret = browser.cookies.get(COOKIE)
        for path in ("/api/call", "/api/runner/poll", "/api/admin/questions", "/mcp"):
            assert (
                await browser.post(path, headers={"Cookie": f"{COOKIE}={secret}"}, json={})
            ).status_code == 401
        assert (await browser.post("/api/manage/refresh", json={"task_ids": []})).status_code == 403
        assert (
            await browser.post(
                "/api/manage/refresh", json={"task_ids": []}, headers={"Origin": "http://ll1:8790"}
            )
        ).status_code == 200
        archive = "/api/manage/sources/local/tasks/saved-task/archive"
        assert (await browser.post(archive, json={"archived": True})).status_code == 403
        assert (
            await browser.post(archive, json={"archived": True}, headers={"Origin": harness.url})
        ).status_code == 200
        assert (
            await browser.post("/api/manage/logout", headers={"Origin": harness.url})
        ).status_code == 200
        assert not (await browser.get("/api/manage/session")).json()["authenticated"]
        assert (
            await browser.get("/api/manage/state", headers={"Cookie": f"{COOKIE}={secret}"})
        ).status_code == 401


async def test_password_origin_failures_rate_limit_and_expiry(harness):
    enable(harness)
    async with httpx.AsyncClient(base_url=harness.url) as browser:
        for origin in (None, "http://evil.example", "null"):
            headers = {"Origin": origin} if origin else {}
            assert (
                await browser.post(
                    "/api/manage/login",
                    json={"username": "ping", "password": "test-password"},
                    headers=headers,
                )
            ).status_code == 403
        headers = {"Origin": harness.url}
        for _ in range(5):
            response = await browser.post(
                "/api/manage/login", json={"username": "ping", "password": "wrong"}, headers=headers
            )
            assert response.status_code == 401 and response.json()["error"] == "帳號或密碼不正確"
        response = await browser.post(
            "/api/manage/login",
            json={"username": "ping", "password": "test-password"},
            headers=headers,
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
        auth = harness.app.state.management_auth
        auth.attempts = {"127.0.0.1": [time.time() - 61]}
        assert (
            await browser.post(
                "/api/manage/login",
                json={"username": "ping", "password": "test-password"},
                headers=headers,
            )
        ).status_code == 200
        secret = browser.cookies.get(COOKIE)
        auth.sessions[secret] = time.time() - 1
        assert (await browser.get("/api/manage/state")).status_code == 401


async def test_token_mode_and_management_origin_do_not_expand_tool_origins(harness):
    async with harness.http() as client:
        assert (await client.get("/api/manage/session")).json() == {
            "authenticated": False,
            "password_enabled": False,
        }
        assert (await client.get("/api/manage/state")).status_code == 200
        enable(harness)
        assert (
            await client.get("/api/manage/state", headers={"Origin": "http://ll1:8790"})
        ).status_code == 200
        assert (
            await client.post(
                "/api/call", json={"name": "projects_list"}, headers={"Origin": "http://ll1:8790"}
            )
        ).status_code == 403


def test_password_settings_require_hash_owner_and_exact_origins(tmp_path):
    password_hash = hash_password("test-password")
    for origin in ("http://ll1:8790/manage", "http://user:pass@ll1", "*", "http://ll1:0"):
        with pytest.raises(ValidationError):
            ManagementLogin(client_id="owner", password_hash=password_hash, origins=[origin])
    with pytest.raises(ValidationError):
        ManagementLogin(client_id="owner", password_hash="plaintext-password")
    with pytest.raises(ValidationError, match="existing client"):
        ServerConfig(
            state_dir=tmp_path,
            admin_token="a" * 40,
            clients={},
            runners={},
            management_login=ManagementLogin(client_id="missing", password_hash=password_hash),
        )
