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


def _transcript(
    text: str, *, dialogue: bool = False, staff_label: str = "Сотрудник",
    bot_before_ms: int | None = None,
) -> str:
    """Собрать финальные фразы из потока JSON, выбирая нормализованный вариант."""
    if not text.strip():
        raise SpeechKitError("SpeechKit вернул пустой ответ при получении текста", retryable=True)
    decoder = json.JSONDecoder()
    phrases: dict[tuple[str, int], tuple[int | None, str]] = {}
    refined: dict[tuple[str, int], tuple[int | None, str]] = {}
    cursor = 0
    try:
        while (cursor := _WHITESPACE.match(text, cursor).end()) < len(text):
            chunk, cursor = decoder.raw_decode(text, cursor)
            if chunk.get("error") is not None:
                raise SpeechKitError("SpeechKit вернул ошибку в потоке распознавания")
            result = chunk["result"]
            if "final" in result:
                index = int(result.get("audioCursors", {}).get("finalIndex", len(phrases)))
                _store_phrase(phrases, result, result["final"], index, dialogue)
            if "finalRefinement" in result:
                refinement = result["finalRefinement"]
                if normalized := refinement.get("normalizedText"):
                    _store_phrase(
                        refined, result, normalized, int(refinement["finalIndex"]), dialogue
                    )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SpeechKitError("SpeechKit вернул неожиданный формат расшифровки") from exc

    for key, (time_ms, phrase) in refined.items():
        original_time, _ = phrases.get(key, (None, ""))
        phrases[key] = (original_time if original_time is not None else time_ms, phrase)
    if not dialogue:
        return " ".join(phrases[key][1] for key in sorted(phrases) if phrases[key][1])

    def chronological(key: tuple[str, int]) -> tuple[bool, int, int, str]:
        time_ms, _ = phrases[key]
        return time_ms is None, time_ms or 0, key[1], key[0]

    speakers = {"0": "Клиент", "1": staff_label}
    lines = []
    for key in sorted(phrases, key=chronological):
        time_ms, phrase = phrases[key]
        if not phrase:
            continue
        speaker = speakers.get(key[0], "Собеседник")
        if key[0] == "1" and bot_before_ms is not None:
            if time_ms is None:
                speaker = "Собеседник"
            elif time_ms < bot_before_ms:
                speaker = "Ася / АТС"
        prefix = ""
        if time_ms is not None:
            minutes, seconds = divmod(time_ms // 1000, 60)
            prefix = f"[{minutes:02d}:{seconds:02d}] "
        lines.append(f"{prefix}{speaker}: {phrase}")
    return "\n".join(lines)


def _store_phrase(phrases: dict, result: dict, update: dict, index: int, dialogue: bool) -> None:
    alternatives = update.get("alternatives")
    if not alternatives:
        return
    alternative = alternatives[0]
    phrase = alternative["text"].strip()
    if not phrase:
        return
    # Граница фразы может включать паузу перед речью. Сортируем по первому слову.
    time_ms = next(
        (word["startTimeMs"] for word in alternative.get("words", []) if "startTimeMs" in word),
        alternative.get("startTimeMs"),
    )
    channel = update.get("channelTag", result.get("channelTag", "0")) if dialogue else ""
    phrases[str(channel), index] = (
        int(time_ms) if time_ms is not None else None,
        phrase,
    )


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

    async def poll(
        self, operation_id: str, *, dialogue: bool = False, staff_label: str = "Сотрудник",
        bot_before_ms: int | None = None,
    ) -> str | None:
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
        return _transcript(
            response.text, dialogue=dialogue, staff_label=staff_label, bot_before_ms=bot_before_ms,
        )
