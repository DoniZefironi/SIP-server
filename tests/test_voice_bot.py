import io
import json
import wave
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings, get_settings
from app.main import app

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
