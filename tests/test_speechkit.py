import json
from base64 import b64decode, b64encode

import httpx
import pytest

from app.services.speechkit import SpeechKit, SpeechKitError, _transcript


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


def test_dialogue_orders_phrases_with_colliding_channel_indices() -> None:
    chunks = [
        {
            "channelTag": "1",
            "audioCursors": {"finalIndex": "0"},
            "final": {"alternatives": [{"startTimeMs": "3500", "text": "Проверю сроки."}]},
        },
        {
            "audioCursors": {"finalIndex": "0"},
            "final": {
                "channelTag": "0",
                "alternatives": [{"startTimeMs": "1200", "text": "когда"}],
            },
        },
        {
            "finalRefinement": {
                "finalIndex": "0",
                "normalizedText": {"channelTag": "0", "alternatives": [{"text": "Когда?"}]},
            }
        },
    ]
    response = "\n".join(json.dumps({"result": chunk}) for chunk in chunks)

    assert _transcript(response, dialogue=True) == (
        "[00:01] Клиент: Когда?\n[00:03] Сотрудник: Проверю сроки."
    )


def test_single_speaking_side_keeps_its_label_in_dialogue() -> None:
    response = json.dumps(
        {
            "result": {
                "channelTag": "0",
                "final": {"alternatives": [{"text": "Алло."}]},
            }
        }
    )

    assert _transcript(response, dialogue=True) == "Клиент: Алло."
    assert _transcript(response) == "Алло."


def test_dialogue_orders_by_first_word_and_preserves_text_on_empty_refinement() -> None:
    events = [
        {
            "channelTag": channel,
            "audioCursors": {"finalIndex": "0"},
            "final": {
                "alternatives": [
                    {"startTimeMs": "0", "words": [{"startTimeMs": time}], "text": phrase}
                ]
            },
        }
        for channel, time, phrase in (("0", "4500", "Спасибо."), ("1", "2400", "Отправил."))
    ]
    events.append(
        {
            "channelTag": "0",
            "finalRefinement": {
                "finalIndex": "0",
                "normalizedText": {"alternatives": [{"text": ""}]},
            },
        }
    )
    response = "\n".join(json.dumps({"result": event}) for event in events)

    assert _transcript(response, dialogue=True) == (
        "[00:02] Сотрудник: Отправил.\n[00:04] Клиент: Спасибо."
    )


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
