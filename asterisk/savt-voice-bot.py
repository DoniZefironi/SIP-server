#!/usr/bin/env python3
"""Голосовой диалог SAVT на время звонка."""

import base64
import binascii
import json
import re
import signal
import sys
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from select import select
from tempfile import TemporaryDirectory

SECRET_FILE = Path("/etc/asterisk/savt-voice-bot.secret")
MAX_QUESTION_BYTES = 256_000
MAX_ANSWER_BYTES = 2_000_044
MAX_RESPONSE_BYTES = 2_700_000
WAIT_NOTICE_SECONDS = 1.1
REFERENCE_PROMPTS = {
    "reference_found": "bot_reference_found",
    "reference_unavailable": "bot_reference_unavailable",
    "ask_question": "bot_ask_question",
}


def agi(command: str) -> int:
    print(command, flush=True)
    response = sys.stdin.readline()
    result = re.match(r"200 result=(-?\d+)(?:\s|$)", response)
    if result is None:
        raise RuntimeError("Asterisk не подтвердил команду AGI")
    value = int(result[1])
    # RECORD FILE может вернуть оставшееся время при остановке по тишине.
    return 0 if value >= 0 and "(timeout)" in response else value


def _choice(result: int, answered: bool) -> str | None:
    if result in (ord("0"), ord("2")):
        return "operator"
    if answered and result == ord("1"):
        return "answered"
    if result < 0:
        return "hangup"
    return None


def _play(path: Path, digits: str) -> int:
    return agi(f'STREAM FILE "{path}" "{digits}"')


def fetch_turn(secret: str, question: bytes, context: dict, wait_file: Path) -> dict:
    payload = json.dumps({
        "audio": base64.b64encode(question).decode("ascii"),
        **context,
    }, ensure_ascii=False).encode()
    with closing(HTTPConnection("127.0.0.1", 8000, timeout=17)) as connection:
        connection.request(
            "POST",
            "/internal/telephony/voice-turn",
            body=payload,
            headers={"Content-Type": "application/json", "X-Telephony-Secret": secret},
        )
        if connection.sock is not None:
            readable, _, _ = select([connection.sock], [], [], WAIT_NOTICE_SECONDS)
            if not readable:
                history = context["history"]
                choice = _choice(_play(wait_file, "012" if history else "02"), bool(history))
                if choice in ("operator", "answered"):
                    return {"status": "operator" if choice == "operator" else "done"}
        with connection.getresponse() as response:
            if response.status != 200:
                raise RuntimeError(f"API голосового бота вернул HTTP {response.status}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Ответ голосового бота слишком длинный")
    result = json.loads(body)
    if not isinstance(result, dict) or result.get("status") not in (
        "answered", "operator", "done", "repeat", "confirm_reference", *REFERENCE_PROMPTS
    ):
        raise RuntimeError("API голосового бота вернул неизвестный результат")
    return result


def dialogue(secret: str, phrases: Path, suffix: str, linkedid: str | None) -> str:
    history = []
    retries = 0
    stage = "reference"
    reference = None
    prompt = phrases / f"bot_question{suffix}"
    with TemporaryDirectory(prefix="savt-voice-") as directory:
        question_path = Path(directory, "question")
        answer_path = Path(directory, "answer")
        while True:
            digits = "012" if history else "02"
            if prompt is not None:
                choice = _choice(_play(prompt, digits), bool(history))
                if choice is not None:
                    return choice
            question_file = question_path.with_suffix(".wav")
            question_file.unlink(missing_ok=True)
            result = agi(f'RECORD FILE "{question_path}" wav "{digits}#" 12000 0 BEEP s=1')
            choice = _choice(result, bool(history))
            if choice is not None:
                return choice
            with question_file.open("rb") as question:
                audio = question.read(MAX_QUESTION_BYTES + 1)
            if 44 < len(audio) <= MAX_QUESTION_BYTES:
                turn = fetch_turn(
                    secret, audio,
                    {
                        "history": history, "stage": stage,
                        "reference": reference, "linkedid": linkedid,
                    },
                    phrases / "bot_wait",
                )
            else:
                turn = {"status": "repeat"}
            match turn["status"]:
                case "operator":
                    return "operator"
                case "done":
                    return "answered" if history else "ended"
                case "repeat":
                    if retries:
                        if stage != "question":
                            stage, reference = "question", None
                            retries = 0
                            prompt = phrases / "bot_ask_question"
                            continue
                        return "operator"
                    retries += 1
                    prompt = phrases / "bot_retry"
                    continue
            retries = 0
            if turn["status"] in REFERENCE_PROMPTS:
                stage, reference = "question", None
                prompt = phrases / REFERENCE_PROMPTS[turn["status"]]
                continue
            answer = base64.b64decode(turn["audio"], validate=True)
            if len(answer) > MAX_ANSWER_BYTES or answer[:4] != b"RIFF" or answer[8:12] != b"WAVE":
                raise RuntimeError("API голосового бота вернул некорректный WAV")
            answer_path.with_suffix(".wav").write_bytes(answer)
            if turn["status"] == "confirm_reference":
                reference = turn["reference"]
                if not isinstance(reference, dict):
                    raise RuntimeError("API голосового бота вернул неверный номер")
                stage, prompt = "confirm_reference", answer_path
                continue
            exchange = turn["exchange"]
            if not isinstance(exchange, dict):
                raise RuntimeError("API голосового бота вернул неверный контекст")
            choice = _choice(_play(answer_path, "012"), True)
            if choice is not None:
                return choice
            prompt = None if history else phrases / f"bot_feedback{suffix}"
            history = [*history[-1:], exchange]
            stage, reference = "question", None


def run() -> None:
    for line in sys.stdin:  # Заголовки окружения AGI заканчиваются пустой строкой.
        if not line.strip():
            break
    else:
        return

    secret = SECRET_FILE.read_text(encoding="ascii").strip()
    if not secret:
        return
    phrases = Path(sys.argv[1]) if len(sys.argv) >= 2 else Path("/var/lib/asterisk/phrases")
    suffix = "_afterhours" if len(sys.argv) >= 3 and sys.argv[2] == "_afterhours" else ""
    linkedid = sys.argv[3] if len(sys.argv) >= 4 and sys.argv[3] else None
    result = dialogue(secret, phrases, suffix, linkedid)
    if result in ("answered", "ended"):
        agi(f"SET VARIABLE SAVT_BOT_RESULT {result}")


def on_hangup(_signal, _frame) -> None:
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGHUP, on_hangup)
    try:
        run()
    except (
        OSError, HTTPException, ValueError, KeyError, TypeError, binascii.Error, RuntimeError
    ) as error:
        # Секрет, звук звонящего и ответ бота не выводим из процесса AGI.
        print(f"SAVT voice bot: {type(error).__name__}", file=sys.stderr)
