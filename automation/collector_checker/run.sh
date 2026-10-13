#!/bin/bash
# Обёртка для расписания: один прогон collector_checker с записью вывода
# в суточный лог. Строка для cron:
#
#   0 * * * * /path/to/collector_checker/run.sh > /dev/null 2>&1
#
# Перенаправление нужно: вывод и так ложится в logs/, а без него cron слал бы
# его почтой каждый час. Аргументы пробрасываются в main.py: ./run.sh --no-notify
#
# Если прогон завершился ошибкой или не смог стартовать (нет python, нет прав
# на data/), обёртка сама сообщает об этом в Telegram: упавший прогон не может
# отправить сообщение о собственном падении. Чего обёртка не ловит: cron, который
# не вызвал её вовсе. Это видно только по пропавшим строкам в data/runs.log.
set -uo pipefail

cd "$(dirname "$0")" || exit 1

# Секреты живут в .env рядом со скриптом: в crontab они попали бы в вывод ps.
# set -a экспортирует всё объявленное в файле, чтобы python это увидел.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

PYTHON="venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi

LOG_FILE="logs/collector_checker_$(date +%Y-%m-%d).log"

log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE" >&2
}

# Отправка сообщения о сбое. Сначала через notify.py, а если python нет или он
# сам упал - через curl прямо отсюда: иначе падение окружения осталось бы немым.
alert() {
    local text="$1"
    if [ -n "$PYTHON" ] && "$PYTHON" tools/notify.py "$text" 2>&1 | while IFS= read -r l; do log "$l"; done; then
        return 0
    fi
    if command -v curl >/dev/null && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        # -f: без него 401 от Telegram выглядел бы как успех. tr: .env с CRLF.
        local token chat
        token="$(printf '%s' "$TELEGRAM_BOT_TOKEN" | tr -d '\r ')"
        chat="$(printf '%s' "$TELEGRAM_CHAT_ID" | tr -d '\r ')"
        if curl -sSf -m 15 -o /dev/null "https://api.telegram.org/bot${token}/sendMessage" \
                --data-urlencode "chat_id=${chat}" --data-urlencode "text=${text}" 2>/dev/null; then
            log "сообщение о сбое ушло через curl"
            return 0
        fi
    fi
    log "сообщение о сбое не доставлено: notify.py и curl не справились (нет python или curl, не заданы TELEGRAM_* или Telegram отказал)"
    return 1
}

fail_start() {
    log "$1"
    alert "$(printf 'Прогон collector_checker не стартовал: %s\n\nХост: %s\nКаталог: %s\nВремя: %s' \
        "$1" "$(hostname)" "$(pwd)" "$(date '+%Y-%m-%d %H:%M:%S %Z')")"
    exit 1
}

if ! mkdir -p logs data 2>/dev/null || [ ! -w data ] || [ ! -w logs ]; then
    fail_start "нет прав на запись в data/ или logs/ ($(pwd))"
fi
if [ -z "$PYTHON" ]; then
    fail_start "python не найден: ни venv/bin/python, ни python3 в PATH"
fi

# Два прогона разом (cron и ручной запуск в ту же минуту) пишут одно состояние
# через один временный файл и шлют сообщения дважды. mkdir атомарен на обеих ОС,
# flock на macOS нет. В локе лежит pid подоболочки с самим прогоном: лок, переживший
# перезагрузку, иначе глушил бы все прогоны навсегда, и снаружи это ничем
# не отличалось бы от нормы. ps вместо kill -0: чужой pid даёт kill-у EPERM.
alive() {
    if command -v ps >/dev/null; then
        ps -p "$1" >/dev/null 2>&1
    else
        kill -0 "$1" 2>/dev/null
    fi
}

LOCK_DIR="data/.run.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    OTHER="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")"
    if [ -n "$OTHER" ] && alive "$OTHER"; then
        log "прогон уже идёт (pid $OTHER), выходим"
        exit 0
    fi
    log "снимаю протухший лок (pid ${OTHER:-неизвестен} не живой)"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || fail_start "не удалось создать лок $LOCK_DIR"
fi

# Вывод этого прогона отдельно от суточного лога: суточный дописывается
# и в нём хвост может принадлежать прошлым прогонам. mktemp ведёт себя по-разному:
# macOS принимает голое имя с -t, GNU mktemp требует шаблон с XXXXXX.
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
    RUN_OUT="$(mktemp -t collector-checker)"
elif [ "$OS" = "Linux" ]; then
    RUN_OUT="$(mktemp "${TMPDIR:-/tmp}/collector-checker.XXXXXX")"
else
    RUN_OUT="$(mktemp "/tmp/collector-checker.XXXXXX")"
fi
if [ -z "$RUN_OUT" ]; then
    log "не удалось создать временный файл, хвост лога в сообщение о сбое не попадёт"
    RUN_OUT="/dev/null"
fi
trap 'rm -rf "$LOCK_DIR"; [ "$RUN_OUT" != "/dev/null" ] && rm -f "$RUN_OUT"' EXIT

# Прогон идёт в фоновой подоболочке, а обёртка ждёт её: bash не обрабатывает
# сигналы, пока ждёт конвейер на переднем плане, и kill по pid обёртки оставлял
# python сиротой без лока. Лок держит pid подоболочки, он жив ровно столько,
# сколько прогон. Конвейер вместо exec > >(...): при exec последние строки
# терялись. Метки времени ставит цикл read, -u нужен, чтобы они стояли
# в момент события, а не сброса буфера.
(
    "$PYTHON" -u main.py "$@" 2>&1 \
        | while IFS= read -r line || [ -n "$line" ]; do
            echo "$(date +'%Y-%m-%d %H:%M:%S') $line"
          done | tee -a "$LOG_FILE" | tee "$RUN_OUT"
    exit "${PIPESTATUS[0]}"
) &
SUB=$!
echo "$SUB" > "$LOCK_DIR/pid"
# По TERM/INT гасим весь прогон, а не только обёртку. pkill есть на macOS и в procps.
trap 'log "получен сигнал, останавливаю прогон"; pkill -TERM -P "$SUB" 2>/dev/null; kill -TERM "$SUB" 2>/dev/null; exit 143' TERM INT
wait "$SUB"
CODE=$?
trap - TERM INT

# Код 2 это ошибка в аргументах, то есть человек в терминале: он её видит сам.
if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 2 ]; then
    alert "$(printf 'Прогон collector_checker завершился ошибкой (код %s): список не загружен, состояние не сохранено, сообщение не ушло или python упал\n\nХост: %s\nКаталог: %s\nВремя: %s\n\nПоследние строки лога:\n%s' \
        "$CODE" "$(hostname)" "$(pwd)" "$(date '+%Y-%m-%d %H:%M:%S %Z')" \
        "$(grep -vE "NotOpenSSLWarning|warnings.warn" "$RUN_OUT" | tail -n 10)")"
fi

exit "$CODE"
