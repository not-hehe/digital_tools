import os
from datetime import timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
INPUT_FILE = DATA_DIR / "input_urls.xlsx"

# ---------------------------------------------------------------------------
# Источник списка доменов
# ---------------------------------------------------------------------------

SOURCE_FILE = "file"

TARGET_SOURCE = os.environ.get("DC_TARGET_SOURCE", SOURCE_FILE)

EXCEL_COLUMN_NAME = "url"
EXCEL_SHEET_NAME = None
EXCEL_EMPTY_ROW_BREAK = 25

# ---------------------------------------------------------------------------
# Проверка
# ---------------------------------------------------------------------------

# Значения подобраны под цикл запуска раз в 5 минут. Худший случай — легла
# вся сеть и все домены уходят в таймаут: 366 / 100 * 5с ≈ 20 секунд.
# Прежние 10 потоков по 15 секунд давали в том же случае 9 минут, то есть
# прогоны начали бы наползать друг на друга.
REQUEST_TIMEOUT = 5
MAX_REDIRECTS = 5
MAX_WORKERS = 100
USER_AGENT = "domain-checker/1.0"

# Коллектор метрик-образец глушит прокси из окружения: ему нужен только
# внутренний Prometheus. Здесь адреса и внутренние, и публичные — если выход
# наружу на сервере идёт через прокси, False даст ложные отказы на всех
# публичных доменах. Проверяется первым прогоном на сервере.
TRUST_ENV = os.environ.get("DC_TRUST_ENV", "").lower() in ("1", "true", "yes")

# Здоровым считается любой полученный ответ с кодом из этого диапазона:
# 3xx входит, содержимое не разбирается.
HEALTHY_MIN = 200
HEALTHY_MAX = 400

MSK = timezone(timedelta(hours=3))

# ---------------------------------------------------------------------------
# Mattermost
# ---------------------------------------------------------------------------

# Адрес вебхука даёт право писать в канал любому, у кого он есть, поэтому
# в коде его нет. Пусто — сообщения не отправляются, прогон продолжается.
MATTERMOST_WEBHOOK_URL = os.environ.get("DC_MATTERMOST_WEBHOOK", "")
MATTERMOST_TIMEOUT = 10

LOG_LEVEL = os.environ.get("DC_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
