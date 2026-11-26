from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.db.models import Call
from app.db.session import get_session
from app.main import app


@pytest.mark.parametrize(
    ("direction", "answered_by", "initiated_by", "number"),
    [
        ("incoming", "PJSIP/+375291234567@life-trunk", None, "+375291234567"),
        ("outgoing", None, "PJSIP/104", "104"),
        ("incoming", "Local/3@sector-phone", None, None),
        ("incoming", None, None, None),
    ],
)
async def test_call_history_exposes_employee_number_in_list_and_detail(
    monkeypatch, direction, answered_by, initiated_by, number
):
    now = datetime(2026, 11, 26, tzinfo=UTC)
    call = Call(
        id=1, direction=direction, started_at=now, ended_at=now, sector_by_default=False,
        answered_by=answered_by, initiated_by=initiated_by,
    )

    async def db_session():
        yield AsyncMock(
            get=AsyncMock(side_effect=[call, None]),
            scalar=AsyncMock(return_value=1),
            execute=AsyncMock(return_value=Mock(all=lambda: [(call, False)])),
        )

    monkeypatch.setitem(app.dependency_overrides, get_session, db_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        listing = await api.get("/api/calls")
        card = await api.get("/api/calls/1")

    assert listing.status_code == card.status_code == 200
    for item in (listing.json()["items"][0], card.json()):
        assert item["employee_number"] == number
        assert item["answered_by"] == answered_by
        assert item["initiated_by"] == initiated_by
