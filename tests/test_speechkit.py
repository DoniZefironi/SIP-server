import json
from base64 import b64decode, b64encode

import httpx
import pytest

from app.services.speechkit import SpeechKit, SpeechKitError


async def test_submit_wav_asynchronously() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://stt.api.cloud.yandex.net/stt/v3/recognizeFileAsync"
        data = json.loads(request.content)
        assert b64decode(data["content"]) == b"synthetic WAV"
        assert data["recognitionModel"]["audioFormat"] == {
            "containerAudio": {"containerAudioType": "WAV"}
        }
        return httpx.Response(200, json={"id": "fake-operation", "done": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpeechKit(http)
        assert await client.submit(b64encode(b"synthetic WAV").decode("ascii")) == "fake-operation"


async def test_poll_uses_one_operation_and_merges_final_phrases() -> None:
    responses = iter([False, True])
    chunks = [
        {"partial": {"alternatives": [{"text": "черновик"}]}},
        {"audioCursors": {"finalIndex": "1"}, "final": {"alternatives": [{"text": "Сроки"}]}},
        {"audioCursors": {"finalIndex": "0"}, "final": {"alternatives": [{"text": "заявка два"}]}},
        {
            "finalRefinement": {
                "finalIndex": "0",
                "normalizedText": {"alternatives": [{"text": "Заявка 2."}]},
            }
        },
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        if request.url.host == "operation.api.cloud.yandex.net":
            assert request.url.path == "/operations/fake-operation"
            return httpx.Response(200, json={"done": next(responses)})
        assert request.url.path == "/stt/v3/getRecognition"
        assert request.url.params["operation_id"] == "fake-operation"
        return httpx.Response(
            200, text="\n".join(json.dumps({"result": chunk}, indent=2) for chunk in chunks)
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = SpeechKit(http)
        assert await client.poll("fake-operation") is None
        assert await client.poll("fake-operation") == "Заявка 2. Сроки"


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, False), (403, False)])
async def test_submission_is_retried_only_after_explicit_throttling(
    status: int, retryable: bool
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(SpeechKitError) as error:
            await SpeechKit(http).submit("some-content")
    assert error.value.retryable is retryable
