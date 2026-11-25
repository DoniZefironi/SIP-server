#!/usr/bin/env python3
"""Голосовой диалог SAVT на время звонка."""

import base64
import binascii
import io
import json
import os
import re
import signal
import sys
import wave
from array import array
from collections import deque
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from select import select
from tempfile import TemporaryDirectory
from time import monotonic

SECRET_FILE = Path("/etc/asterisk/savt-voice-bot.secret")
AUDIO_FD = 3  # EAGI передаёт сюда входящий PCM канала.
SAMPLE_RATE = 8000
FRAME_BYTES = 320  # 20 мс моно, 16 бит.
SPEECH_LEVEL = 192
START_SILENCE_SECONDS = 5
END_SILENCE_SECONDS = 2
MAX_SPEECH_BYTES = 29 * SAMPLE_RATE * 2
MAX_ANSWER_BYTES = 2_000_044
MAX_RESPONSE_BYTES = 2_700_000
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
    return int(result[1])


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


class Utterance:
    def __init__(self) -> None:
        self.pre_roll: deque[bytes] = deque(maxlen=12)
        self.pcm = bytearray()
        self.voiced_frames = 0
        self.last_speech: float | None = None
        self.last_speech_end = 0
        self.too_long = False

    def add(self, frame: bytes, now: float) -> None:
        samples = array("h", frame)
        voiced = sum(map(abs, samples)) >= SPEECH_LEVEL * len(samples)
        if self.last_speech is None:
            self.pre_roll.append(frame)
            self.voiced_frames = self.voiced_frames + 1 if voiced else 0
            if self.voiced_frames < 3:
                return
            self.pcm.extend(b"".join(self.pre_roll))
        elif len(self.pcm) < MAX_SPEECH_BYTES:
            self.pcm.extend(frame)
        elif voiced:
            self.too_long = True

        if voiced:
            self.last_speech = now
            self.last_speech_end = len(self.pcm)

    def audio(self) -> bytes:
        if self.last_speech is None or self.too_long:
            return b""
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            # Оставляем 200 мс после последнего слышимого звука.
            pcm = self.pcm[:self.last_speech_end + 10 * FRAME_BYTES]
            if sys.byteorder == "big":
                samples = array("h", pcm)
                samples.byteswap()
                pcm = samples.tobytes()
            wav.writeframes(pcm)
        return output.getvalue()


def _read_pcm() -> bytes:
    data = bytearray()
    while True:
        try:
            block = os.read(AUDIO_FD, 8192)
        except BlockingIOError:
            break
        if not block:
            break
        data.extend(block)
    return bytes(data)


def listen(digits: str, answered: bool) -> tuple[str | None, bytes]:
    _read_pcm()
    choice = _choice(_play(Path("beep"), digits), answered)
    if choice is not None:
        return choice, b""
    _read_pcm()

    utterance = Utterance()
    buffer = bytearray()
    start_deadline = monotonic() + START_SILENCE_SECONDS
    while True:
        result = agi("WAIT FOR DIGIT 100")
        choice = _choice(result, answered)
        if choice is not None:
            return choice, b""

        buffer.extend(_read_pcm())
        now = monotonic()
        complete = len(buffer) - len(buffer) % FRAME_BYTES
        for index in range(0, complete, FRAME_BYTES):
            utterance.add(bytes(buffer[index:index + FRAME_BYTES]), now)
        del buffer[:complete]

        if result == ord("#"):
            break
        if utterance.last_speech is None:
            if now >= start_deadline:
                break
        elif now - utterance.last_speech >= END_SILENCE_SECONDS:
            break

    if utterance.too_long:
        return "too_long", b""
    return None, utterance.audio()


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
            readable, _, _ = select([connection.sock], [], [], 0)
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
        "answered", "operator", "done", "repeat", "emergency", "urgent_operator", "no_answer",
        "confirm_reference", *REFERENCE_PROMPTS,
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
        answer_path = Path(directory, "answer")
        while True:
            digits = "012" if history else "02"
            if prompt is not None:
                choice = _choice(_play(prompt, digits), bool(history))
                if choice is not None:
                    return choice
            choice, audio = listen(digits, bool(history))
            if choice == "too_long":
                prompt = phrases / "bot_retry_long"
                continue
            if choice is not None:
                return choice
            if audio:
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
                case "emergency":
                    if _play(phrases / "bot_emergency", "") >= 0:
                        return "emergency"
                    return "operator"
                case "urgent_operator":
                    _play(phrases / "bot_urgent_operator", "")
                    return "operator"
                case "no_answer":
                    _play(phrases / "bot_no_answer", "")
                    return "operator"
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
    os.set_blocking(AUDIO_FD, False)
    result = dialogue(secret, phrases, suffix, linkedid)
    if result in ("answered", "ended", "emergency"):
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
