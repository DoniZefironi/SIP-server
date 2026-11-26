import argparse
import asyncio
import io
import re
import wave
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import get_settings
from app.services.savtassist import ObjectKind, ObjectLookupRequest, QuestionRequest

MAX_QUESTION_BYTES = 256_000
VOICE_RECORDING_SECONDS = 29
MAX_VOICE_WAV_BYTES = VOICE_RECORDING_SECONDS * 8000 * 2 + 44
MAX_VOICE_TURN_BYTES = 640_000
_MAX_QUESTION_FRAMES = 13 * 8000
_MAX_ANSWER_BYTES = 2_000_000
_PCM = {"format": "lpcm", "sampleRateHertz": "8000", "lang": "ru-RU"}
_STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
_INTRO = (
    "Здравствуйте, я виртуальный оператор Ася. Чем могу помочь? "
    "Назовите номер объекта или шкафа либо сразу задайте вопрос."
)
_TRANSFER = "Чтобы связаться с сотрудником, нажмите ноль или скажите «оператор»."
_VOICEMAIL = "Чтобы оставить сообщение, нажмите ноль или скажите «оставить сообщение»."
_FINISH = "Если всё ясно, нажмите один или скажите «вопрос решён»."
_PROMPTS = {
    "bot_question": _INTRO,
    "bot_feedback": f"Можно задать ещё вопрос. {_TRANSFER} {_FINISH}",
    "bot_question_afterhours": _INTRO,
    "bot_feedback_afterhours": f"Можно задать ещё вопрос. {_VOICEMAIL} {_FINISH}",
    "bot_ask_question": "Хорошо, задайте ваш вопрос.",
    "bot_reference_found": "Спасибо, нашла номер. Задайте ваш вопрос.",
    "bot_reference_unavailable": "Не удалось найти номер. Задайте вопрос, я попробую помочь.",
    "bot_retry": "Да, я слушаю. Чем могу помочь?",
    "bot_retry_long": "Сформулируйте вопрос короче, пожалуйста.",
    "bot_wait": "Тактактак",
    "bot_no_answer": f"Точного ответа у меня нет. Можете задать другой вопрос. {_TRANSFER}",
    "bot_no_answer_afterhours": (
        f"Точного ответа у меня нет. Можете задать другой вопрос. {_VOICEMAIL}"
    ),
    "bot_emergency": "Срочно звоните один, один, два. Отойдите в безопасное место.",
    "bot_urgent_operator": (
        "При дыме, огне или угрозе людям сразу звоните один, один, два."
    ),
}
_OPERATOR = re.compile(
    r"(?:"
    r"(?:(?:мне )?(?:нужен|нужна) |позовите |дайте )?"
    r"(?:оператор[а]?|сотрудник[а]?|специалист[а]?)"
    r"|(?:соедините|свяжите|переключите)(?: меня)? "
    r"(?:с(?:о)? (?:оператором|специалистом|сотрудником)|на (?:оператора|специалиста|сотрудника))"
    r"|(?:я )?хочу (?:поговорить|связаться) с(?:о)? (?:оператором|специалистом|сотрудником)"
    r"|(?:(?:я )?хочу )?оставить (?:голосовое )?сообщение"
    r")"
)
_DONE = {
    "вопрос решен", "спасибо вопрос решен", "ответ помог", "все ясно", "все понятно",
    "спасибо все понятно", "спасибо все ясно", "вопросов больше нет", "до свидания",
}
_GREETING = {"алло", "здравствуйте", "здравствуй", "здрасте", "здрасьте", "привет"}
_FILLER = re.compile(r"(?:та+к)+|эм+|м+|э+|ну|угу")
_SHORT_FOLLOWUPS = {"когда", "где", "почему", "зачем", "куда", "откуда", "сколько", "как"}
_QUESTION_WORDS = re.compile(
    r"\b(?:где|куда|откуда|когда|почему|зачем|сколько|кто|что|чем|как\w*|можно|ли"
    r"|помогите|помощь|подскаж\w*|объясните|расскажите|покажите|скажите)\b"
    r"|\bне (?:могу|получается|работает|запускается|включается|открывается)\b"
)
_FOLLOWUP = re.compile(
    r"\b(?:ее|его|их|там|туда|это|этот|эту|эта|эти|этого|этой|этом)\b"
    r"|^(?:а )?(?:что дальше|как дальше|где именно|куда именно|как именно)$"
)
_NO_REFERENCE = {
    "нет номера", "номера нет", "номер не знаю", "не знаю номера", "не знаю номер",
    "не знаю", "я не знаю номера", "я не знаю номер", "у меня нет номера", "без номера",
    "пропустить", "нет", "неверно", "нет неверно", "не тот номер",
}
_CONFIRMED = {"да", "верно", "да верно", "да правильно", "правильно", "все верно", "да все верно"}
_REFERENCE_PREFIX = re.compile(
    r"^(?:у меня )?(?:номер\s+)?(шкаф[а]?|объект[а]?|проект[а]?)\b[\s:№,]*"
)
_SEPARATORS = {
    "_": "_", "подчеркивание": "_", "нижнее подчеркивание": "_",
    "-": "-", "дефис": "-", "тире": "-", "/": "/", "дробь": "/",
}
_NUMBER_WORDS = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}


