#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( EUID != 0 )); then
  echo 'Запустите скрипт через sudo.' >&2
  exit 1
fi

cd -- "${1:-/opt/savt-telephony}"
revision=$(git rev-parse --short=12 HEAD)
extensions=$(readlink -f /etc/asterisk/extensions.conf)
agi_dir=$(timeout 5s asterisk -rx 'core show settings' |
  sed -n 's/^[[:space:]]*AGI Scripts directory:[[:space:]]*//p')
if [[ -z "$agi_dir" ]]; then
  echo 'Asterisk не сообщил путь к AGI.' >&2
  exit 1
fi

work=$(mktemp -d)
candidate=$(mktemp --tmpdir="$(dirname -- "$extensions")" .extensions.savt.XXXXXX)
trap 'rm -f -- "$candidate" "$work"/active "$work"/bot_*.wav; rmdir -- "$work"' EXIT
cp -p -- "$extensions" "$work/active"
cp -p -- "$work/active" "$candidate"

agi_file="savt-voice-bot-$revision.py"
phrase_dir="savt-voice-$revision"
python3 - "$work/active" asterisk/extensions.conf "$candidate" "$agi_file" "$phrase_dir" <<'PY'
import re
import sys
from pathlib import Path


def bot_context(text):
    headers = list(re.finditer(rb"(?m)^[ \t]*\[([^]\r\n]+)\][^\r\n]*\r?$", text))
    indices = [i for i, match in enumerate(headers) if match[1] == b"voice-bot-pilot"]
    if len(indices) != 1:
        raise SystemExit("Ожидается один контекст [voice-bot-pilot].")
    index = indices[0]
    start = headers[index].start()
    end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
    block = text[start:end]
    # Оставляем завершающие пустые строки и комментарии в исходном файле.
    lines = block.splitlines(keepends=True)
    while lines[-1].lstrip().startswith(b";") or not lines[-1].strip():
        lines.pop()
    return start, start + sum(map(len, lines))


active_path, source_path, output_path = map(Path, sys.argv[1:4])
agi_file, phrase_dir = (value.encode("ascii") for value in sys.argv[4:])
active = active_path.read_bytes()
source = source_path.read_bytes().replace(b"\r\n", b"\n")
start, end = bot_context(active)
source_start, source_end = bot_context(source)
replacement = source[source_start:source_end]
invocation = b"EAGI(savt-voice-bot.py,${PHRASES}"
if replacement.count(invocation) != 1:
    raise SystemExit("В новом контексте не найден вызов голосового AGI.")
replacement = replacement.replace(invocation, b"EAGI(" + agi_file + b",${PHRASES}/" + phrase_dir)
if b"\r\n" in active[start:end]:
    replacement = replacement.replace(b"\n", b"\r\n")
output_path.write_bytes(active[:start] + replacement + active[end:])
PY

echo 'Обновляю приложение'
docker compose up -d --build --no-deps app
docker compose exec -T app python -m app.services.voice_bot "/tmp/$phrase_dir" </dev/null
docker compose cp "app:/tmp/$phrase_dir/." "$work/" </dev/null

prompts="bot_question bot_feedback bot_question_afterhours bot_feedback_afterhours
bot_ask_question bot_reference_found bot_reference_unavailable bot_retry bot_retry_long bot_wait
bot_emergency bot_urgent_operator"
for prompt in $prompts; do
  if [[ ! -s "$work/$prompt.wav" ]]; then
    echo "Отсутствует $prompt.wav." >&2
    exit 1
  fi
done
install -d -o root -g asterisk -m 0755 "/var/lib/asterisk/phrases/$phrase_dir"
install -o root -g asterisk -m 0644 "$work"/bot_*.wav "/var/lib/asterisk/phrases/$phrase_dir/"
mkdir -p -- "$agi_dir"
install -o root -g asterisk -m 0755 asterisk/savt-voice-bot.py "$agi_dir/$agi_file"

echo 'Проверяю доступ Asterisk к голосовому API'
runuser -u asterisk -- python3 - <<'PY'
import base64
import io
import json
import wave
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path

secret = Path("/etc/asterisk/savt-voice-bot.secret").read_text(encoding="ascii").strip()
buffer = io.BytesIO()
with wave.open(buffer, "wb") as audio:
    audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
    audio.writeframes(bytes(320))
body = json.dumps({"audio": base64.b64encode(buffer.getvalue()).decode("ascii")})
with closing(HTTPConnection("127.0.0.1", 8000, timeout=5)) as connection:
    connection.request(
        "POST", "/internal/telephony/voice-turn", body=body,
        headers={"Content-Type": "application/json", "X-Telephony-Secret": secret},
    )
    with connection.getresponse() as response:
        if response.status != 200:
            raise SystemExit(f"Голосовой API вернул HTTP {response.status}.")
        if json.load(response).get("status") != "repeat":
            raise SystemExit("Голосовой API не подтвердил проверочную реплику.")
PY

if ! cmp -s -- "$work/active" "$extensions"; then
  echo 'Рабочий dialplan изменился во время установки. Запустите обновление повторно.' >&2
  exit 1
fi
backup=$(mktemp "${extensions}.bak.XXXXXXXX")
cp -p -- "$extensions" "$backup"
mv -fT -- "$candidate" "$extensions"

if ! timeout 10s asterisk -rx 'dialplan reload' ||
   ! timeout 5s asterisk -rx 'dialplan show voice-bot-pilot' | grep -F "$agi_file"; then
  cp -p -- "$backup" "$candidate"
  mv -fT -- "$candidate" "$extensions"
  timeout 10s asterisk -rx 'dialplan reload'
  echo 'Проверка нового маршрута завершилась ошибкой. Восстановлен предыдущий dialplan.' >&2
  exit 1
fi

printf 'Голосовой бот установлен. Версия %s. Резервная копия %s\n' "$revision" "$backup"
