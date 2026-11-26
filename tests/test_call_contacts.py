import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.db.models import Call
from app.db.session import get_session
from app.main import app
from app.services.bitrix import Bitrix


@pytest.mark.parametrize(
    ("raw", "ids", "crm_error", "status"),
    [
        ("8 (029) 123-45-67", [17], False, 200),
        ("8 (029) 123-45-67", [17, 42], False, 200),
        ("8 (029) 123-45-67", [], False, 200),
        ("104", [], False, 200),
        ("8 (029) 123-45-67", [], True, 502),
    ],
)
async def test_contacts_link_to_matching_bitrix_cards(
    monkeypatch, admin_session, raw, ids, crm_error, status,
):
    @asynccontextmanager
    async def begin():
        yield

    async def db_session():
        yield SimpleNamespace(begin=begin, get=AsyncMock(return_value=Call(caller_raw=raw)))

    monkeypatch.setitem(app.dependency_overrides, get_session, db_session)
    visited = []

    def bitrix_response(request: httpx.Request) -> httpx.Response:
        method = request.url.path.split("/")[-1]
        visited.append(method)
        payload = json.loads(request.content)
        if method == "crm.duplicate.findbycomm.json":
            assert payload["entity_type"] == "CONTACT" and payload["type"] == "PHONE"
            assert "+375291234567" in payload["values"] and raw in payload["values"]
            if crm_error:
                return httpx.Response(502)
            return httpx.Response(200, json={"result": {"CONTACT": ids}})
        assert method == "crm.item.list.json"
        assert payload["filter"] == {"@id": ids}
        return httpx.Response(200, json={"result": {"items": [
            {"id": item_id, "name": "Имя", "lastName": str(item_id)} for item_id in ids
        ]}})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(bitrix_response)) as crm_http,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://panel.test")
        as api,
    ):
        monkeypatch.setattr(
            app.state, "bitrix", Bitrix(crm_http, "https://crm.test/rest/1/fake-secret/"),
            raising=False,
        )
        response = await api.get("/api/calls/1/contacts")

    assert response.status_code == status
    assert "fake-secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    if status == 200:
        assert response.json() == {"items": [
            {"id": item_id, "name": f"Имя {item_id}",
             "url": f"https://crm.test/crm/contact/details/{item_id}/"} for item_id in ids
        ]}
    expected = [] if raw == "104" else ["crm.duplicate.findbycomm.json"]
    if ids:
        expected.append("crm.item.list.json")
    assert visited == expected
