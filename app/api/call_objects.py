import asyncio
import base64
import binascii
import hmac
import logging
import struct
import wave
from collections.abc import AsyncIterator
from time import perf_counter
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import Call
from app.db.session import get_session
from app.services.savtassist import (
    CallObjectLookup,
    ObjectLookupRequest,
    QuestionAnswer,
    QuestionRequest,
    SavtAssist,
    SavtAssistError,
)
from app.services.voice_bot import (
    MAX_QUESTION_BYTES,
    MAX_VOICE_TURN_BYTES,
    MAX_VOICE_WAV_BYTES,
    VOICE_RECORDING_SECONDS,
    SpeechKitVoice,
    VoiceError,
    VoiceExchange,
    VoiceTurn,
    VoiceTurnRequest,
    confirms_reference,
    question_pcm,
    question_with_history,
    reference_confirmation,
    skips_reference,
    spoken_reference,
    voice_intent,
)
from app.services.voice_safety import answer_requires_operator, safety_route

router = APIRouter(prefix="/internal/telephony", tags=["internal: telephony"])
log = logging.getLogger(__name__)


async def _cloud_http(request: Request) -> AsyncIterator[httpx.AsyncClient]:
    client = getattr(request.app.state, "voice_http", None)
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=5) as client:
            yield client


CloudHTTP = Annotated[httpx.AsyncClient, Depends(_cloud_http)]


async def _savtassist(
    settings: Annotated[Settings, Depends(get_settings)],
    http: CloudHTTP,
    supplied_secret: Annotated[str | None, Header(alias="X-Telephony-Secret")] = None,
) -> SavtAssist:
    secret = settings.savtassist_api_secret.get_secret_value().strip()
    if not secret:
        raise HTTPException(503, "Интеграция с SavtAssist не настроена")
    if not secret.isascii():
        raise HTTPException(503, "Некорректный секрет SavtAssist")
    if not supplied_secret or not hmac.compare_digest(supplied_secret.encode(), secret.encode()):
        raise HTTPException(403, "Доступ запрещён")
    try:
        return SavtAssist(http, settings.savtassist_base_url, secret)
    except (ValueError, httpx.InvalidURL):
        raise HTTPException(503, "Некорректный адрес SavtAssist") from None


