import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import create_engine, select, update

from app.config import Settings, get_settings
from app.db.models import Call
from app.db.session import get_session
from app.main import app

_PATH = "/internal/telephony/calls/123.4/resolve-object"
_HEADERS = {"X-Telephony-Secret": "test-secret"}
_MATCH = {
    "kind": "cabinet",
    "cabinet_id": 72,
    "cabinet_number": "25_125_2",
    "project_id": 184,
    "project_number": "25_125",
    "bitrix_deal_id": "4582",
}


async def test_lookup_persists_confirmed_reference_for_active_call(monkeypatch) -> None:
    engine = create_engine("sqlite://")
    real_http_client = httpx.AsyncClient
    provider_requests = []
    provider_reply = {"status": "found", "match": _MATCH}

    with engine.connect() as connection:
        with connection.begin():
            Call.__table__.create(connection)
            connection.execute(
                Call.__table__.insert().values(
                    id=1, linkedid="123.4", started_at=datetime.now(UTC), sector=4
                )
            )

        @asynccontextmanager
        async def begin():
            with connection.begin():
                yield

        async def session():
            yield AsyncMock(
                begin=begin,
                execute=AsyncMock(side_effect=connection.execute),
                scalar=AsyncMock(side_effect=connection.scalar),
            )

        def provider(request: httpx.Request) -> httpx.Response:
            provider_requests.append(request)
            if provider_reply is None:
                return httpx.Response(500)
            return httpx.Response(200, json=provider_reply)

        def mock_client(**kwargs):
            return real_http_client(transport=httpx.MockTransport(provider), **kwargs)

        monkeypatch.setattr("app.api.call_objects.httpx.AsyncClient", mock_client)
        monkeypatch.setitem(app.dependency_overrides, get_session, session)
        monkeypatch.setitem(
            app.dependency_overrides,
            get_settings,
            lambda: Settings(
                savtassist_base_url="https://helper.savt.by", savtassist_api_secret="test-secret"
            ),
        )

        async with real_http_client(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:
            assert (await api.post(_PATH, json={"number": "25_125_2"})).status_code == 403
            assert not provider_requests

            payload = {"number": "25_125_2", "kind": "cabinet"}
            response = await api.post(_PATH, headers=_HEADERS, json=payload)
            assert response.status_code == 200
            expected = {"number": "25_125_2", "status": "found", "match": _MATCH}
            assert response.json() == expected
            assert len(provider_requests) == 1
            outbound = provider_requests[0]
            assert str(outbound.url) == "https://helper.savt.by/internal/telephony/resolve-object"
            assert outbound.headers["x-telephony-secret"] == "test-secret"
            assert json.loads(outbound.content) == payload

            provider_reply = None
            assert (await api.post(_PATH, headers=_HEADERS, json=payload)).status_code == 502
            with connection.begin():
                assert connection.scalar(select(Call.object_lookup)) == expected
                connection.execute(update(Call).values(ended_at=datetime.now(UTC)))
            assert (await api.post(_PATH, headers=_HEADERS, json=payload)).status_code == 409
            assert len(provider_requests) == 2
    engine.dispose()


@pytest.mark.parametrize(
    ("provider_status", "provider_reply", "expected_status"),
    [
        (200, {"status": "answered", "answer": "Инструкция доступна в Savt Assist."}, 200),
        (200, {"status": "needs_operator", "answer": None}, 200),
        (200, {"status": "answered", "answer": "   "}, 502),
        (503, {"detail": "Не удалось ответить на вопрос"}, 502),
    ],
)
async def test_general_question_uses_shared_secret_and_provider_contract(
    monkeypatch, provider_status, provider_reply, expected_status
) -> None:
    real_http_client = httpx.AsyncClient
    provider_requests = []
    question = "Где найти инструкцию?"

    def provider(request: httpx.Request) -> httpx.Response:
        provider_requests.append(request)
        return httpx.Response(provider_status, json=provider_reply)

    def mock_client(**kwargs):
        return real_http_client(transport=httpx.MockTransport(provider), **kwargs)

    monkeypatch.setattr("app.api.call_objects.httpx.AsyncClient", mock_client)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(
            savtassist_base_url="https://helper.savt.by", savtassist_api_secret="test-secret"
        ),
    )

    path = "/internal/telephony/answer-question"
    async with real_http_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        assert (await api.post(path, json={"question": question})).status_code == 403
        assert (await api.post(path, headers=_HEADERS, json={"question": " "})).status_code == 422
        assert not provider_requests

        response = await api.post(path, headers=_HEADERS, json={"question": f" {question} "})
        assert response.status_code == expected_status
        if expected_status == 200:
            assert response.json() == provider_reply

    outbound, = provider_requests
    assert str(outbound.url) == f"https://helper.savt.by{path}"
    assert outbound.headers["x-telephony-secret"] == "test-secret"
    assert json.loads(outbound.content) == {"question": question}
