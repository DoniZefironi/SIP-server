import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

_ENTITY_TYPES = {"deal": 2, "contact": 3, "company": 4}
_LIMIT_ERRORS = {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT"}
_WEBHOOK_PATH = re.compile(r"/rest/[0-9]+/[A-Za-z0-9_-]+/?")
_NUMBERED_CUSTOM_FIELD = re.compile(r"UF_CRM_([0-9]+)_([0-9]+)")


class BitrixError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, uncertain: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


@dataclass(frozen=True, slots=True)
class CallerCard:
    id: int
    label: str
    owner_id: int | None


def positive_id(value: object) -> int | None:
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value)):
        number = int(value)
        if 0 < number < 2**63:
            return number
    return None


class Bitrix:
    def __init__(self, http: httpx.AsyncClient, webhook: str) -> None:
        try:
            url = httpx.URL(webhook.strip())
        except httpx.InvalidURL:
            raise ValueError("Нужен HTTPS URL входящего вебхука Bitrix24") from None
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.query
            or url.fragment
            or not _WEBHOOK_PATH.fullmatch(url.path)
        ):
            raise ValueError("Нужен HTTPS URL входящего вебхука Bitrix24")
        self._base = str(url).rstrip("/")
        self._portal = url.copy_with(path="/")
        self._http = http
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0

    async def _call(
        self, method: str, payload: dict[str, Any], *, writing: bool = False
    ) -> dict[str, Any]:
        async with self._rate_lock:
            await asyncio.sleep(max(0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + 0.55
        try:
            response = await self._http.post(f"{self._base}/{method}.json", json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise BitrixError("Не удалось подключиться к Bitrix24", retryable=True) from None
        except httpx.RequestError:
            raise BitrixError(
                "Не получен ответ Bitrix24", retryable=not writing, uncertain=writing
            ) from None

        try:
            body = response.json()
        except ValueError:
            body = None
        code = body.get("error") if isinstance(body, dict) else None
        status = response.status_code
        if status == 429 or (isinstance(code, str) and code in _LIMIT_ERRORS):
            raise BitrixError("Достигнут лимит Bitrix24", retryable=True)
        if status == 408 or status >= 500:
            raise BitrixError(
                f"Bitrix24 вернул HTTP {status}", retryable=not writing, uncertain=writing
            )
        if not response.is_success or (isinstance(body, dict) and "error" in body):
            raise BitrixError(f"Bitrix24 отклонил запрос ({method}, HTTP {status})")
        if not isinstance(body, dict) or "result" not in body:
            raise BitrixError("Неожиданный ответ Bitrix24", uncertain=writing)
        return body

    async def find_deal(self, field: str, code: str) -> int | None:
        # На портале с useOriginalUfNames=Y приходило пользовательское поле без id.
        # В формате camelCase можно выбрать и проверить оба поля одним запросом.
        if match := _NUMBERED_CUSTOM_FIELD.fullmatch(field):
            field = f"ufCrm{match[1]}_{match[2]}"
        elif field.startswith("UF_CRM_"):
            field = f"ufCrm_{field.removeprefix('UF_CRM_')}"
        body = await self._call(
            "crm.item.list",
            {
                "entityTypeId": _ENTITY_TYPES["deal"],
                "useOriginalUfNames": "N",
                "filter": {f"={field}": code},
                "select": ["id", field],
            },
        )
        items = _items(body)
        total = body.get("total")
        if len(items) == 1 and not body.get("next") and (total is None or positive_id(total) == 1):
            if items[0].get(field) == code:
                return positive_id(items[0].get("id"))
        return None

    async def departments(self, department_ids: tuple[int, ...]) -> list[dict[str, Any]]:
        commands = {f"department_{number}": number for number in department_ids}
        body = await self._call(
            "batch",
            {"cmd": {key: f"department.get?ID={number}" for key, number in commands.items()}},
        )
        batch = body["result"]
        pages = batch.get("result") if isinstance(batch, dict) else None
        if not isinstance(pages, dict) or batch.get("result_error"):
            raise BitrixError("Bitrix24 не вернул выбранные подразделения")
        departments = []
        for key, number in commands.items():
            page = pages.get(key)
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise BitrixError("Bitrix24 вернул неверный список подразделений")
            departments.extend(item for item in page if positive_id(item.get("ID")) == number)
        return departments

    async def directory_pages(
        self, method: str, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        items = []
        start = 0
        while True:
            body = await self._call(method, {**payload, "start": start})
            page = body["result"]
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise BitrixError("Bitrix24 вернул неверный ответ справочника")
            items.extend(page)
            if "next" not in body:
                return items
            next_start = positive_id(body["next"])
            if next_start is None or next_start <= start:
                raise BitrixError("Bitrix24 вернул неверную страницу справочника")
            start = next_start

    async def find_by_phone(self, kind: str, phones: list[str]) -> list[int]:
        body = await self._call(
            "crm.duplicate.findbycomm",
            {"entity_type": kind.upper(), "type": "PHONE", "values": phones},
        )
        result = body["result"]
        if result == []:
            return []
        ids = result.get(kind.upper(), []) if isinstance(result, dict) else None
        if not isinstance(ids, list):
            raise BitrixError("Bitrix24 вернул неверный результат поиска по телефону")
        return sorted({number for value in ids if (number := positive_id(value)) is not None})

    def card_url(self, kind: str, card_id: int) -> str:
        return str(self._portal.copy_with(path=f"/crm/{kind}/details/{card_id}/"))

    async def caller_cards(self, kind: str, ids: list[int]) -> list[CallerCard]:
        if kind == "contact":
            name_fields = ("name", "lastName")
            fallback = "Контакт"
        else:
            name_fields = ("title",)
            fallback = "Компания"
        cards = []
        for offset in range(0, len(ids), 50):
            batch = ids[offset : offset + 50]
            body = await self._call(
                "crm.item.list",
                {
                    "entityTypeId": _ENTITY_TYPES[kind],
                    "filter": {"@id": batch},
                    "select": ["id", "assignedById", *name_fields],
                },
            )
            for item in _items(body):
                card_id = positive_id(item.get("id"))
                if card_id is None or card_id not in batch:
                    continue
                label = " ".join(
                    part.strip()
                    for field in name_fields
                    if isinstance(part := item.get(field), str) and part.strip()
                )
                cards.append(
                    CallerCard(
                        card_id,
                        label or f"{fallback} {card_id}",
                        positive_id(item.get("assignedById")),
                    )
                )
        return cards

    async def send(self, kind: str, target_id: int, message: str) -> int:
        if kind == "chat":
            method = "im.message.add"
            payload = {"DIALOG_ID": str(target_id), "MESSAGE": message, "URL_PREVIEW": "N"}
        else:
            method = "crm.timeline.comment.add"
            payload = {"fields": {"ENTITY_TYPE": kind, "ENTITY_ID": target_id, "COMMENT": message}}
        body = await self._call(method, payload, writing=True)
        external_id = positive_id(body["result"])
        if external_id is None:
            raise BitrixError("В ответе Bitrix24 нет ID отправленной записи", uncertain=True)
        return external_id

    async def find_sent(self, kind: str, target_id: int, marker: str) -> int | None:
        if kind == "chat":
            body = await self._call(
                "im.dialog.messages.get", {"DIALOG_ID": str(target_id), "LIMIT": 50}
            )
            result = body["result"]
            entries = result.get("messages") if isinstance(result, dict) else None
            text_key, id_key = "text", "id"
        else:
            body = await self._call(
                "crm.timeline.comment.list",
                {
                    "filter": {"ENTITY_TYPE": kind, "ENTITY_ID": target_id},
                    "order": {"ID": "DESC"},
                    "select": ["ID", "COMMENT"],
                },
            )
            entries = body["result"]
            text_key, id_key = "COMMENT", "ID"
        if not isinstance(entries, list):
            raise BitrixError("Не удалось прочитать историю доставки Bitrix24", retryable=True)
        for entry in entries:
            if isinstance(entry, dict) and marker in str(entry.get(text_key, "")).splitlines():
                if (external_id := positive_id(entry.get(id_key))) is not None:
                    return external_id
        return None


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    result = body["result"]
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BitrixError("Bitrix24 вернул неверный список карточек")
    return items