@router.post("/calls/{linkedid}/resolve-object")
async def resolve_call_object(
    linkedid: Annotated[str, Path(min_length=1, max_length=64)],
    request: ObjectLookupRequest,
    client: Annotated[SavtAssist, Depends(_savtassist)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallObjectLookup:
    """Найти объект и сохранить результат до завершения звонка."""
    return await _resolve_for_call(linkedid, request, client, session)


async def _resolve_for_call(
    linkedid: str, request: ObjectLookupRequest, client: SavtAssist, session: AsyncSession
) -> CallObjectLookup:
    async with session.begin():
        row = (
            await session.execute(
                select(Call.id, Call.ended_at).where(Call.linkedid == linkedid)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(404, "Звонок не найден")
        call_id, ended_at = row
        if ended_at is not None:
            raise HTTPException(409, "Звонок уже завершён")

    try:
        result = await client.resolve_object(request)
    except SavtAssistError as error:
        raise HTTPException(502, str(error)) from None

    async with session.begin():
        saved = await session.scalar(
            update(Call)
            .where(Call.id == call_id, Call.ended_at.is_(None))
            .values(object_lookup=result.model_dump())
            .returning(Call.id)
        )
        if saved is None:
            raise HTTPException(409, "Звонок завершился во время поиска")
    return result


@router.post("/answer-question")
async def answer_question(
    request: QuestionRequest,
    client: Annotated[SavtAssist, Depends(_savtassist)],
) -> QuestionAnswer:
    """Получить короткий ответ на общий вопрос для озвучки."""
    if safety_route(request.question):
        return QuestionAnswer(status="needs_operator")
    try:
        result = await client.answer_question(request)
    except SavtAssistError as error:
        raise HTTPException(502, str(error)) from None
    if result.answer is not None and answer_requires_operator(result.answer):
        return QuestionAnswer(status="needs_operator")
    return result


async def _read_body(request: Request, limit: int) -> bytearray:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "Запись вопроса слишком велика")
        body.extend(chunk)
    return body


def _voice_pcm(audio: bytes | bytearray, *, dialogue: bool = False) -> bytes:
    limit = MAX_VOICE_WAV_BYTES if dialogue else MAX_QUESTION_BYTES
    if len(audio) > limit:
        raise HTTPException(413, "Запись вопроса слишком велика")
    try:
        if dialogue:
            return question_pcm(audio, max_frames=VOICE_RECORDING_SECONDS * 8000)
        return question_pcm(audio)
    except (ValueError, wave.Error, EOFError, struct.error):
        raise HTTPException(422, "Неверный формат записи вопроса") from None


def _speechkit(settings: Settings, http: httpx.AsyncClient) -> SpeechKitVoice:
    key = settings.speechkit_api_key.get_secret_value().strip()
    if not key or not key.isascii():
        raise HTTPException(503, "Ключ SpeechKit для голосового бота не настроен")
    return SpeechKitVoice(http, key)


async def _reference_turn(
    text: str, dialogue: VoiceTurnRequest, client: SavtAssist, speech: SpeechKitVoice,
    session: AsyncSession,
) -> tuple[VoiceTurn, bytes] | None:
    if dialogue.reference is not None and confirms_reference(text):
        try:
            if dialogue.linkedid:
                lookup = await _resolve_for_call(
                    dialogue.linkedid, dialogue.reference, client, session
                )
            else:
                lookup = await client.resolve_object(dialogue.reference)
        except (SavtAssistError, HTTPException, SQLAlchemyError) as error:
            log.warning("voice reference lookup failed type=%s", type(error).__name__)
            return VoiceTurn(status="reference_unavailable"), b""
        status = "reference_found" if lookup.status == "found" else "reference_unavailable"
        return VoiceTurn(status=status), b""

    if skips_reference(text):
        return VoiceTurn(status="ask_question"), b""
    reference = spoken_reference(text)
    if reference is not None:
        return (
            VoiceTurn(status="confirm_reference", reference=reference),
            await speech.synthesize(reference_confirmation(reference)),
        )
    return None


async def _process_audio(
    pcm: bytes,
    client: SavtAssist,
    speech: SpeechKitVoice,
    dialogue: VoiceTurnRequest | None = None,
    session: AsyncSession | None = None,
) -> tuple[VoiceTurn, bytes]:
    try:
        async with asyncio.timeout(15):
            started = perf_counter()
            question = await speech.recognize(pcm)
            recognized = perf_counter()
            safety = safety_route(question)
            if safety is not None:
                log.info("voice turn %s stt_ms=%d", safety, (recognized - started) * 1000)
                return VoiceTurn(status=safety), b""
            if dialogue is not None:
                intent = voice_intent(question)
                if intent != "question":
                    return VoiceTurn(status=intent), b""
                if dialogue.stage != "question" and session is not None:
                    reference_turn = await _reference_turn(
                        question, dialogue, client, speech, session
                    )
                    if reference_turn is not None:
                        return reference_turn
            elif not question:
                return VoiceTurn(status="repeat"), b""

            query = question_with_history(question, dialogue.history if dialogue else [])
            result = await client.answer_question(QuestionRequest(question=query))
            looked_up = perf_counter()
            if result.answer is None:
                log.info(
                    "voice turn operator stt_ms=%d knowledge_ms=%d",
                    (recognized - started) * 1000,
                    (looked_up - recognized) * 1000,
                )
                return VoiceTurn(status="operator"), b""
            if answer_requires_operator(result.answer):
                log.info("voice turn urgent_operator reason=knowledge_answer")
                return VoiceTurn(status="urgent_operator"), b""

            audio = await speech.synthesize(result.answer)
            log.info(
                "voice turn answered stt_ms=%d knowledge_ms=%d tts_ms=%d",
                (recognized - started) * 1000,
                (looked_up - recognized) * 1000,
                (perf_counter() - looked_up) * 1000,
            )
            return VoiceTurn(
                status="answered",
                exchange=VoiceExchange(question=question[:500], answer=result.answer),
            ), audio
    except (VoiceError, SavtAssistError) as error:
        raise HTTPException(502, str(error)) from None
    except ValidationError:
        raise HTTPException(502, "Некорректный результат распознавания") from None
    except TimeoutError:
        raise HTTPException(504, "Истекло время ожидания ответа бота") from None


@router.post(
    "/answer-audio",
    response_class=Response,
    responses={
        200: {"content": {"audio/wav": {}}},
        204: {"description": "Нужно передать звонок сотруднику"},
    },
)
async def answer_audio(
    request: Request,
    client: Annotated[SavtAssist, Depends(_savtassist)],
    settings: Annotated[Settings, Depends(get_settings)],
    http: CloudHTTP,
) -> Response:
    """Распознать вопрос из WAV и вернуть озвученный ответ."""
    speech = _speechkit(settings, http)
    pcm = _voice_pcm(await _read_body(request, MAX_QUESTION_BYTES))
    turn, audio = await _process_audio(pcm, client, speech)
    if turn.status != "answered":
        return Response(status_code=204)
    return Response(audio, media_type="audio/wav")


@router.post("/voice-turn", response_model_exclude_none=True)
async def voice_turn(
    request: Request,
    client: Annotated[SavtAssist, Depends(_savtassist)],
    settings: Annotated[Settings, Depends(get_settings)],
    http: CloudHTTP,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> VoiceTurn:
    """Обработать одну реплику. Короткий контекст хранится только в процессе AGI."""
    speech = _speechkit(settings, http)
    body = await _read_body(request, MAX_VOICE_TURN_BYTES)
    try:
        data = VoiceTurnRequest.model_validate_json(body)
        pcm = _voice_pcm(base64.b64decode(data.audio, validate=True), dialogue=True)
    except (ValidationError, binascii.Error, ValueError):
        raise HTTPException(422, "Некорректная реплика голосового бота") from None
    turn, audio = await _process_audio(pcm, client, speech, data, session)
    if audio:
        turn.audio = base64.b64encode(audio).decode("ascii")
    return turn
