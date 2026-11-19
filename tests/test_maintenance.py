import os
import time
from pathlib import Path

from app.services.maintenance import clean_recordings

DAY = 86400


def make(path: Path, size: int, age_seconds: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    os.utime(path, (now - age_seconds, now - age_seconds))
    return path


def test_clean_recordings(tmp_path: Path) -> None:
    now = time.time()
    calls, messages = tmp_path / "calls", tmp_path / "messages"
    empty_old = make(calls / "a.wav", 44, 2 * 3600, now)
    empty_fresh = make(calls / "b.wav", 44, 60, now)  # клиент ещё ждёт в очереди
    talk = make(calls / "c.wav", 5000, 10 * DAY, now)
    expired = make(calls / "d.wav", 5000, 400 * DAY, now)
    message_short = make(messages / "m.wav", 44, 2 * 3600, now)  # сообщения не трогаем
    message_old = make(messages / "n.wav", 5000, 400 * DAY, now)
    other = make(calls / "notes.txt", 0, 400 * DAY, now)

    assert clean_recordings(tmp_path, 365, now) == (1, 2)

    assert not empty_old.exists()
    assert empty_fresh.exists()
    assert talk.exists()
    assert not expired.exists()
    assert message_short.exists()
    assert not message_old.exists()
    assert other.exists()


def test_missing_folders_are_fine(tmp_path: Path) -> None:
    assert clean_recordings(tmp_path / "nope", 365) == (0, 0)