class VoiceReference(ObjectLookupRequest):
    number: str = Field(min_length=1, max_length=100, pattern=r"^[0-9]+(?:[_/-][0-9]+)*$")


class VoiceExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=600)


class VoiceTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audio: str = Field(max_length=((MAX_VOICE_WAV_BYTES + 2) // 3) * 4)
    history: list[VoiceExchange] = Field(default_factory=list, max_length=2)
    stage: Literal["reference", "confirm_reference", "question"] = "question"
    reference: VoiceReference | None = None
    linkedid: str | None = Field(default=None, min_length=1, max_length=64)
    prepare_question: bool = False

    @model_validator(mode="after")
    def require_reference_for_confirmation(self) -> "VoiceTurnRequest":
        if (self.stage == "confirm_reference") != (self.reference is not None):
            raise ValueError("Номер требуется при подтверждении")
        return self


class VoiceAnswerRequest(QuestionRequest):
    history: list[VoiceExchange] = Field(default_factory=list, max_length=2)


class VoiceTurn(BaseModel):
    status: Literal[
        "answered", "operator", "done", "repeat", "emergency", "urgent_operator", "no_answer",
        "confirm_reference", "reference_found", "reference_unavailable", "ask_question",
        "question_ready",
    ]
    audio: str | None = None
    exchange: VoiceExchange | None = None
    reference: VoiceReference | None = None
    question: str | None = None
    thinking: bool | None = None


def _normalized_words(text: str) -> str:
    words = " ".join(re.findall(r"[^\W_]+", text.casefold().replace("ё", "е")))
    return words.removeprefix("пожалуйста ").removesuffix(" пожалуйста")


def voice_intent(text: str) -> Literal["question", "operator", "done", "repeat"]:
    words = _normalized_words(text)
    if words in {"добрый день", "добрый вечер", "доброе утро"} or all(
        word in _GREETING or _FILLER.fullmatch(word) for word in words.split()
    ):
        return "repeat"
    command = words.removeprefix("алло ").removeprefix("да ").removeprefix("нет ")
    if _OPERATOR.fullmatch(command):
        return "operator"
    if command in _DONE:
        return "done"
    return "question"


def has_spoken_question(text: str, history: list[VoiceExchange]) -> bool:
    words = _normalized_words(text)
    content = [
        word for word in words.split() if word not in _GREETING and not _FILLER.fullmatch(word)
    ]
    return len(content) > 1 or (bool(history) and words in _SHORT_FOLLOWUPS)


def needs_thinking_prompt(text: str) -> bool:
    return text.endswith("?") or bool(_QUESTION_WORDS.search(_normalized_words(text)))


def skips_reference(text: str) -> bool:
    return _normalized_words(text) in _NO_REFERENCE


def confirms_reference(text: str) -> bool:
    return _normalized_words(text) in _CONFIRMED


def _number_groups(text: str) -> list[str]:
    groups = []
    value = None
    previous_rank = 0
    for part in text.replace(",", " ").split():
        if re.fullmatch(r"[0-9]+", part):
            if value is not None:
                groups.append(str(value))
                value = None
            groups.append(part)
            continue
        word = _NUMBER_WORDS.get(part)
        if word is None:
            return []
        rank = 3 if word >= 100 else 2 if word >= 10 else 1
        if value is not None and rank >= previous_rank:
            groups.append(str(value))
            value = None
        value = (value or 0) + word
        previous_rank = 1 if 10 <= word < 20 else rank
    if value is not None:
        groups.append(str(value))
    return groups


def spoken_reference(text: str) -> VoiceReference | None:
    text = text.casefold().replace("ё", "е").strip(" ,.!?")
    text = re.sub(r"^(?:(?:нет|алло|здравствуйте)[, ]+)", "", text)
    prefix = _REFERENCE_PREFIX.match(text)
    kind: ObjectKind | None = None
    if prefix:
        kind = "cabinet" if prefix[1].startswith("шкаф") else "project"
        text = text[prefix.end():]
    else:
        text = text.removeprefix("номер ").removeprefix("№").lstrip()

    separators = r"([_/-]|\b(?:нижнее подчеркивание|подчеркивание|дефис|тире|дробь)\b)"
    parts = re.split(separators, text)
    number_parts = []
    for segment in parts[::2]:
        groups = _number_groups(segment)
        if not groups:
            return None
        number_parts.append("_".join(groups))
    number = number_parts[0] + "".join(
        _SEPARATORS[separator] + part
        for separator, part in zip(parts[1::2], number_parts[1:], strict=True)
    )
    if len(number) > 100:
        return None
    return VoiceReference(number=number, kind=kind)


def reference_confirmation(reference: VoiceReference) -> str:
    # Паузы между группами помогают отличить 25_125 от 25125.
    spoken = reference.number.replace("_", ", ").replace("-", " дефис ").replace("/", " дробь ")
    spoken = re.sub(r"\b0[0-9]+", lambda match: " ".join(match[0]), spoken)
    return f"Я услышала номер {spoken}. Верно? Скажите да или нет."


def question_with_history(question: str, history: list[VoiceExchange]) -> str:
    words = _normalized_words(question)
    if not history or (words not in _SHORT_FOLLOWUPS and not _FOLLOWUP.search(words)):
        return question
    current = f"Текущий вопрос: {question}"
    if len(current) >= 2000:
        return question
    previous = "\n".join(
        f"Клиент: {exchange.question[:240]}\nБот: {exchange.answer[:320]}"
        for exchange in history
    )
    context = f"\n\nКонтекст для уточнений к текущему вопросу:\n{previous}"
    return current + context[:2000 - len(current)]


class VoiceError(Exception):
    pass


def question_pcm(audio: bytes | bytearray, max_frames: int = _MAX_QUESTION_FRAMES) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 8000):
            raise ValueError("Ожидается WAV PCM, 8 кГц, моно, 16 бит")
        frames = wav.getnframes()
        if frames > max_frames:
            raise ValueError("Запись вопроса слишком длинная")
        pcm = wav.readframes(frames)
        if len(pcm) != frames * 2:
            raise ValueError("Запись вопроса неполная")
    return pcm


class SpeechKitVoice:
    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._headers = {"Authorization": f"Api-Key {api_key}", "x-data-logging-enabled": "false"}

    async def recognize(self, pcm: bytes) -> str:
        if not pcm.strip(b"\x00"):
            return ""
        try:
            response = await self._http.post(
                _STT_URL,
                params=_PCM,
                content=pcm,
                headers={**self._headers, "Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            text = response.json()["result"]
            if not isinstance(text, str):
                raise ValueError
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise VoiceError("SpeechKit не распознал вопрос") from None
        return text.strip()

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        params = {**_PCM, "voice": "alena", "text": text}
        if speed != 1.0:
            params["speed"] = str(speed)
        try:
            response = await self._http.post(_TTS_URL, data=params, headers=self._headers)
            response.raise_for_status()
        except httpx.HTTPError:
            raise VoiceError("SpeechKit не синтезировал ответ") from None
        pcm = response.content
        if not 0 < len(pcm) <= _MAX_ANSWER_BYTES or len(pcm) % 2:
            raise VoiceError("SpeechKit вернул некорректный звук")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            wav.writeframes(pcm)
        return buffer.getvalue()


async def _synthesize_prompts() -> dict[str, bytes]:
    key = get_settings().speechkit_api_key.get_secret_value().strip()
    if not key:
        raise SystemExit("Укажи TELEPHONY_SPEECHKIT_API_KEY")
    async with httpx.AsyncClient(timeout=8) as http:
        speech = SpeechKitVoice(http, key)
        return {
            name: await speech.synthesize(text, speed=1.55 if name == "bot_wait" else 1.0)
            for name, text in _PROMPTS.items()
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Подготовить WAV-подсказки для голосового пилота")
    parser.add_argument("directory", type=Path)
    directory = parser.parse_args().directory
    try:
        prompts = asyncio.run(_synthesize_prompts())
        directory.mkdir(parents=True, exist_ok=True)
        for name, audio in prompts.items():
            (directory / f"{name}.wav").write_bytes(audio)
    except VoiceError as error:
        raise SystemExit(str(error)) from None
