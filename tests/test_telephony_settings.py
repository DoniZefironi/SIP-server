import httpx
import pytest

from app.ami.astdb import AstDB
from app.main import app
from app.services.telephony_settings import StationSettings


class Station:
    connected = True

    def __init__(self) -> None:
        self.values = {
            ("savt", "work_days"): "fri-mon&wed",
            ("savt", "work_hours"): "2200-0559",
            ("savt", "queue_wait"): "180",
            ("savt", "department_routes"): "keep",
            ("savt-holiday", "20261231"): "1",
        }
        self.writes = []

    async def list_action(self, fields):
        family = fields["Family"]
        if fields["Action"] == "DBGet":
            assert (family, fields["Key"]) not in self.values
            return {"Response": "Error", "Message": "Database entry not found"}, []
        assert fields["Action"] == "DBGetTree"
        return {"Response": "Success"}, [
            {"Event": "DBGetTreeResponse", "Key": f"/{family}/{key}", "Val": value}
            for (section, key), value in self.values.items() if section == family
        ]

    async def send_action(self, fields):
        self.writes.append(fields)
        key = (fields["Family"], fields["Key"])
        if fields["Action"] == "DBPut":
            self.values[key] = fields["Val"]
        else:
            assert fields["Action"] == "DBDel"
            if self.values.pop(key, None) is None:
                return {"Response": "Error", "Message": "Database entry could not be deleted"}
        return {"Response": "Success"}


@pytest.fixture
def station(monkeypatch) -> Station:
    station = Station()
    monkeypatch.setattr(
        app.state, "station_settings", StationSettings(AstDB(station)), raising=False
    )
    return station


@pytest.mark.usefixtures("admin_session")
async def test_settings_api_reads_existing_values_and_saves_one_update(station: Station) -> None:
    url = "/api/settings/telephony"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        current = await api.get(url)
        assert current.status_code == 200
        settings = current.json()
        assert settings["work_days"] == ["mon", "wed", "fri", "sat", "sun"]
        assert (settings["opens_at"], settings["closes_at"]) == ("22:00", "06:00")
        assert settings["queue_wait_seconds"] == 180

        invalid = await api.put(url, json={**settings, "default_sector": 6})
        assert invalid.status_code == 422
        assert not station.writes

        updated = {
            **settings, "work_days": ["mon", "wed"], "opens_at": "08:00",
            "closes_at": "17:00", "queue_wait_seconds": None, "ring_time_seconds": 23,
        }
        saved = await api.put(url, json=updated)
        assert saved.status_code == 200
        assert saved.json() == updated
        assert len(station.writes) == 1
        assert station.values["savt", "telephony_settings"] == (
            "mon&wed|0800-1659|3|6|1|0|23|120|5"
        )
        assert station.values["savt", "department_routes"] == "keep"
        assert (await api.get(url)).json() == updated


@pytest.mark.usefixtures("admin_session")
async def test_holiday_api_applies_to_existing_astdb_calendar(station: Station) -> None:
    url = "/api/settings/holidays"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        assert (await api.get(url)).json() == {"dates": ["2026-12-31"]}
        assert (await api.put(f"{url}/2026-12-30")).status_code == 204
        assert station.values["savt-holiday", "20261230"] == "1"
        assert (await api.get(url)).json() == {"dates": ["2026-12-30", "2026-12-31"]}
        for _ in range(2):
            deleted = await api.delete(f"{url}/2026-12-31")
            assert deleted.status_code == 204
            assert not deleted.content
        assert (await api.get(url)).json() == {"dates": ["2026-12-30"]}
        assert (await api.put(f"{url}/2026-02-30")).status_code == 422

        station.connected = False
        assert (await api.get(url)).status_code == 503
