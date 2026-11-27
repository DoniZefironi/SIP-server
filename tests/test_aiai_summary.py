import json

import httpx

from app.config import Settings
from app.services.summarization import _summary_client


async def test_aiai_summary_uses_selected_model_and_preserves_deal_routing() -> None:
    transcript = "Клиент: Уточните срок по проекту 26_127. Сотрудник: Сообщу срок завтра."
    summary = {"topic": "Срок поставки", "agreements": [], "next_steps": ["Уточнить срок"]}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.aiai.by/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer aiai-test"
        body = json.loads(request.content)
        assert body["model"] == "gemini-2.5-flash-lite"
        assert body["messages"][1] == {"role": "user", "content": transcript}
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(summary)}}],
        })

    settings = Settings(
        _env_file=None,
        summary_provider="aiai",
        aiai_api_key="aiai-test",
        aiai_base_url="https://api.aiai.by/v1/",
        aiai_model="gemini-2.5-flash-lite",
        yandexgpt_enabled=True,
        yandexgpt_api_key="yandex-test",
        yandexgpt_folder_id="test-folder",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = _summary_client(settings, http)
        assert client is not None
        result = await client.summarize(transcript)

    assert result.model_dump() == {**summary, "deal_code": "26_127"}


async def test_aiai_summary_rejects_insecure_address() -> None:
    settings = Settings(
        _env_file=None,
        summary_provider="aiai",
        aiai_api_key="aiai-test",
        aiai_base_url="http://api.aiai.by/v1",
    )
    async with httpx.AsyncClient() as http:
        assert _summary_client(settings, http) is None
        assert "Authorization" not in http.headers
