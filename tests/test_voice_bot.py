import base64
import io
import json
import wave
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings, get_settings
from app.main import app
from app.services.voice_bot import spoken_reference

_PATH = "/internal/telephony/answer-audio"
_HEADERS = {"X-Telephony-Secret": "test-secret", "Content-Type": "audio/wav"}
_PCM = b"\x01\x00" * 8000
_QUESTION = "Где инструкция?"
_ANSWER = "Инструкция есть в Savt Assist."


def _wav(pcm: bytes, *, rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        wav.writeframes(pcm)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("text", "number", "kind"),
    [
        ("Объект 25 125.", "25_125", "project"),
        ("Номер шкафа 25 125 2", "25_125_2", "cabinet"),
        ("двадцать пять сто двадцать пять два", "25_125_2", None),
        ("Шкаф № 025 дефис 01 дробь 2", "025-01/2", "cabinet"),
        ("Нет, объект 24_069", "24_069", "project"),
        ("Где открыть документы по шкафу 25 125 2?", None, None),
        ("Нет номера", None, None),
    ],
)
def test_spoken_reference_preserves_groups_without_guessing_kind(text, number, kind):
    reference = spoken_reference(text)
    if number is None:
        assert reference is None
    else:
        assert reference is not None
        assert (reference.number, reference.kind) == (number, kind)


@pytest.mark.parametrize(
    ("pcm", "provider_reply", "expected_status", "expected_hosts"),
    [
        (
            _PCM,
            {"status": "answered", "answer": _ANSWER},
            200,
            ["stt.api.cloud.yandex.net", "helper.savt.by", "tts.api.cloud.yandex.net"],
        ),
        (
            _PCM,
            {"status": "needs_operator", "answer": None},
            204,
            ["stt.api.cloud.yandex.net", "helper.savt.by"],
        ),
        (b"\x00" * len(_PCM), None, 204, []),
        (_PCM, None, 502, ["stt.api.cloud.yandex.net", "helper.savt.by"]),
    ],
)
async def test_answer_audio(monkeypatch, pcm, provider_reply, expected_status, expected_hosts):
    real_client = httpx.AsyncClient
    visited_hosts = []

    def provider(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        visited_hosts.append(host)
        if host == "stt.api.cloud.yandex.net":
            assert request.url.params["sampleRateHertz"] == "8000"
            assert request.content == _PCM
            assert request.headers["authorization"] == "Api-Key speechkit-test-key"
            assert "x-telephony-secret" not in request.headers
            return httpx.Response(200, json={"result": _QUESTION})
        if host == "helper.savt.by":
            assert request.url.path == "/internal/telephony/answer-question"
            assert request.headers["x-telephony-secret"] == "test-secret"
            assert "authorization" not in request.headers
            assert json.loads(request.content) == {"question": _QUESTION}
            if provider_reply is None:
                return httpx.Response(503)
            return httpx.Response(200, json=provider_reply)
        assert host == "tts.api.cloud.yandex.net"
        assert request.headers["authorization"] == "Api-Key speechkit-test-key"
        assert parse_qs(request.content.decode())["text"] == [_ANSWER]
        return httpx.Response(200, content=_PCM)

    def mock_client(**kwargs):
        return real_client(transport=httpx.MockTransport(provider), **kwargs)

    monkeypatch.setattr("app.api.call_objects.httpx.AsyncClient", mock_client)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(
            savtassist_api_secret="test-secret", speechkit_api_key="speechkit-test-key"
        ),
    )

    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        assert (await api.post(_PATH, content=_wav(pcm))).status_code == 403
        wrong_rate = await api.post(_PATH, headers=_HEADERS, content=_wav(pcm, rate=16000))
        assert wrong_rate.status_code == 422
        oversized = await api.post(_PATH, headers=_HEADERS, content=bytes(256_001))
        assert oversized.status_code == 413
        assert not visited_hosts

        response = await api.post(_PATH, headers=_HEADERS, content=_wav(pcm))

    assert response.status_code == expected_status
    assert visited_hosts == expected_hosts
    if expected_status == 200:
        assert response.headers["content-type"] == "audio/wav"
        assert response.content == _wav(_PCM)
    if expected_status == 204:
        assert response.content == b""


async def test_voice_dialogue_carries_context_and_handles_spoken_choices(monkeypatch) -> None:
    real_client = httpx.AsyncClient
    recognized = iter((
        _QUESTION,
        "А где именно её открыть?",
        "Нет, соедините меня со специалистом, пожалуйста.",
        "Алло.",
        "Спасибо, вопрос решён.",
        "Где инструкция?",
    ))
    provider_questions = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stt.api.cloud.yandex.net":
            return httpx.Response(200, json={"result": next(recognized)})
        if request.url.host == "helper.savt.by":
            provider_questions.append(json.loads(request.content)["question"])
            return httpx.Response(200, json={"status": "answered", "answer": _ANSWER})
        assert request.url.host == "tts.api.cloud.yandex.net"
        return httpx.Response(200, content=_PCM)

    monkeypatch.setattr(
        "app.api.call_objects.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs),
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(savtassist_api_secret="test-secret", speechkit_api_key="test-key"),
    )
    path = "/internal/telephony/voice-turn"
    audio = base64.b64encode(_wav(_PCM)).decode()
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        first = (await api.post(path, headers=_HEADERS, json={"audio": audio})).json()
        assert first["status"] == "answered"
        assert first["exchange"] == {"question": _QUESTION, "answer": _ANSWER}
        assert base64.b64decode(first["audio"]) == _wav(_PCM)

        history = [first["exchange"]]
        payload = {"audio": audio, "history": history}
        followup = (await api.post(path, headers=_HEADERS, json=payload)).json()
        assert followup["status"] == "answered"
        assert followup["exchange"]["question"] == "А где именно её открыть?"
        for status in ("operator", "repeat", "done"):
            result = await api.post(path, headers=_HEADERS, json=payload)
            assert result.json() == {"status": status}

        separate = (await api.post(path, headers=_HEADERS, json={"audio": audio})).json()
        assert separate["status"] == "answered"
    assert provider_questions[0] == provider_questions[2] == _QUESTION
    assert len(provider_questions) == 3
    assert provider_questions[1].startswith("Текущий вопрос: А где именно её открыть?")
    assert _QUESTION in provider_questions[1] and _ANSWER in provider_questions[1]
