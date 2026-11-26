import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from sqlalchemy import create_engine, select

from app.db.models import Call
from app.db.session import get_session
from app.main import app
from app.services.call_history import history_conditions


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


def test_number_search_matches_partial_formatted_and_original_numbers():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.connection.create_function(
                "regexp_replace", 4,
                lambda raw, pattern, replacement, _: re.sub(pattern, replacement, raw)
                if raw is not None else None,
            )
            Call.__table__.create(connection)
            now = datetime(2026, 11, 26, tzinfo=UTC)
            for call_id, caller, raw in (
                (1, "+375291234567", "375291234567"),
                (2, "+375331234567", "375331234567"),
                (3, None, "(104)"),
                (4, None, "8 (029) 123-45-67"),
            ):
                connection.execute(Call.__table__.insert().values(
                    id=call_id, linkedid=str(call_id), started_at=now,
                    caller=caller, caller_raw=raw,
                ))

            for query, expected in (
                ("123-45-67", [1, 2, 4]),
                ("   +375 (29) 123-45-67 ", [1, 4]),
                ("80 (29) 12", [1, 4]),
                ("+37529", [1, 4]),
                ("8 (029) 123-45-67", [1, 4]),
                ("104", [3]),
                ("9999999", []),
                ("%", []),
                ("  ", [1, 2, 3, 4]),
            ):
                result = connection.scalars(
                    select(Call.id).where(*history_conditions(caller=query)).order_by(Call.id)
                ).all()
                assert result == expected, query
    finally:
        engine.dispose()
