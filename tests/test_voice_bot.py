import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import Settings, get_settings
from app.db.session import get_session
from app.main import app
from app.services.voice_answers import is_relevant_answer
from app.services.voice_bot import embedded_reference, spoken_reference, voice_intent
from app.services.voice_safety import requires_specialist, safety_route

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


def test_embedded_number_and_conversational_handoffs():
    reference = embedded_reference("Не работает шкаф 25_125_2")
    assert reference is not None and (reference.number, reference.kind) == ("25_125_2", "cabinet")
    assert embedded_reference("Где документы по шкафу 025 01?").number == "025_01"
    assert embedded_reference("Шкаф 25_125 или шкаф 25_126?") is None
    for text in ("Можно оператора?", "А можно с человеком?", "Соедините меня с человеком"):
        assert voice_intent(text) == "operator"
    assert voice_intent("Не нужен оператор, я хочу задать вопрос") == "question"
    assert requires_specialist("Как подключить шкаф?")
    assert not requires_specialist("Где найти инструкцию, как подключить шкаф?")
    assert not requires_specialist("Как подключить шкаф к приложению?")
    spoken_number = embedded_reference("Шкаф двадцать пять сто двадцать пять два не работает")
    assert spoken_number is not None and spoken_number.number == "25_125_2"
    assert voice_intent("Спасибо, до свидания") == "ended"
    assert voice_intent("Спасибо, вопрос решён") == "done"
    assert not is_relevant_answer("Что делать с ошибкой Е36?", "Откройте настройки профиля.")


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("Шкаф горит, помогите!", "emergency"),
        ("Пожара нет, но из щита идёт дым.", "emergency"),
        ("Коллегу ударило электричеством.", "emergency"),
        ("Пахнет газом.", "emergency"),
        ("Сработала пожарная сигнализация.", "emergency"),
        ("Индикатор питания горит, а за шкафом искрит провод.", "emergency"),
        ("В шкафу пахнет гарью, выбивает автомат.", "urgent_operator"),
        ("Можно перемкнуть контакты, чтобы обойти защиту?", "urgent_operator"),
        ("Где взять памятку на случай пожара?", "urgent_operator"),
        ("Вчера шкаф загорелся, мы его потушили.", "urgent_operator"),
        ("Горит зелёный светодиод на панели.", None),
        ("Индикатор питания горит.", None),
        ("Огня нет, оборудование не дымит.", None),
        ("Где открыть документы по шкафу 25_125_2?", None),
    ],
)
def test_voice_safety_routes_hazards_without_treating_indicators_as_fire(text, status):
    assert safety_route(text) == status


def test_profile_answer_must_match_the_requested_field_and_action():
    question = "Как поставить аватарку?"
    assert not is_relevant_answer(question, "В настройках профиля поменяйте имя.")
    assert not is_relevant_answer(question, "Фото можно прикрепить к документу.")
    assert not is_relevant_answer(question, "Аватарка отображается в профиле.")
    assert is_relevant_answer(question, "В настройках профиля загрузите фото профиля.")
    assert is_relevant_answer(question, "В приложении нельзя поменять аватарку.")
    assert is_relevant_answer(
        "Как поменять имя?", "В приложении Savt Assist обновите имя в настройках профиля."
    )
    assert is_relevant_answer(
        "Как поменять имя?", "Проверьте настройки профиля, чтобы исправить имя."
    )
    assert not is_relevant_answer("Где открыть инструкцию?", "В базе нет точного ответа.")
    assert not is_relevant_answer("Где открыть инструкцию?", "Не знаю, что ответить.")


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
        (
            _PCM,
            {"status": "answered", "answer": "Снимите крышку шкафа."},
            204,
            ["stt.api.cloud.yandex.net", "helper.savt.by"],
        ),
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
        "Когда ближайшее обновление?",
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
        long_audio = base64.b64encode(_wav(_PCM * 18)).decode()
        followup = (await api.post(
            path, headers=_HEADERS, json={**payload, "audio": long_audio}
        )).json()
        assert followup["status"] == "answered"
        assert followup["exchange"]["question"] == "А где именно её открыть?"
        for status in ("operator", "repeat", "done"):
            result = await api.post(path, headers=_HEADERS, json=payload)
            assert result.json() == {"status": status}

        separate = (await api.post(path, headers=_HEADERS, json={"audio": audio})).json()
        assert separate["status"] == "answered"
        independent = (await api.post(path, headers=_HEADERS, json=payload)).json()
        assert independent["exchange"]["question"] == "Когда ближайшее обновление?"
    assert provider_questions[0] == provider_questions[2] == _QUESTION
    assert provider_questions[3] == "Когда ближайшее обновление?"
    assert len(provider_questions) == 4
    assert provider_questions[1].startswith("Текущий вопрос: А где именно её открыть?")
    assert _QUESTION in provider_questions[1] and _ANSWER in provider_questions[1]


