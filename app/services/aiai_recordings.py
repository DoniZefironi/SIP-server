import asyncio
import io
import wave
from pathlib import Path

from app.db.models import Call
from app.services.aiai import AiaiError, AiaiSpeech
from app.services.recordings import ready_recording

_MAX_RECORDING_BYTES = 60_000_000
_AUDIO_CHUNK_BYTES = 8_000_000


def recording_paths(call: Call, kind: str, root: Path) -> list[Path]:
    if kind == "mix":
        caller = ready_recording(call, "client", root)
        staff = ready_recording(call, "staff", root)
        if caller is not None and staff is not None:
            return [caller, staff]
    path = ready_recording(call, "message" if kind == "message" else "mix", root)
    if path is None:
        raise AiaiError("Запись недоступна", retryable=True)
    return [path]


def _wav_chunks(path: Path, *, split_ms: int | None = None) -> list[tuple[int, bytes]]:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            sample_width = wav.getsampwidth()
            frame_size = wav.getnchannels() * sample_width
            rate = wav.getframerate()
            if frames * frame_size > _MAX_RECORDING_BYTES:
                raise AiaiError("Запись превышает лимит расшифровки (60 МБ на дорожку)")
            frames_per_chunk = (_AUDIO_CHUNK_BYTES - 44) // frame_size
            split = (split_ms * rate + 999) // 1000 if split_ms is not None else 0
            boundaries = (0, split, frames) if 0 < split < frames else (0, frames)
            result = []
            for left, right in zip(boundaries[:-1], boundaries[1:], strict=True):
                for start in range(left, right, frames_per_chunk):
                    count = min(frames_per_chunk, right - start)
                    pcm = wav.readframes(count)
                    if len(pcm) != count * frame_size:
                        raise AiaiError("Запись неполная", retryable=True)
                    leading_frames = 0
                    if sample_width > 1:
                        # STT может проставить первой фразе 0 даже после длинной паузы.
                        leading_frames = (len(pcm) - len(pcm.lstrip(b"\x00"))) // frame_size
                    if leading_frames == count:
                        continue
                    with io.BytesIO() as buffer:
                        with wave.open(buffer, "wb") as chunk:
                            chunk.setparams(wav.getparams())
                            chunk.writeframes(pcm[leading_frames * frame_size:])
                        offset_ms = (start + leading_frames) * 1000 // rate
                        result.append((offset_ms, buffer.getvalue()))
            return result
    except (OSError, EOFError, wave.Error) as error:
        raise AiaiError("Запись недоступна", retryable=True) from error


async def recognize_tracks(
    client: AiaiSpeech, paths: list[Path], *, staff_label: str, bot_before_ms: int | None,
) -> str:
    dialogue = len(paths) == 2
    text_parts = []
    dialogue_parts: list[tuple[int, int, str, str]] = []
    for channel, path in enumerate(paths):
        split_ms = bot_before_ms if channel == 1 else None
        for offset, audio in await asyncio.to_thread(_wav_chunks, path, split_ms=split_ms):
            text, phrases = await client.transcribe_recording(audio, with_phrases=dialogue)
            if dialogue:
                speaker = "Клиент" if channel == 0 else staff_label
                if split_ms is not None and offset < split_ms:
                    speaker = "Ася / АТС"
                dialogue_parts.extend(
                    (offset + p.start_ms, channel, speaker, p.text) for p in phrases
                )
            elif text:
                text_parts.append(text)
    if not dialogue:
        return " ".join(text_parts)

    lines = []
    for start, _, speaker, text in sorted(dialogue_parts, key=lambda item: item[:2]):
        minutes, seconds = divmod(start // 1000, 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {speaker}: {text}")
    return "\n".join(lines)
