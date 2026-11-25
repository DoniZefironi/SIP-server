#!/usr/bin/env python3
"""AGI для одного голосового вопроса в тестовом сценарии SAVT."""

import re
import signal
import sys
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from tempfile import TemporaryDirectory

SECRET_FILE = Path("/etc/asterisk/savt-voice-bot.secret")
MAX_QUESTION_BYTES = 256_000
MAX_ANSWER_BYTES = 2_000_044


def agi(command: str) -> int:
    print(command, flush=True)
    response = sys.stdin.readline()
    result = re.match(r"200 result=(-?\d+)(?:\s|$)", response)
    if result is None:
        raise RuntimeError("Asterisk не подтвердил команду AGI")
    return int(result[1])


def fetch_answer(secret: str, question: bytes) -> bytes:
    connection = HTTPConnection("127.0.0.1", 8000, timeout=17)
    try:
        connection.request(
            "POST",
            "/internal/telephony/answer-audio",
            body=question,
            headers={"Content-Type": "audio/wav", "X-Telephony-Secret": secret},
        )
        with connection.getresponse() as response:
            if response.status == 204:
                return b""
            if response.status != 200 or response.getheader("Content-Type") != "audio/wav":
                raise RuntimeError(f"API голосового бота вернул HTTP {response.status}")
            answer = response.read(MAX_ANSWER_BYTES + 1)
            if len(answer) > MAX_ANSWER_BYTES or answer[:4] != b"RIFF" or answer[8:12] != b"WAVE":
                raise RuntimeError("API голосового бота вернул некорректный WAV")
            return answer
    finally:
        connection.close()


def run() -> None:
    for line in sys.stdin:  # Заголовки окружения AGI заканчиваются пустой строкой.
        if not line.strip():
            break
    else:
        return

    secret = SECRET_FILE.read_text(encoding="ascii").strip()
    if not secret:
        return
    with TemporaryDirectory(prefix="savt-voice-") as directory:
        question_path = Path(directory, "question")
        result = agi(f'RECORD FILE "{question_path}" wav "0#" 12000 0 BEEP s=2')
        if result < 0 or result == ord("0"):
            return
        with question_path.with_suffix(".wav").open("rb") as question:
            audio = question.read(MAX_QUESTION_BYTES + 1)
        if not 44 < len(audio) <= MAX_QUESTION_BYTES:
            return
        answer = fetch_answer(secret, audio)
        if answer:
            answer_path = Path(directory, "answer")
            answer_path.with_suffix(".wav").write_bytes(answer)
            if agi(f'STREAM FILE "{answer_path}" ""') == 0:
                agi('SET VARIABLE SAVT_BOT_RESULT answered')


def on_hangup(_signal, _frame) -> None:
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGHUP, on_hangup)
    try:
        run()
    except (OSError, HTTPException, UnicodeError, RuntimeError) as error:
        # Секрет, звук звонящего и ответ бота не выводим из процесса AGI.
        print(f"SAVT voice bot: {type(error).__name__}", file=sys.stderr)