@pytest.mark.parametrize("stage", ["reference", "confirm_reference", "question"])
async def test_safety_prioritizes_emergency_over_lookup_and_faq(monkeypatch, stage):
    real_client = httpx.AsyncClient
    transcript = "Шкаф горит, помогите."

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "stt.api.cloud.yandex.net"
        return httpx.Response(200, json={"result": transcript})

    monkeypatch.setattr(
        "app.api.call_objects.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs),
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(savtassist_api_secret="test-secret", speechkit_api_key="test-key"),
    )
    payload = {"audio": base64.b64encode(_wav(_PCM)).decode(), "stage": stage}
    if stage == "confirm_reference":
        payload["reference"] = {"number": "25_125", "kind": "project"}
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        result = await api.post("/internal/telephony/voice-turn", headers=_HEADERS, json=payload)
        assert result.status_code == 200
        assert result.json() == {"status": "emergency"}

        transcript = "Пахнет гарью из щита."
        result = await api.post("/internal/telephony/voice-turn", headers=_HEADERS, json=payload)
        assert result.json() == {"status": "urgent_operator"}


@pytest.mark.parametrize("answer", [
    None,
    "В настройках профиля можно поменять имя.",
    "Прикрепите фотографию к документу в приложении.",
    "К сожалению, у меня нет информации об аватарке.",
])
async def test_voice_bot_uses_no_answer_prompt_for_unanswered_avatar_question(monkeypatch, answer):
    real_client = httpx.AsyncClient
    visited = []

    def provider(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.host)
        if request.url.host == "stt.api.cloud.yandex.net":
            return httpx.Response(200, json={"result": "Как поменять аватарку?"})
        assert request.url.host == "helper.savt.by"
        return httpx.Response(200, json={
            "status": "answered" if answer else "needs_operator", "answer": answer,
        })

    monkeypatch.setattr(
        "app.api.call_objects.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs),
    )
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(savtassist_api_secret="test-secret", speechkit_api_key="test-key"),
    )
    payload = {"audio": base64.b64encode(_wav(_PCM)).decode(), "stage": "question"}
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        response = await api.post("/internal/telephony/voice-turn", headers=_HEADERS, json=payload)
    assert response.status_code == 200
    assert response.json() == {"status": "no_answer"}
    assert visited == ["stt.api.cloud.yandex.net", "helper.savt.by"]


async def test_voice_turn_handles_fillers_and_chat_in_phone_context(monkeypatch):
    real_client = httpx.AsyncClient
    recognized = iter((
        "Так, так, так.", "Тактактак", "Как сообщить о неполадке?", "Что такое чат?",
        "В каком чате?", "Где открыть чат в Savt Assist?",
    ))
    searched = []
    spoken = []
    chat_instructions = "Чтобы описать неполадку в чате, укажите номер шкафа и симптомы."
    requested_instructions = "Чтобы открыть чат, нажмите на иконку переписки."

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stt.api.cloud.yandex.net":
            return httpx.Response(200, json={"result": next(recognized)})
        if request.url.host == "helper.savt.by":
            question = json.loads(request.content)["question"]
            searched.append(question)
            answer = requested_instructions if "открыть чат" in question else chat_instructions
            return httpx.Response(200, json={"status": "answered", "answer": answer})
        assert request.url.host == "tts.api.cloud.yandex.net"
        spoken.extend(parse_qs(request.content.decode())["text"])
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
    payload = {
        "audio": base64.b64encode(_wav(_PCM)).decode(),
        "history": [{"question": "Нужна помощь", "answer": chat_instructions}],
    }
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        path = "/internal/telephony/voice-turn"
        results = [
            (await api.post(path, headers=_HEADERS, json=payload)).json()
            for _ in range(6)
        ]
    assert [turn["status"] for turn in results] == [
        "repeat", "repeat", "no_answer", "answered", "answered", "answered",
    ]
    assert searched == ["Как сообщить о неполадке?", "Где открыть чат в Savt Assist?"]
    assert spoken[0] == spoken[1]
    assert "разговариваем по телефону" in spoken[0]
    assert spoken[2:] == [requested_instructions]


