import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"

# Путь к рабочему списку задаётся переменной окружения.
COLLECTORS_FILE = Path(os.environ.get("CC_COLLECTORS_FILE") or DATA_DIR / "collectors.txt")
STATE_FILE = DATA_DIR / "state.txt"
RUNLOG_FILE = DATA_DIR / "runs.log"
RUNLOG_MAX_LINES = 5000   # около 100 суток

# Повторы внутри прогона: следующий прогон через час.
REQUEST_TIMEOUT = 15
ATTEMPTS = 3
RETRY_PAUSE = 10
# Предел длительности одной попытки; таймаут выше действует между байтами.
ATTEMPT_DEADLINE = 30
MAX_WORKERS = 15   # все коллекторы параллельно
USER_AGENT = "collector-checker/1.0"

HEALTHY_CODE = 200

# От стольких упавших (или поднявшихся) за прогон формируется одно сообщение списком.
GROUP_MIN_DOWN = 3

TELEGRAM_TOKEN_VAR = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_VAR = "TELEGRAM_CHAT_ID"
TELEGRAM_HOST = "api.telegram.org"
# Сначала IP с заголовком Host, затем имя.
TELEGRAM_IPS = ("149.154.166.110", "149.154.167.220", "149.154.167.198")
# Проверка сертификата при соединении по IP. CC_TELEGRAM_VERIFY=0 отключает её;
# без проверки токен доступен любому посреднику.
TELEGRAM_VERIFY_CERT = os.environ.get("CC_TELEGRAM_VERIFY", "1").strip().lower() not in ("0", "false", "no")
TELEGRAM_TIMEOUT = 10
TELEGRAM_ENDPOINT_PAUSE = 2

MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    """Московское время без tzinfo для файлов и сообщений."""
    return datetime.now(tz=MSK).replace(tzinfo=None)
