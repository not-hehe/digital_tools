import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# Рабочий список закрыт .gitignore: в свежей копии его нет, и путь должен
# меняться без правки кода.
COLLECTORS_FILE = Path(os.environ.get("CC_COLLECTORS_FILE") or DATA_DIR / "collectors.txt")
STATE_FILE = DATA_DIR / "state.txt"
RUNLOG_FILE = DATA_DIR / "runs.log"
RUNLOG_MAX_LINES = 5000   # около 100 суток часовых прогонов с событиями

# Три попытки гасят одиночное моргание: подтверждать падение
# следующим прогоном нельзя, он через час.
REQUEST_TIMEOUT = 15
ATTEMPTS = 3
RETRY_PAUSE = 10
# Таймаут чтения сбрасывается с каждым байтом: сервер, отдающий заголовки по байту,
# тянул бы попытку бесконечно. Дольше потолка попытка обрывается, считается
# таймаутом и повторов не получает.
ATTEMPT_DEADLINE = 30
MAX_WORKERS = 15   # все коллекторы разом: худший прогон равен худшей проверке, а не их сумме
USER_AGENT = "collector-checker/1.0"

HEALTHY_CODE = 200

# От стольких упавших (или поднявшихся) в одном прогоне - одно сообщение списком
# вместо перечня одиночных.
GROUP_MIN_DOWN = 3

TELEGRAM_TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_VAR = "TELEGRAM_CHAT_ID"
TELEGRAM_HOST = "api.telegram.org"
# Запасные адреса тех же серверов: из сети работодателя имя api.telegram.org не проходит,
# а прямой IP с заголовком Host проходит (так шлют коллеги). Перебираются по очереди
# после имени. Сертификат проверяется и по IP, см. alerting/notifier.py.
TELEGRAM_IPS = ("149.154.167.220", "149.154.167.198", "149.154.166.110")
TELEGRAM_TIMEOUT = 10
TELEGRAM_ENDPOINT_PAUSE = 2

MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    """Московское время без tzinfo: отметки идут в файл состояния и в сообщения."""
    return datetime.now(tz=MSK).replace(tzinfo=None)