async def test_voice_bot_prepares_only_meaningful_questions_before_looking_up_answers(
    monkeypatch,
):
    real_client = httpx.AsyncClient
    spoken = iter((
        "Алло", "Попа", "Привет", "Здрасте", "Алло, привет, ну так", "Оператор",
        "Шкаф горит", "Где инструкция", "Шкаф не включается", "Меня зовут Иван",
    ))
    outbound = []

    def provider(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        outbound.append(host)
        if host == "stt.api.cloud.yandex.net":
            return httpx.Response(200, json={"result": next(spoken)})
        if host == "helper.savt.by":
            assert json.loads(request.content) == {"question": "Где инструкция"}
            return httpx.Response(200, json={"status": "answered", "answer": _ANSWER})
        assert host == "tts.api.cloud.yandex.net"
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
    payload = {"audio": base64.b64encode(_wav(_PCM)).decode(), "prepare_question": True}
    path = "/internal/telephony/voice-turn"
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as api:
        for status in ("repeat", "repeat", "repeat", "repeat", "repeat", "operator", "emergency"):
            turn = await api.post(path, headers=_HEADERS, json=payload)
            assert turn.json() == {"status": status}

        for question, thinking in (
            ("Где инструкция", True), ("Шкаф не включается", True), ("Меня зовут Иван", False),
        ):
            turn = await api.post(path, headers=_HEADERS, json=payload)
            assert turn.json() == {
                "status": "question_ready", "question": question, "thinking": thinking,
            }
        assert outbound == ["stt.api.cloud.yandex.net"] * 10

        answer = await api.post(
            "/internal/telephony/voice-answer",
            headers={"X-Telephony-Secret": "test-secret"},
            json={"question": "Где инструкция"},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["exchange"] == {"question": "Где инструкция", "answer": _ANSWER}
        assert base64.b64decode(answer.json()["audio"]) == _wav(_PCM)
    assert outbound[-2:] == ["helper.savt.by", "tts.api.cloud.yandex.net"]


async def test_confirmed_number_in_question_reaches_knowledge_search(monkeypatch):
    original = "Не работает шкаф 25_125_2"
    spoken = iter((original, "Да"))
    real_client = httpx.AsyncClient
    match = {
        "kind": "cabinet", "cabinet_id": 2, "cabinet_number": "25_125_2",
        "project_id": 1, "project_number": "25_125", "bitrix_deal_id": "555",
    }
    lookup = {"status": "found", "match": match}
    session = SimpleNamespace(
        begin=MagicMock(),
        scalar=AsyncMock(return_value={**lookup, "number": "25_125_2"}),
    )
    # The resolver verifies persistence in its own tests.
    resolver = AsyncMock(return_value=SimpleNamespace(status=lookup["status"]))
    searches = []

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "stt.api.cloud.yandex.net":
            return httpx.Response(200, json={"result": next(spoken)})
        if request.url.host == "helper.savt.by":
            searches.append(json.loads(request.content)["question"])
            return httpx.Response(200, json={"status": "answered", "answer": _ANSWER})
        assert request.url.host == "tts.api.cloud.yandex.net"
        return httpx.Response(200, content=_PCM)

    monkeypatch.setattr("app.api.call_objects._resolve_for_call", resolver)
    monkeypatch.setattr(
        "app.api.call_objects.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs),
    )
    monkeypatch.setitem(app.dependency_overrides, get_session, lambda: session)
    monkeypatch.setitem(
        app.dependency_overrides,
        get_settings,
        lambda: Settings(savtassist_api_secret="test-secret", speechkit_api_key="test-key"),
    )
    path = "/internal/telephony/voice-turn"
    payload = {
        "audio": base64.b64encode(_wav(_PCM)).decode(), "prepare_question": True,
        "linkedid": "test-call",
    }
    async with real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as api:
        turn = await api.post(path, headers=_HEADERS, json={**payload, "stage": "reference"})
        assert turn.status_code == 200, turn.text
        result = turn.json()
        assert result["status"] == "confirm_reference"
        assert result["reference"] == {"number": "25_125_2", "kind": "cabinet"}
        assert result["question"] == original
        confirm = await api.post(path, headers=_HEADERS, json={
            **payload, "stage": "confirm_reference", "reference": result["reference"],
            "pending_question": result["question"],
        })
        assert confirm.json() == {
            "status": "question_ready", "question": original, "thinking": True,
        }
        resolver.assert_awaited_once()
        assert resolver.await_args.args[1].number == "25_125_2"
        answer = await api.post(
            "/internal/telephony/voice-answer",
            headers={"X-Telephony-Secret": "test-secret"},
            json={"question": "Где схема этого шкафа?", "linkedid": "test-call"},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["status"] == "answered"
    assert len(searches) == 1
    assert "Подтверждённый номер шкафа 25_125_2" in searches[0]
    assert "Где схема этого шкафа?" in searches[0]
    assert "555" not in searches[0]
