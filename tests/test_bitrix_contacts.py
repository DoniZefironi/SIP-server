import json
from datetime import UTC, datetime

import httpx
import pytest

from app.services.bitrix import Bitrix, BitrixError
from app.services.bitrix_contacts import _contact_values, _station_numbers


async def test_contact_pages_advance_by_id_until_empty():
    pages = {0: [{"id": 17}, {"id": 29}], 29: [{"id": 50}], 50: []}
    visited = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("crm.item.list.json")
        params = json.loads(request.content)
        assert params["entityTypeId"] == 3 and params["select"] == ["*"]
        assert params["order"] == {"id": "ASC"} and params["start"] == 0
        cursor = params["filter"][">id"]
        visited.append(cursor)
        return httpx.Response(200, json={"result": {"items": pages[cursor]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        bitrix = Bitrix(http, "https://crm.test/rest/1/fake-secret/")
        ids = [item["id"] async for page in bitrix.contact_pages() for item in page]
        assert ids == [17, 29, 50]
    assert visited == [0, 29, 50]


async def test_contact_pages_reject_nonadvancing_cursor():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"result": {"items": [{"id": 7}, {"id": 7}]}},
    ))) as http:
        bitrix = Bitrix(http, "https://crm.test/rest/1/fake-secret/")
        with pytest.raises(BitrixError):
            await anext(bitrix.contact_pages())


def test_contact_numbers_are_normalized_for_station():
    values = _contact_values({
        "id": "7", "name": "Анна", "lastName": "Иванова",
        "fm": [
            {"typeId": "PHONE", "value": "8 (029) 123-45-67"},
            {"typeId": "PHONE", "value": "+375291234567"},
            {"typeId": "PHONE", "value": "104"},
            {"typeId": "EMAIL", "value": "test@example.test"},
        ],
    }, datetime.now(UTC))
    assert values["name"] == "Иванова Анна"
    assert values["phones"] == ["+375291234567"]
    assert _station_numbers([values["phones"], ["+375291234567", "+74951234567"]]) == {
        "375291234567", "80291234567", "74951234567",
    }
