import json

import httpx
import pytest

from app.services.yandexgpt import YandexGpt, YandexGptError, find_deal_code


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("Проект 26_127, да, 26-127.", "26_127"),
        ("По сделке № 24 001 уточните сроки.", "24_001"),
        ("Проверьте 26_127 и 25_152.", None),
        ("Стоимость 26 127 рублей. Перезвоните на +375-26-127-12-34.", None),
    ],
)
def test_find_deal_code_uses_unambiguous_evidence(transcript: str, expected: str | None) -> None:
    assert find_deal_code(transcript) == expected


async def test_summarize_uses_validated_structure_without_inventing_deal_code() -> None:
    transcript = "Клиент просит перезвонить завтра."

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://ai.api.cloud.yandex.net/v1/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "gpt://test-folder/yandexgpt-5.1"
        assert body["messages"][1] == {"role": "user", "content": transcript}
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "topic": "Просьба перезвонить",
                                    "agreements": [],
                                    "next_steps": [],
                                }
                            )
                        },
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        summary = await YandexGpt(http, "test-folder").summarize(transcript)
    assert summary.model_dump() == {
        "topic": "Просьба перезвонить",
        "agreements": [],
        "next_steps": [],
        "deal_code": None,
    }


async def test_summarize_rejects_truncated_inference() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(YandexGptError):
            await YandexGpt(http, "test-folder").summarize("Тестовая фраза")
