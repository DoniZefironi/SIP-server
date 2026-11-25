import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.db.models import Call
from app.services.bitrix import Bitrix, BitrixError, CallerCard
from app.services.bitrix_delivery import _message, destinations
from app.services.yandexgpt import CallSummary

_WEBHOOK = "https://portal.example/rest/1/test-hook/"
_SUMMARY = CallSummary(topic="Уточнение сроков", agreements=[], next_steps=[], deal_code="26_127")


async def test_deal_and_all_distinct_contact_owners() -> None:
    client = AsyncMock(spec=Bitrix)
    client.find_deal.return_value = 55
    client.find_by_phone.return_value = [11, 12, 13]
    client.caller_cards.return_value = [
        CallerCard(11, "Первый [USER=50]", 7),
        CallerCard(12, "Второй", 7),
        CallerCard(13, "Третий", 8),
    ]
    client.card_url.side_effect = lambda kind, id: (
        f"https://portal.example/crm/{kind}/details/{id}/"
    )
    call = Call(caller="+375291234567", caller_raw="80291234567")

    targets = await destinations(call, _SUMMARY, client, Settings())

    assert set(targets) == {("deal", 55), ("chat", 7), ("chat", 8)}
    assert targets["deal", 55] is None
    assert targets["chat", 7] == (
        "Совпадения по номеру: [URL=https://portal.example/crm/contact/details/11/]"
        "Первый ［USER=50］[/URL], [URL=https://portal.example/crm/contact/details/12/]Второй[/URL]"
    )
    assert targets["chat", 8] == (
        "Совпадения по номеру: [URL=https://portal.example/crm/contact/details/13/]Третий[/URL]"
    )
    marker = "SAVT-CALL-1-123"
    assert _message(marker, _SUMMARY, targets["chat", 7]).splitlines()[1] == targets["chat", 7]
    kind, numbers = client.find_by_phone.await_args.args
    assert kind == "contact"
    assert {"+375291234567", "375291234567", "80291234567"}.issubset(numbers)


async def test_confirmed_call_object_takes_priority_over_transcript_code() -> None:
    client = AsyncMock(spec=Bitrix)
    call = Call(
        object_lookup={
            "number": "25_125_2",
            "status": "found",
            "match": {"kind": "cabinet", "bitrix_deal_id": "4582"},
        }
    )

    assert await destinations(call, _SUMMARY, client, Settings()) == {("deal", 4582): None}
    client.find_deal.assert_not_awaited()


async def test_company_fallback_if_deal_and_contact_do_not_resolve() -> None:
    client = AsyncMock(spec=Bitrix)
    client.find_deal.return_value = None
    client.find_by_phone.side_effect = [[], [31]]
    client.caller_cards.return_value = [CallerCard(31, "Компания", 8)]
    client.card_url.return_value = "https://portal.example/crm/company/details/31/"

    targets = await destinations(Call(caller="+375291234567"), _SUMMARY, client, Settings())

    assert targets == {
        ("company", 31): None,
        (
            "chat",
            8,
        ): "Кто звонил: [URL=https://portal.example/crm/company/details/31/]Компания[/URL]",
    }
    assert [args.args[0] for args in client.find_by_phone.await_args_list] == ["contact", "company"]


async def test_hidden_number_uses_sector_recipient_without_creating_crm_card() -> None:
    client = AsyncMock(spec=Bitrix)
    summary = _SUMMARY.model_copy(update={"deal_code": None})

    targets = await destinations(
        Call(sector=4), summary, client, Settings(bitrix_sector_recipients={4: 19})
    )

    assert targets == {("chat", 19): "Кто звонил: номер скрыт или недоступен"}
    client.find_by_phone.assert_not_awaited()


async def test_unknown_number_appears_in_chat_for_sector_recipient() -> None:
    client = AsyncMock(spec=Bitrix)
    client.find_by_phone.return_value = []
    summary = _SUMMARY.model_copy(update={"deal_code": None})
    targets = await destinations(
        Call(caller="+375291234567", sector=4),
        summary,
        client,
        Settings(bitrix_sector_recipients={4: 19}),
    )

    assert targets == {("chat", 19): "Кто звонил: +375291234567"}


async def test_contact_details_include_name_and_link_without_webhook_credentials() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)
        assert params["select"] == ["id", "assignedById", "name", "lastName"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "items": [
                        {"id": "35", "assignedById": "7", "name": "Тест", "lastName": "Контакт"},
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        bitrix = Bitrix(http, _WEBHOOK)
        assert await bitrix.caller_cards("contact", [35]) == [CallerCard(35, "Тест Контакт", 7)]
        assert bitrix.card_url("contact", 35) == "https://portal.example/crm/contact/details/35/"


async def test_deal_search_rejects_an_ambiguous_result() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/crm.item.list.json")
        payload = json.loads(request.content)
        assert payload["filter"] == {"=ufCrm_TEST": "26_127"}
        return httpx.Response(
            200, json={"result": {"items": [{"id": 55, "ufCrm_TEST": "26_127"}]}, "total": 2}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await Bitrix(http, _WEBHOOK).find_deal("UF_CRM_TEST", "26_127") is None


async def test_deal_search_selects_id_and_original_code_in_camel_case() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        field = "ufCrm_1730705809359"
        assert payload["useOriginalUfNames"] == "N"
        assert payload["select"] == ["id", field]
        assert payload["filter"] == {f"={field}": "26_127"}
        return httpx.Response(
            200, json={"result": {"items": [{"id": 55, field: "26_127"}]}, "total": 1}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await Bitrix(http, _WEBHOOK).find_deal("UF_CRM_1730705809359", "26_127") == 55


async def test_send_timeout_has_an_uncertain_result() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/im.message.add.json")
        assert json.loads(request.content)["DIALOG_ID"] == "41"
        raise httpx.ReadTimeout("mock timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(BitrixError) as caught:
            await Bitrix(http, _WEBHOOK).send("chat", 41, "Test summary")
    assert caught.value.uncertain
    assert not caught.value.retryable
