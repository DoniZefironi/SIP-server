import wave
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

from app.db.models import Call

AudioKind = Literal["mix", "client", "staff", "message"]
_SUFFIXES = {"mix": ".wav", "client": "-client.wav", "staff": "-staff.wav"}


@dataclass(frozen=True, slots=True)
class RecordingInfo:
    path: Path
    duration_seconds: int


def stereo_wav(client: Path, staff: Path, max_bytes: int) -> bytes | None:
    """Упаковать синхронные дорожки Asterisk: клиент слева, сотрудник справа."""
    output = BytesIO()
    with wave.open(str(client), "rb") as left, wave.open(str(staff), "rb") as right:
        if (
            left.getnchannels() != 1
            or right.getnchannels() != 1
            or left.getsampwidth() != 2
            or right.getsampwidth() != 2
            or left.getframerate() != right.getframerate()
        ):
            return None
        frames = max(left.getnframes(), right.getnframes())
        if not frames or 44 + frames * 4 > max_bytes:
            return None

        with wave.open(output, "wb") as stereo:
            stereo.setnchannels(2)
            stereo.setsampwidth(2)
            stereo.setframerate(left.getframerate())
            stereo.setnframes(frames)
            for start in range(0, frames, 16384):
                count = min(16384, frames - start)
                left_pcm = left.readframes(count).ljust(count * 2, b"\0")
                right_pcm = right.readframes(count).ljust(count * 2, b"\0")
                combined = bytearray(count * 4)
                combined[0::4], combined[1::4] = left_pcm[0::2], left_pcm[1::2]
                combined[2::4], combined[3::4] = right_pcm[0::2], right_pcm[1::2]
                stereo.writeframesraw(combined)
    return output.getvalue()


def recording_info(call: Call, kind: AudioKind, root: Path) -> RecordingInfo | None:
    """Путь и длительность завершённой, непустой WAV-записи звонка."""
    if call.ended_at is None:
        return None

    if kind == "message":
        raw = call.message_file
    else:
        raw = call.recording + _SUFFIXES[kind] if call.recording else None
    if not raw:
        return None

    try:
        path = Path(raw).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return None

        with wave.open(str(path), "rb") as recording:
            frames = recording.getnframes()
            rate = recording.getframerate()
            if frames == 0 or rate <= 0:
                return None

            # Один последний кадр позволяет заметить обрезанный файл без чтения всей записи.
            recording.setpos(frames - 1)
            frame_size = recording.getsampwidth() * recording.getnchannels()
            if len(recording.readframes(1)) != frame_size:
                return None
    except (OSError, EOFError, wave.Error):
        # Файл может появиться чуть позже события AMI или быть уже удалён по сроку хранения.
        return None

    return RecordingInfo(path, frames // rate)


def ready_recording(call: Call, kind: AudioKind, root: Path) -> Path | None:
    info = recording_info(call, kind, root)
    return info.path if info is not None else None
