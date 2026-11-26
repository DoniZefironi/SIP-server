import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.db.models import BitrixDelivery, Call, RecordingJob
from app.db.session import SessionLocal
from app.services.bitrix import Bitrix, BitrixError, positive_id
from app.services.phones import crm_phone_variants
from app.services.summarization import SummaryStatus
from app.services.yandexgpt import CallSummary

log = logging.getLogger(__name__)
_RETRY_DELAY = timedelta(seconds=20)
_MAX_RETRIES = 5
_REQUEST_TIMEOUT = 40
_SEND_WINDOW = timedelta(seconds=90)
_VERIFY_DELAY = timedelta(seconds=5)
_INACTIVE_BBCODE = str.maketrans({"[": "［", "]": "］"})


async def destinations(
    call: Call, summary: CallSummary, client: Bitrix, settings: Settings
) -> dict[tuple[str, int], str | None]:
    deal = None
    if call.object_lookup and call.object_lookup.get("status") == "found":
        match = call.object_lookup.get("match")
        if isinstance(match, dict):
            deal = positive_id(match.get("bitrix_deal_id"))
    if deal is None and summary.deal_code:
        deal = await client.find_deal(settings.bitrix_deal_code_field, summary.deal_code)

    cards: set[tuple[str, int]] = set()
    owner_links: dict[int, list[str]] = defaultdict(list)
    matched_links: list[str] = []
    numbers = crm_phone_variants(call.caller, call.caller_raw)
    if numbers:
        for kind in ("contact", "company"):
            ids = await client.find_by_phone(kind, numbers)
            if ids:
                cards.update((kind, card_id) for card_id in ids)
                for card in await client.caller_cards(kind, ids):
                    name = " ".join(card.label.split()).translate(_INACTIVE_BBCODE)
                    link = f"[URL={client.card_url(kind, card.id)}]{name}[/URL]"
                    matched_links.append(link)
                    if card.owner_id is not None:
                        owner_links[card.owner_id].append(link)
                break

    if not owner_links:
        recipient = settings.bitrix_sector_recipients.get(call.sector)
        fallback = positive_id(recipient) or positive_id(settings.bitrix_default_recipient)
        if fallback:
            owner_links[fallback] = matched_links

    crm = {("deal", deal)} if deal else cards
    targets: dict[tuple[str, int], str | None] = dict.fromkeys(crm)
    label = "Совпадения по номеру" if len(cards) > 1 else "Кто звонил"
    for owner, links in owner_links.items():
        targets["chat", owner] = (
            f"{label}: {', '.join(links)}" if links else _caller_phone_line(call)
        )
    return targets


def _caller_phone_line(call: Call) -> str:
    caller = call.caller or "номер скрыт или недоступен"
    return f"Кто звонил: {caller.translate(_INACTIVE_BBCODE)}"


def _marker(call: Call) -> str:
    return f"SAVT-CALL-{call.id}-{int(call.started_at.timestamp())}"


def _message(marker: str, summary: CallSummary, caller_details: str | None = None) -> str:
    # Квадратные скобки могут интерпретироваться Bitrix24 как BBCode, включая упоминания.
    lines = [f"Суть обращения: {summary.topic}"]
    if summary.deal_code:
        lines.append(f"Код из разговора: {summary.deal_code}")
    for title, values in (
        ("Договорённости", summary.agreements),
        ("Следующие действия", summary.next_steps),
    ):
        if values:
            lines.extend([title, *(f"• {value}" for value in values)])
    header = [marker, caller_details] if caller_details else [marker]
    return "\n".join([*header, *(line.translate(_INACTIVE_BBCODE) for line in lines)])


async def route_next(client: Bitrix, settings: Settings) -> bool:
    async with SessionLocal.begin() as session:
        row = (
            await session.execute(
                select(RecordingJob, Call)
                .join(Call, Call.id == RecordingJob.call_id)
                .where(
                    RecordingJob.summary_status == SummaryStatus.COMPLETED,
                    RecordingJob.bitrix_routing_status == "pending",
                    RecordingJob.bitrix_route_due_at <= datetime.now(UTC),
                )
                .order_by(RecordingJob.bitrix_route_due_at, RecordingJob.call_id)
                .limit(1)
                .with_for_update(of=RecordingJob, skip_locked=True)
            )
        ).first()
        if row is None:
            return False
        job, call = row

        try:
            summary = CallSummary.model_validate(job.summary)
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                targets = await destinations(call, summary, client, settings)
        except ValidationError:
            job.bitrix_routing_status = "failed"
            log.warning("Звонок %s: неверный формат сохранённой выжимки", call.id)
            return True
        except (BitrixError, TimeoutError) as error:
            retryable = isinstance(error, TimeoutError) or error.retryable
            if retryable and job.bitrix_route_attempts < _MAX_RETRIES:
                delay = _RETRY_DELAY * (2**job.bitrix_route_attempts)
                job.bitrix_route_due_at = datetime.now(UTC) + delay
                job.bitrix_route_attempts += 1
            else:
                job.bitrix_routing_status = "failed"
                log.warning("Звонок %s: не удалось выбрать адресатов Bitrix24 (%s)", call.id, error)
            return True

        for (kind, target_id), caller_details in targets.items():
            session.add(
                BitrixDelivery(
                    call_id=call.id,
                    destination=kind,
                    target_id=target_id,
                    caller_details=caller_details,
                )
            )
        job.bitrix_routing_status = "resolved" if targets else "skipped"
        return True


