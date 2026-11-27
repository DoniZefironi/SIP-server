import json

import httpx
import pytest

from app.config import Settings
from app.services import voice_bot
from app.services.aiai import AiaiError, AiaiSpeech, mono_wav
from app.services.aiai_recordings import recognize_tracks
from app.services.voice_bot import voice_speech


def _settings() -> Settings:
    return Settings(_env_file=None, speech_provider="aiai", aiai_api_key="test-key")


async def test_voice_provider_uses_aiai_for_recognition_and_synthesis() -> None:
    audio = mono_wav(b"\x20\x00" * 800)
    endpoints = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.url.host == "api.aiai.by"
        endpoints.append(request.url.path)
        if request.url.path.endswith("/transcriptions"):
            assert b"nova-3" in request.content and b"RIFF" in request.content
            return httpx.Response(200, json={"text": "Как найти инструкцию?"})
        body = json.loads(request.content)
        assert body["model"] == "eleven-flash-v2.5"
        assert body["voice"] == "lily" and body["response_format"] == "mp3"
        return httpx.Response(200, content=audio)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        speech = voice_speech(_settings(), http)
        assert await speech.recognize(audio[44:]) == "Как найти инструкцию?"
        assert await speech.synthesize("Ответ") == audio
    assert endpoints == ["/v1/audio/transcriptions", "/v1/audio/speech"]


async def test_recordings_keep_real_roles_and_bot_boundary(tmp_path) -> None:
    tracks = [tmp_path / "call-client.wav", tmp_path / "call-staff.wav"]
    speech_pcm = b"\x20\x00" * 8000
    tracks[0].write_bytes(mono_wav(b"\x00" * 32_000 + speech_pcm))
    tracks[1].write_bytes(mono_wav(speech_pcm + b"\x00" * 64_000 + speech_pcm))
    results = iter([
        {"text": "Вопрос", "segments": [{"start": 0, "text": "Вопрос"}]},
        {"text": "Привет", "segments": [{"start": 0, "text": "Привет"}]},
        {"text": "Ответ", "segments": [{"start": 0, "text": "Ответ"}]},
    ])

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        assert b"whisper-1" in request.content and b"verbose_json" in request.content
        return httpx.Response(200, json=next(results))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await recognize_tracks(
            AiaiSpeech(http, _settings()), tracks, staff_label="Сотрудник", bot_before_ms=3000,
        )
    assert result.splitlines() == [
        "[00:00] Ася / АТС: Привет", "[00:02] Клиент: Вопрос", "[00:05] Сотрудник: Ответ",
    ]


async def test_missing_timestamps_do_not_silently_break_dialogue() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "Есть речь, но нет реплик"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        speech = AiaiSpeech(http, _settings())
        with pytest.raises(AiaiError, match="таймкоды"):
            await speech.transcribe_recording(mono_wav(b"\x20\x00"), with_phrases=True)


@pytest.mark.parametrize(("recover", "first_status"), [(True, 200), (False, 200), (False, 402)])
async def test_prompt_generation_retries_only_invalid_audio(monkeypatch, recover, first_status):
    audio = mono_wav(b"\x20\x00" * 800)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 2 and recover:
            return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})
        return httpx.Response(first_status, json={"error": "no audio"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(voice_bot, "voice_speech", lambda *args: AiaiSpeech(http, _settings()))
        monkeypatch.setattr(voice_bot, "_PROMPTS", {"bot_question": "Здравствуйте"})
        if recover:
            assert await voice_bot._synthesize_prompts() == {"bot_question": audio}
        else:
            with pytest.raises(voice_bot.VoiceError, match="bot_question: AIAI.BY"):
                await voice_bot._synthesize_prompts()
    assert calls == ["/v1/audio/speech"] * (2 if first_status == 200 else 1)
