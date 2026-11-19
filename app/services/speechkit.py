import json
import re
from urllib.parse import quote

import httpx

_STT = "https://stt.api.cloud.yandex.net/stt/v3"
_OPERATIONS = "https://operation.api.cloud.yandex.net/operations"
_WHITESPACE = re.compile(r"\s*")


class SpeechKitError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _transcript(text: str) -> str:
    """Собрать финальные фразы из потока JSON, выбирая нормализованный вариант."""
    if not text.strip():
        raise SpeechKitError("SpeechKit вернул пустой ответ при получении текста", retryable=True)
    decoder = json.JSONDecoder()
    phrases: dict[int, str] = {}
    refined: dict[int, str] = {}
    cursor = 0
    try:
        while (cursor := _WHITESPACE.match(text, cursor).end()) < len(text):
            chunk, cursor = decoder.raw_decode(text, cursor)
            if chunk.get("error") is not None:
                raise SpeechKitError("SpeechKit вернул ошибку в потоке распознавания")
            result = chunk["result"]
            if "final" in result:
                index = int(result.get("audioCursors", {}).get("finalIndex", len(phrases)))
                alternatives = result["final"].get("alternatives", [])
                if alternatives:
                    phrases[index] = alternatives[0]["text"].strip()
            if "finalRefinement" in result:
                refinement = result["finalRefinement"]
                alternatives = refinement.get("normalizedText", {}).get("alternatives", [])
                if alternatives:
                    refined[int(refinement["finalIndex"])] = alternatives[0]["text"].strip()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SpeechKitError("SpeechKit вернул неожиданный формат расшифровки") from exc

    phrases.update(refined)
    return " ".join(phrases[index] for index in sorted(phrases) if phrases[index])


class SpeechKit:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        submitting = method == "POST"
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SpeechKitError("Не удалось подключиться к SpeechKit", retryable=True) from exc
        except httpx.RequestError as exc:
            message = (
                "Результат отправки в SpeechKit неизвестен" if submitting else "Сбой SpeechKit"
            )
            raise SpeechKitError(message, retryable=not submitting) from exc

        status = response.status_code
        if not response.is_success:
            if submitting and (status == 408 or status >= 500):
                raise SpeechKitError(f"Результат отправки в SpeechKit неизвестен (HTTP {status})")
            retryable = status == 429 or (not submitting and (status == 408 or status >= 500))
            raise SpeechKitError(f"SpeechKit вернул HTTP {status}", retryable=retryable)
        return response

    async def submit(self, content: str) -> str:
        response = await self._request(
            "POST",
            f"{_STT}/recognizeFileAsync",
            json={
                "content": content,
                "recognitionModel": {
                    "model": "general",
                    "audioFormat": {"containerAudio": {"containerAudioType": "WAV"}},
                    "textNormalization": {"textNormalization": "TEXT_NORMALIZATION_ENABLED"},
                },
            },
        )
        try:
            operation_id = response.json()["id"]
            if not isinstance(operation_id, str) or not 0 < len(operation_id) <= 255:
                raise ValueError("invalid operation ID")
        except (KeyError, TypeError, ValueError) as exc:
            raise SpeechKitError("SpeechKit не вернул идентификатор отправленной операции") from exc
        return operation_id

    async def poll(self, operation_id: str) -> str | None:
        response = await self._request("GET", f"{_OPERATIONS}/{quote(operation_id, safe='')}")
        try:
            operation = response.json()
            if not isinstance(operation, dict):
                raise ValueError("invalid operation")
        except ValueError as exc:
            raise SpeechKitError("Неверный ответ при проверке операции", retryable=True) from exc
        if operation.get("error") is not None:
            raise SpeechKitError("Операция распознавания завершилась ошибкой в SpeechKit")
        # В ответе с незавершённой операцией поле done может быть опущено.
        if not operation.get("done"):
            return None

        response = await self._request(
            "GET", f"{_STT}/getRecognition", params={"operation_id": operation_id}
        )
        return _transcript(response.text)
