import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import create_engine

from app.db.models import Call
from app.db.session import get_session
from app.main import app
from app.services.bitrix import Bitrix
from app.services.department_directory import DepartmentDirectory
from app.services.department_routes import DepartmentRoutes


class Station:
    connected = True
    routes: str | None = None

    async def list_action(self, fields):
        if fields["Action"] == "DBGet":
            if self.routes is None:
                return {"Response": "Error", "Message": "Database entry not found"}, []
            return {"Response": "Success"}, [{"Event": "DBGetResponse", "Val": self.routes}]
        assert fields["Action"] == "CoreShowChannels"
        return {"Response": "Success"}, [
            {"Event": "CoreShowChannel", "Linkedid": "active", "BridgeId": "bridge-1"},
            {"Event": "CoreShowChannel", "Linkedid": "idle", "BridgeId": "<none>"},
        ]

    async def send_action(self, fields):
        assert fields["Action"] == "DBPut"
        assert fields["Family"] == "savt"
        assert fields["Key"] == "department_routes"
        self.routes = fields["Val"]
        return {"Response": "Success"}


async def test_department_routes_and_membership(monkeypatch) -> None:
    requests = []

    def bitrix_response(request: httpx.Request) -> httpx.Response:
        method = request.url.path.split("/")[-1]
        start = json.loads(request.content)["start"]
        requests.append((method, start))
        if method == "department.get.json":
            if start == 0:
                return httpx.Response(
                    200, json={"result": [{"ID": "7", "NAME": "Продажи"}], "next": 50}
                )
            return httpx.Response(200, json={
                "result": [{"ID": "8", "NAME": "Сопровождение", "PARENT": "7"}]
            })
        assert method == "user.get.json"
        return httpx.Response(200, json={
            "result": [
                {"ID": "14", "NAME": "Елена", "LAST_NAME": "Иванова", "UF_DEPARTMENT": [7, "8"]},
                {"ID": "15", "NAME": "Гость", "UF_DEPARTMENT": [7], "USER_TYPE": "extranet"},
            ]
        })

    station = Station()
    monkeypatch.setattr(app.state, "department_routes", DepartmentRoutes(station), raising=False)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            Call.__table__.create(connection)
            now = datetime.now(UTC)
            for call_id, linkedid in enumerate(("active", "idle", "stale"), start=1):
                connection.execute(Call.__table__.insert().values(
                    id=call_id, linkedid=linkedid, started_at=now, answered_at=now, sector=4,
                ))

            async def db_session():
                yield AsyncMock(execute=AsyncMock(side_effect=connection.execute))

            monkeypatch.setitem(app.dependency_overrides, get_session, db_session)
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(bitrix_response)) as bitrix_http,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as api,
            ):
                bitrix = Bitrix(bitrix_http, "https://example.test/rest/1/test-only")
                monkeypatch.setattr(
                    app.state, "department_directory", DepartmentDirectory(bitrix), raising=False
                )
                listing = (await api.get("/api/departments")).json()
                assert [(item["id"], item["parent_id"], item["employee_count"])
                        for item in listing["items"]] == [(7, None, 1), (8, 7, 1)]
                assert listing["items"][0]["call_status"] == "unconfigured"

                route = {
                    "sector": 4, "primary_number": "+375 (29) 123-45-67",
                    "additional_numbers": ["+375291234568"], "queue_wait_seconds": 120,
                }
                saved = await api.put("/api/departments/7/settings", json=route)
                assert saved.status_code == 200
                assert saved.json()["primary_number"] == "+375291234567"
                assert station.routes == "-|-|-|7^+375291234567^queue^120^+375291234568|-"
                conflict = await api.put("/api/departments/8/settings", json=route)
                assert conflict.status_code == 409

                detail = (await api.get("/api/departments/7")).json()
                assert detail["employees"][0]["name"] == "Иванова Елена"
                assert detail["settings"] == saved.json()
                assert detail["call_status"] == "in_call"
                assert detail["active_calls"] == 1
                assert requests == [
                    ("department.get.json", 0),
                    ("department.get.json", 50),
                    ("user.get.json", 0),
                ]

                station.connected = False
                unavailable = (await api.get("/api/departments/7")).json()
                assert unavailable["settings_available"] is False
                assert unavailable["call_status"] == "unknown"
                assert unavailable["active_calls"] is None
                station.connected = True
                assert (await api.delete("/api/departments/7/settings")).status_code == 204
                assert station.routes == "-|-|-|-|-"
    finally:
        engine.dispose()
