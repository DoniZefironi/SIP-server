import asyncio
import hmac
import struct
import wave
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import ValidationError
from sqlalchemy import select, update
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
from app.services.voice_bot import MAX_QUESTION_BYTES, SpeechKitVoice, VoiceError, question_pcm

router = APIRouter(prefix="/internal/telephony", tags=["internal: telephony"])


async def _savtassist(
    settings: Annotated[Settings, Depends(get_settings)],
    supplied_secret: Annotated[str | None, Header(alias="X-Telephony-Secret")] = None,
) -> AsyncIterator[SavtAssist]:
    secret = settings.savtassist_api_secret.get_secret_value().strip()
    if not secret:
        raise HTTPException(503, "Интеграция с SavtAssist не настроена")
    if not secret.isascii():
        raise HTTPException(503, "Некорректный секрет SavtAssist")
    if not supplied_secret or not hmac.compare_digest(supplied_secret.encode(), secret.encode()):
        raise HTTPException(403, "Доступ запрещён")
    async with httpx.AsyncClient() as http:
        try:
            client = SavtAssist(http, settings.savtassist_base_url, secret)
        except (ValueError, httpx.InvalidURL):
            raise HTTPException(503, "Некорректный адрес SavtAssist") from None
        yield client


@router.post("/calls/{linkedid}/resolve-object")
async def resolve_call_object(
    linkedid: Annotated[str, Path(min_length=1, max_length=64)],
    request: ObjectLookupRequest,
    client: Annotated[SavtAssist, Depends(_savtassist)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CallObjectLookup:
    """Найти объект и сохранить результат до завершения звонка."""
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
    try:
        return await client.answer_question(request)
    except SavtAssistError as error:
        raise HTTPException(502, str(error)) from None


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
) -> Response:
    """Распознать вопрос из WAV и вернуть озвученный ответ."""
    key = settings.speechkit_api_key.get_secret_value().strip()
    if not key or not key.isascii():
        raise HTTPException(503, "Ключ SpeechKit для голосового бота не настроен")

    audio = bytearray()
    async for chunk in request.stream():
        if len(audio) + len(chunk) > MAX_QUESTION_BYTES:
            raise HTTPException(413, "Запись вопроса слишком велика")
        audio.extend(chunk)
    try:
        pcm = question_pcm(audio)
    except (ValueError, wave.Error, EOFError, struct.error) as error:
        raise HTTPException(422, "Неверный формат записи вопроса") from error

    try:
        async with asyncio.timeout(15), httpx.AsyncClient(timeout=5) as http:
            speech = SpeechKitVoice(http, key)
            question = await speech.recognize(pcm)
            if not question:
                return Response(status_code=204)
            result = await client.answer_question(QuestionRequest(question=question))
            if result.answer is None:
                return Response(status_code=204)
            answer = await speech.synthesize(result.answer)
    except (VoiceError, SavtAssistError) as error:
        raise HTTPException(502, str(error)) from None
    except ValidationError:
        raise HTTPException(502, "Некорректный результат распознавания") from None
    except TimeoutError:
        raise HTTPException(504, "Истекло время ожидания ответа бота") from None
    return Response(answer, media_type="audio/wav")
