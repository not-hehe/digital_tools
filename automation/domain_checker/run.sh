#!/bin/bash
# Запуск проверки доменов. Это то, что ставится в расписание:
#
#   */5 * * * * /opt/domain-checker/run.sh >> /var/log/domain-checker.log 2>&1
#
# Аргументы пробрасываются в main.py, так что вручную можно и так:
#
#   ./run.sh --limit 5 --no-notify
#
# Если прогон не состоялся, обёртка сама сообщает об этом в Mattermost:
# упавший прогон не может отправить сообщение о собственном падении.
# Код возврата main.py возвращается наружу как есть - на него можно повесить
# OnFailure= у systemd, не полагаясь на эту отправку.
set -uo pipefail

cd "$(dirname "$0")"

# Секреты и настройки живут в .env рядом со скриптом, а не в расписании:
# в crontab они попали бы в вывод ps у любого пользователя машины.
# set -a экспортирует всё, что объявлено в файле, чтобы python это увидел.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

PYTHON="venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

LOG="$(mktemp -t domain-checker)"
trap 'rm -f "$LOG"' EXIT

"$PYTHON" main.py "$@" 2>&1 | tee "$LOG"
CODE=${PIPESTATUS[0]}

# Код 2 - ошибка в аргументах, то есть человек в терминале. Он её и так видит,
# в канал такое слать незачем.
if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 2 ]; then
    "$PYTHON" tools/notify.py "$(printf '🔥 Прогон чекера не состоялся (код %s)\n\nХост: %s\nКаталог: %s\nВремя: %s\n\nПоследние строки вывода:\n```\n%s\n```\n\nПока прогон не состоялся, падения доменов не отслеживаются: счётчик отказов не растёт, и канал будет молчать так же, как при исправной работе.' \
        "$CODE" "$(hostname)" "$(pwd)" "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$(grep -vE "NotOpenSSLWarning|warnings.warn" "$LOG" | tail -n 10)")" || true
fi

exit "$CODE"
