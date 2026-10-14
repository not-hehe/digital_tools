#!/bin/bash
# Запуск одного прогона из cron:  * * * * * /path/to/collector_checker/run.sh > /dev/null 2>&1
# Вывод пишется в logs/; перенаправление отключает почту cron.
# Аргументы передаются в main.py: ./run.sh --no-notify
# При ошибке прогона или невозможности запуска отправляет сообщение в Telegram,
# не чаще раза в FAIL_EVERY_MIN минут.
# Пропущенный cron-ом запуск не отслеживается; его видно по пропуску в data/runs.log.
set -uo pipefail

cd "$(dirname "$0")" || exit 1

# Секреты берутся из .env: crontab виден в ps всем пользователям.
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
# Под cron локаль бывает пустой, а в выводе кириллица.
export PYTHONUTF8=1
# urllib3 на macOS предупреждает про LibreSSL в каждом прогоне; к делу не относится.
export PYTHONWARNINGS="ignore:urllib3 v2 only supports OpenSSL"

LOG_FILE="logs/collector_checker_$(date +%Y-%m-%d).log"

log() {
    echo "$(date +'%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG_FILE" >&2
}

# Отправка сообщения о сбое: notify.py, при недоступном python curl.
send_alert() {
    local text="$1"
    if [ -n "$PYTHON" ] && "$PYTHON" tools/notify.py "$text" 2>&1 | while IFS= read -r l; do log "$l"; done; then
        return 0
    fi
    if command -v curl >/dev/null && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        local token chat
        token="$(printf '%s' "$TELEGRAM_BOT_TOKEN" | tr -d '\r ')"
        chat="$(printf '%s' "$TELEGRAM_CHAT_ID" | tr -d '\r ')"
        # -f: ответ 401 считается ошибкой.
        if curl -sSf -m 15 -o /dev/null "https://api.telegram.org/bot${token}/sendMessage" \
                --data-urlencode "chat_id=${chat}" --data-urlencode "text=${text}" 2>/dev/null; then
            log "сообщение о сбое ушло через curl"
            return 0
        fi
    fi
    log "сообщение о сбое не доставлено: notify.py и curl не справились (нет python или curl, не заданы TELEGRAM_* или Telegram отказал)"
    return 1
}

# Прогон идёт каждую минуту, сломанный скрипт слал бы 60 сообщений в час (Ваня, 2026-10-14).
# Минуты считаются целыми, как у напоминаний о коллекторах. Неотправленное отметку
# не ставит: следующий запуск попробует снова.
FAIL_STAMP="data/.fail_alert"
FAIL_EVERY_MIN=5

alert() {
    local now last passed
    now="$(date +%s)"
    last="$(cat "$FAIL_STAMP" 2>/dev/null || true)"
    if [[ "$last" =~ ^[0-9]+$ ]]; then
        passed=$(( now / 60 - last / 60 ))
        if [ "$passed" -ge 0 ] && [ "$passed" -lt "$FAIL_EVERY_MIN" ]; then
            log "сообщение о сбое отложено, ещё $(( FAIL_EVERY_MIN - passed )) мин"
            return 0
        fi
    fi
    send_alert "$1" || return 1
    echo "$now" 2>/dev/null > "$FAIL_STAMP" \
        || log "отметка $FAIL_STAMP не записана: сообщения о сбое пойдут каждую минуту"
}

fail_start() {
    log "$1"
    alert "$(printf 'Прогон collector_checker не стартовал: %s\nВремя: %s' \
        "$1" "$(date '+%Y-%m-%d %H:%M:%S %Z')")"
    exit 1
}

if ! mkdir -p logs data 2>/dev/null || [ ! -w data ] || [ ! -w logs ]; then
    fail_start "нет прав на запись в data/ или logs/"
fi
if [ -z "$PYTHON" ]; then
    fail_start "python не найден: ни venv/bin/python, ни python3 в PATH"
fi

# Лок через каталог: mkdir атомарен и есть на macOS и Linux.
# В каталоге лежит pid прогона; если процесс мёртв, лок считается протухшим.
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
# Pid самой обёртки, а не подоболочки: лок должен жить и пока уходит сообщение о сбое,
# иначе следующий запуск снимет его как протухший и прогоны пойдут вдвоём (аудит 2026-10-14).
echo "$$" > "$LOCK_DIR/pid"

# Вывод текущего прогона хранится отдельно: в сообщение о сбое попадает его хвост.
# Аргументы mktemp различаются на macOS и Linux.
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
# Чужой лок не снимаем: его мог забрать следующий запуск.
trap '[ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK_DIR"; [ "$RUN_OUT" != "/dev/null" ] && rm -f "$RUN_OUT"' EXIT

# Прогон в фоновой подоболочке: сигнал обёртке останавливает и python.
# -u: вывод без буферизации, метки времени соответствуют событиям.
(
    "$PYTHON" -u main.py "$@" 2>&1 \
        | while IFS= read -r line || [ -n "$line" ]; do
            echo "$(date +'%Y-%m-%d %H:%M:%S') $line"
          done | tee -a "$LOG_FILE" | tee "$RUN_OUT"
    exit "${PIPESTATUS[0]}"
) &
SUB=$!
trap 'log "получен сигнал, останавливаю прогон"; pkill -TERM -P "$SUB" 2>/dev/null; kill -TERM "$SUB" 2>/dev/null; exit 143' TERM INT
wait "$SUB"
CODE=$?
trap - TERM INT

# Код 2 (ошибка аргументов) не оповещается. Недоставка в Telegram кода 1 не даёт.
# Удачный прогон снимает отметку: новый сбой после починки сообщается сразу.
if [ "$CODE" -eq 0 ]; then
    rm -f "$FAIL_STAMP"
elif [ "$CODE" -ne 2 ]; then
    alert "$(printf 'Прогон collector_checker завершился ошибкой (код %s): список не загружен, состояние не сохранено или python упал\nВремя: %s\n\nПоследние строки лога:\n%s' \
        "$CODE" "$(date '+%Y-%m-%d %H:%M:%S %Z')" \
        "$(tail -n 10 "$RUN_OUT")")"
fi

exit "$CODE"
