import wave
from base64 import b64decode
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from app.db.models import Call
from app.services.recordings import ready_recording
from app.services.transcription import _wav_content


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


def test_transcription_uses_aligned_channels_and_falls_back_to_mix(tmp_path: Path) -> None:
    client = tmp_path / "call-client.wav"
    staff = tmp_path / "call-staff.wav"
    mix = tmp_path / "call.wav"
    write_wav(client, b"\x01\x02\x03\x04")
    write_wav(mix, b"\x00\x01\x00\x01")
    call = Call(recording=str(mix.with_suffix("")), ended_at=datetime.now(UTC))

    content, dialogue = _wav_content(call, "mix", tmp_path)
    assert not dialogue
    assert b64decode(content) == mix.read_bytes()

    write_wav(staff, b"\x05\x06")
    content, dialogue = _wav_content(call, "mix", tmp_path)
    assert dialogue
    with wave.open(BytesIO(b64decode(content)), "rb") as stereo:
        assert (stereo.getnchannels(), stereo.getframerate()) == (2, 8000)
        assert stereo.readframes(2) == b"\x01\x02\x05\x06\x03\x04\x00\x00"
