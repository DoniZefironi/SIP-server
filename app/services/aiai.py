import asyncio
import io
import wave
from dataclasses import dataclass

import httpx

from app.config import Settings

_MAX_SPEECH_BYTES = 8_000_000
_WAV_FORMAT = (1, 2, 8000)


class AiaiError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class AiaiAudioError(AiaiError):
    pass


def aiai_connection(settings: Settings) -> tuple[str, dict[str, str]]:
    key = settings.aiai_api_key.get_secret_value().strip()
    if not key:
        raise AiaiError("Для AIAI.BY нужен TELEPHONY_AIAI_API_KEY")
    try:
        url = httpx.URL(settings.aiai_base_url.strip())
    except httpx.InvalidURL:
        raise AiaiError("Некорректный TELEPHONY_AIAI_BASE_URL") from None
    if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
        raise AiaiError("Для TELEPHONY_AIAI_BASE_URL нужен HTTPS-адрес без параметров и логина")
    return str(url).rstrip("/"), {"Authorization": f"Bearer {key}"}


@dataclass(frozen=True, slots=True)
class Phrase:
    start_ms: int
    text: str


def mono_wav(pcm: bytes, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return buffer.getvalue()


def _phrases(data: dict, text: str) -> list[Phrase]:
    segments = data.get("segments")
    if not isinstance(segments, list):
        if text:
            raise ValueError("missing segments")
        return []
    phrases = []
    for segment in segments:
        start = float(segment["start"])
        if not 0 <= start <= 24 * 60 * 60:
            raise ValueError("invalid timestamp")
        phrase = segment["text"].strip()
        if phrase:
            phrases.append(Phrase(round(start * 1000), phrase))
    if text and not phrases:
        raise ValueError("missing phrases")
    return phrases


def _is_phone_wav(content: bytes) -> bool:
    try:
        with wave.open(io.BytesIO(content), "rb") as wav:
            frames = wav.getnframes()
            return (
                (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == _WAV_FORMAT
                and 0 < frames < 8000 * 125 and len(wav.readframes(frames)) == frames * 2
            )
    except (wave.Error, EOFError):
        return False


async def _phone_wav(content: bytes, speed: float) -> bytes:
    if not content or len(content) > _MAX_SPEECH_BYTES:
        raise AiaiAudioError("AIAI.BY вернул некорректный размер аудио")
    if speed == 1.0 and _is_phone_wav(content):
        return content
    filters = ("-af", f"atempo={speed}") if speed != 1.0 else ()
    try:
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "pipe",
            "-i", "pipe:0", "-vn", *filters, "-t", "125",
            "-f", "s16le", "-ac", "1", "-ar", "8000", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise AiaiError("Не удалось запустить преобразование звука AIAI.BY") from None
    try:
        pcm, _ = await process.communicate(content)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
    if process.returncode or not 0 < len(pcm) < 2_000_000 or len(pcm) % 2:
        raise AiaiAudioError(
            f"AIAI.BY вернул непригодное аудио (FFmpeg {process.returncode}, PCM {len(pcm)} байт)"
        )
    return mono_wav(pcm)


class AiaiSpeech:
    def __init__(self, http: httpx.AsyncClient, settings: Settings) -> None:
        self._http = http
        self._url, self._headers = aiai_connection(settings)
        self._voice_stt = settings.aiai_voice_stt_model.strip()
        self._recording_stt = settings.aiai_recording_stt_model.strip()
        self._tts = settings.aiai_tts_model.strip()
        self._voice = settings.aiai_tts_voice.strip()

    async def _request(self, endpoint: str, **kwargs) -> httpx.Response:
        try:
            response = await self._http.post(
                f"{self._url}/audio/{endpoint}", headers=self._headers, **kwargs,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise AiaiError("Не удалось подключиться к AIAI.BY", retryable=True) from error
        except httpx.RequestError as error:
            raise AiaiError("Результат аудиозапроса AIAI.BY неизвестен") from error
        if not response.is_success:
            status = response.status_code
            raise AiaiError(f"AIAI.BY вернул HTTP {status}", retryable=status in (402, 429))
        return response

    async def _transcribe(
        self, audio: bytes, model: str, *, with_phrases: bool = False,
    ) -> tuple[str, list[Phrase]]:
        response = await self._request(
            "transcriptions",
            data={"model": model, "language": "ru", "response_format": (
                "verbose_json" if with_phrases else "json"
            )},
            files={"file": ("call.wav", audio, "audio/wav")},
        )
        try:
            data = response.json()
            text = data["text"].strip()
            return text, _phrases(data, text) if with_phrases else []
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as error:
            raise AiaiError("AIAI.BY вернул некорректную расшифровку или таймкоды") from error

    async def recognize(self, pcm: bytes) -> str:
        if not pcm.strip(b"\x00"):
            return ""
        text, _ = await self._transcribe(mono_wav(pcm), self._voice_stt)
        return text

    async def transcribe_recording(
        self, audio: bytes, *, with_phrases: bool = False,
    ) -> tuple[str, list[Phrase]]:
        return await self._transcribe(audio, self._recording_stt, with_phrases=with_phrases)

    async def synthesize(self, text: str, *, speed: float = 1.0) -> bytes:
        response = await self._request("speech", json={
            "model": self._tts, "input": text, "voice": self._voice,
            "response_format": "mp3",
        })
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type == "application/json" or content_type.startswith("text/"):
            raise AiaiAudioError("AIAI.BY вернул текст вместо аудио")
        return await _phone_wav(response.content, speed)
