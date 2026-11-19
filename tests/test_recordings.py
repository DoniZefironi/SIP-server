import wave
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import Call
from app.services.recordings import ready_recording


def write_wav(path: Path, frames: bytes) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        recording.writeframes(frames)


def test_recording_is_available_only_after_call_ends(tmp_path: Path) -> None:
    path = tmp_path / "conversation.wav"
    write_wav(path, b"\x00\x01" * 16)
    call = Call(recording=str(path.with_suffix("")))

    assert ready_recording(call, "mix", tmp_path) is None

    call.ended_at = datetime.now(UTC)
    assert ready_recording(call, "mix", tmp_path) == path


def test_message_is_available_once_the_file_is_complete(tmp_path: Path) -> None:
    path = tmp_path / "message.wav"
    call = Call(ended_at=datetime.now(UTC), message_file=str(path))

    assert ready_recording(call, "message", tmp_path) is None

    write_wav(path, b"")
    assert ready_recording(call, "message", tmp_path) is None

    write_wav(path, b"\x00\x01" * 16)
    content = path.read_bytes()
    path.write_bytes(content[:-1])
    assert ready_recording(call, "message", tmp_path) is None

    path.write_bytes(content)
    assert ready_recording(call, "message", tmp_path) == path
    assert ready_recording(call, "message", tmp_path / "other") is None
