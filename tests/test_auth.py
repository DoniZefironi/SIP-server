from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx

from app.db.models import AuthUser
from app.main import app
from app.services import auth


async def test_password_hash_is_salted_and_uses_argon2id():
    password = "a password used only in tests"
    encoded = await auth.hash_password(password)
    assert encoded.startswith("$argon2id$")
    assert encoded != await auth.hash_password(password)
    assert await auth.verify_password(password, encoded)
    assert not await auth.verify_password("another password", encoded)


async def test_private_routes_reject_anonymous_requests():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test",
    ) as api:
        for path in (
            "/api/calls", "/api/calls/counts", "/api/calls/1", "/api/calls/1/audio/mix",
            "/api/calls/1/contacts",
            "/api/statistics/calls?date_from=2026-11-25T00:00Z&date_to=2026-11-26T00:00Z",
            "/api/statistics/callers?date_from=2026-11-25T00:00Z&date_to=2026-11-26T00:00Z",
            "/api/departments", "/api/departments/25", "/api/contacts", "/api/settings/telephony",
            "/api/settings/holidays", "/api/events", "/api/auth/users", "/docs", "/openapi.json",
        ):
            response = await api.get(path)
            assert response.status_code == 401, path
            assert response.headers["Cache-Control"] == "no-store"


async def test_viewer_cannot_write_and_manager_cannot_administer(monkeypatch):
    account = AuthUser(
        id=1, username="viewer-test", role="viewer", is_active=True, created_at=datetime.now(UTC),
    )
    monkeypatch.setattr(auth, "current_user", AsyncMock(return_value=account))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test",
        cookies={"__Host-savt_access": "a" * 43}, headers={"X-SAVT-CSRF": "1"},
    ) as api:
        profile = (await api.get("/api/auth/me")).json()
        assert profile["role"] == "viewer" and "password_hash" not in profile
        assert (await api.get("/api/settings/telephony")).status_code == 403
        assert (await api.get("/api/contacts")).status_code == 403
        assert (await api.delete("/api/departments/25/settings")).status_code == 403
        account.role = "manager"
        assert (await api.get("/api/auth/users")).status_code == 403
        assert (await api.get("/api/events")).status_code == 403
        assert (await api.delete(
            "/api/departments/25/settings", headers={"Sec-Fetch-Site": "cross-site"},
        )).status_code == 403
        assert (await api.post("/api/auth/logout", headers={"X-SAVT-CSRF": "0"})).status_code == 403


async def test_login_validation_never_echoes_password():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test",
    ) as api:
        response = await api.post(
            "/api/auth/login", json={"username": "username", "password": "hidden" * 100},
            headers={"X-SAVT-CSRF": "1"},
        )
        assert response.status_code == 422
        assert "hidden" not in response.text