async def _reconcile(delivery: BitrixDelivery, client: Bitrix, marker: str) -> None:
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            external_id = await client.find_sent(delivery.destination, delivery.target_id, marker)
    except (BitrixError, TimeoutError) as error:
        retryable = isinstance(error, TimeoutError) or error.retryable
        if retryable and delivery.attempts < _MAX_RETRIES:
            delivery.next_attempt_at = datetime.now(UTC) + _RETRY_DELAY * (2**delivery.attempts)
            delivery.attempts += 1
            return
        external_id = None

    delivery.external_id = external_id
    delivery.status = "sent" if external_id else "uncertain"
    delivery.last_error = None if external_id else "Не удалось подтвердить доставку в Bitrix24"


async def deliver_next(client: Bitrix) -> bool:
    async with SessionLocal.begin() as session:
        row = (
            await session.execute(
                select(BitrixDelivery, Call, RecordingJob.summary)
                .join(Call, Call.id == BitrixDelivery.call_id)
                .join(RecordingJob, RecordingJob.call_id == BitrixDelivery.call_id)
                .where(
                    BitrixDelivery.status.in_(("pending", "sending")),
                    BitrixDelivery.next_attempt_at <= datetime.now(UTC),
                )
                .order_by(
                    BitrixDelivery.next_attempt_at,
                    BitrixDelivery.call_id,
                    BitrixDelivery.destination,
                    BitrixDelivery.target_id,
                )
                .limit(1)
                .with_for_update(of=BitrixDelivery, skip_locked=True)
            )
        ).first()
        if row is None:
            return False
        delivery, call, summary = row
        marker = _marker(call)
        if delivery.status == "sending":
            await _reconcile(delivery, client, marker)
            return True
        try:
            caller_details = None
            if delivery.destination == "chat":
                caller_details = delivery.caller_details or _caller_phone_line(call)
            message = _message(marker, CallSummary.model_validate(summary), caller_details)
        except ValidationError:
            delivery.status = "failed"
            delivery.last_error = "Неверный формат сохранённой выжимки"
            return True

        delivery.status = "sending"
        delivery.next_attempt_at = datetime.now(UTC) + _SEND_WINDOW
        key = (delivery.call_id, delivery.destination, delivery.target_id)

    error = None
    external_id = None
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            external_id = await client.send(key[1], key[2], message)
    except TimeoutError:
        error = BitrixError("Истекло время ожидания Bitrix24", uncertain=True)
    except BitrixError as exc:
        error = exc

    async with SessionLocal.begin() as session:
        delivery = await session.get(BitrixDelivery, key, with_for_update=True)
        if delivery is None or delivery.status != "sending":
            return True
        if error is None:
            delivery.external_id = external_id
            delivery.status = "sent"
            delivery.last_error = None
            return True

        delivery.last_error = str(error)
        if error.uncertain:
            delivery.next_attempt_at = datetime.now(UTC) + _VERIFY_DELAY
            delivery.attempts = 0
        elif error.retryable and delivery.attempts < _MAX_RETRIES:
            delay = _RETRY_DELAY * (2**delivery.attempts)
            delivery.status = "pending"
            delivery.next_attempt_at = datetime.now(UTC) + delay
            delivery.attempts += 1
        else:
            delivery.status = "failed"
    return True


async def run_bitrix_delivery(settings: Settings) -> None:
    # Вебхук содержит секрет прямо в URL, который HTTPX выводит при уровне INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(timeout=25) as http:
        try:
            client = Bitrix(http, settings.bitrix_webhook_url.get_secret_value())
        except ValueError as error:
            log.error("Bitrix24: %s", error)
            return

        while True:
            worked = False
            try:
                routed = await route_next(client, settings)
                delivered = await deliver_next(client)
                worked = routed or delivered
            except Exception as error:
                # Не выводим текст исключения, в котором может оказаться URL вебхука.
                log.error("Ошибка фоновой доставки в Bitrix24 (%s)", type(error).__name__)
            await asyncio.sleep(1 if worked else 3)
