import httpx

from app.services.summary_model import (
    CallSummary,
    ChatSummary,
    SummaryError,
    SummaryText,
    find_deal_code,
)

__all__ = ["CallSummary", "SummaryText", "YandexGpt", "YandexGptError", "find_deal_code"]

YandexGptError = SummaryError


class YandexGpt(ChatSummary):
    def __init__(self, client: httpx.AsyncClient, folder_id: str) -> None:
        super().__init__(
            client,
            endpoint="https://ai.api.cloud.yandex.net/v1/chat/completions",
            model=f"gpt://{folder_id}/yandexgpt-5.1",
            provider="YandexGPT",
        )
