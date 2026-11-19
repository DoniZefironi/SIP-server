import wave
from pathlib import Path
from typing import Literal

from app.db.models import Call

AudioKind = Literal["mix", "client", "staff", "message"]
_SUFFIXES = {"mix": ".wav", "client": "-client.wav", "staff": "-staff.wav"}


def ready_recording(call: Call, kind: AudioKind, root: Path) -> Path | None:
    """Путь к завершённой, непустой WAV-записи звонка."""
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
            if frames == 0:
                return None

            # Один последний кадр позволяет заметить обрезанный файл без чтения всей записи.
            recording.setpos(frames - 1)
            frame_size = recording.getsampwidth() * recording.getnchannels()
            if len(recording.readframes(1)) != frame_size:
                return None
    except (OSError, EOFError, wave.Error):
        # Файл может появиться чуть позже события AMI или быть уже удалён по сроку хранения.
        return None

    return path
