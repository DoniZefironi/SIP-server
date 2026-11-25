import argparse
import asyncio
import io
import wave
from pathlib import Path

import httpx

from app.config import get_settings

MAX_QUESTION_BYTES = 256_000
_MAX_QUESTION_FRAMES = 13 * 8000
_MAX_ANSWER_BYTES = 2_000_000
_PCM = {"format": "lpcm", "sampleRateHertz": "8000", "lang": "ru-RU"}
_STT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
_TTS_URL = "https://tts.api.cloud.yandex.net/speech/v1/tts:synthesize"
_PROMPTS = {
    "bot_question": (
        "После сигнала задайте вопрос или нажмите ноль, чтобы связаться с сотрудником."
    ),
    "bot_feedback": (
        "Вам помог ответ? Если да, нажмите один. Чтобы связаться с сотрудником, нажмите два."
    ),
}


class VoiceError(Exception):
    pass


def question_pcm(audio: bytes) -> bytes:
    with wave.open(io.BytesIO(audio), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 8000):
            raise ValueError("Ожидается WAV PCM, 8 кГц, моно, 16 бит")
        frames = wav.getnframes()
        if frames > _MAX_QUESTION_FRAMES:
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

    async def synthesize(self, text: str) -> bytes:
        try:
            response = await self._http.post(
                _TTS_URL, data={**_PCM, "voice": "alena", "text": text}, headers=self._headers
            )
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
        return {name: await speech.synthesize(text) for name, text in _PROMPTS.items()}


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
