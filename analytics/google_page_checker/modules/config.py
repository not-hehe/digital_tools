import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
INPUT_FILE = DATA_DIR / "input_urls.xlsx"
OUTPUT_FILE = DATA_DIR / "output_report.txt"      # расширенный отчёт
SUMMARY_FILE = DATA_DIR / "output_summary.txt"    # краткий отчёт «для чата»
LOG_FILE = BASE_DIR / "logs" / "app.log"

EXCEL_COLUMN_NAME = "url"
# None — читать ВСЕ листы документа по порядку (на каждом ищется колонка
# EXCEL_COLUMN_NAME); номер или имя — только один конкретный лист.
EXCEL_SHEET_NAME = None
# Сколько ПУСТЫХ строк подряд считается концом данных листа: меньшие
# разрывы проходим насквозь, на этом числе переходим к следующему листу.
EXCEL_EMPTY_ROW_BREAK = 25

BROWSER_HEADLESS = True
# Задержка перед каждой операцией Playwright. Ноль: детекту она не помогала,
# а на 372 сайтах в несколько прогонов превращалась в часы ожидания.
BROWSER_SLOW_MO = 0
BLOCK_RESOURCES = False
MAX_CONCURRENT_PAGES = 10

# Разрешить Chromium договариваться по устаревшим версиям TLS.
# В боевом логе 6 сайтов отдавали ERR_SSL_VERSION_OR_CIPHER_MISMATCH —
# это серверы на TLS 1.0/1.1, которые современный Chromium по умолчанию
# отвергает ещё до отправки запроса.
ALLOW_LEGACY_TLS = True

# ---------------------------------------------------------------------------
# Навигация и ожидание
# ---------------------------------------------------------------------------

# Событие, по которому считаем навигацию состоявшейся.
# domcontentloaded вместо load: на тяжёлых сайтах load не наступал за 45 с,
# и страница уходила в ERROR, хотя вёрстка давно была получена. Событие
# load всё равно дожидается ниже — но уже мягко, без потери страницы.
NAV_WAIT_UNTIL = "domcontentloaded"

PAGE_TIMEOUT = 30000        # мс на саму навигацию
LOAD_STATE_TIMEOUT = 15000  # мс на мягкое дожидание события load

# Ждём затишья сети (networkidle)
NETWORKIDLE_TIMEOUT = 15000  # мс

# Гарантированный минимум ожидания ПОСЛЕ события load
MIN_WAIT_AFTER_LOAD = 4.0  # секунд

# Прицельное дожидание запроса к контейнеру
TARGETED_WAIT_TIMEOUT = 10000  # мс

# Зонд состояния контейнера
PROBE_TIMEOUT = 10000  # мс

# Повторные попытки — только для транзиентных отказов (см. modules/failures.py).
# Домен без записи DNS и мусорная ячейка Excel за паузу не починятся, поэтому
# на них попытка ровно одна.
MAX_RETRIES = 3
RETRY_PAUSE = 3.0   # секунд между попытками

# Полных прогонов всего списка URL за один запуск.
# В боевом логе 20 прогонов дали двадцать одинаковых результатов при
# стоимости около четырёх часов; четырёх достаточно, чтобы поймать
# нестабильные страницы.
RUN_COUNT = 4

# ---------------------------------------------------------------------------
# Ответ сервера кодом ошибки
# ---------------------------------------------------------------------------

# Разбирать ли содержимое страницы, если сервер ответил кодом >= 400.
# Сервер ответил — значит страница есть: кастомная 404-я или страница
# «доступ запрещён» вполне может тащить тот же GTM, что и остальной сайт.
ANALYZE_HTTP_ERRORS = True

# Коды, при которых имеет смысл повторить попытку: это транзиентные
# состояния бэкенда, а не постоянный ответ. Прочие коды >= 400 считаются
# стабильными и не ретраятся — страница разбирается сразу.
HTTP_TRANSIENT_CODES = frozenset({408, 425, 429, 500, 502, 503, 504,
                                  520, 521, 522, 523, 524})

# ---------------------------------------------------------------------------
# Сетевые паттерны
# ---------------------------------------------------------------------------

# Широкая сеть: любой запрос к инфраструктуре Google Analytics
GA_PATTERNS = [
    r"google-analytics\.com",
    r"googletagmanager\.com",
    r"/g/collect",
    r"/mp/collect",
    r"analytics\.google\.com",
]

# Настоящие хиты аналитики
GA_COLLECT_PATTERNS = [
    r"google-analytics\.com/collect",
    r"google-analytics\.com/[a-z]/collect",
    r"google-analytics\.com/mp/collect",
    r"google-analytics\.com/batch",
    r"analytics\.google\.com/g/collect",
]

# Служебная телеметрия ошибок библиотеки GA (например,
# https://www.google-analytics.com/u/d?t=error&_e=exc&...) это НЕ хит аналитики
GA_ERROR_MARKERS = ("t=error", "_e=exc")

# ---------------------------------------------------------------------------
# HTML-маркеры: признаки GTM/GA в вёрстке страницы
# ---------------------------------------------------------------------------

# СИЛЬНЫЕ маркеры — идентификаторы собственной установки. Настоящий сниппет, вставленный владельцем прямо в HTML, практически всегда несёт ID.
HTML_GTM_STRONG_PATTERNS = [
    r"GTM-[A-Z0-9]{4,}",                      # ID контейнера Google Tag Manager
    r"id=G-[A-Z0-9]{6,}",                     # ID потока GA4 в src скрипта gtag
    r"['\"]G-[A-Z0-9]{6,}['\"]",              # ID потока GA4 в gtag('config', 'G-...')
    r"UA-\d{4,}-\d+",                         # ID Universal Analytics
]

# СЛАБЫЕ маркеры — упоминания без ID. Часто принадлежат чужим скриптам (счётчики, баннеры согласия, чаты, интеграции вида «если есть gtag —
# продублируй событие») и сами по себе установкой GA НЕ считаются.
HTML_GTM_WEAK_PATTERNS = [
    r"googletagmanager\.com/gtm\.js",         # подключение контейнера GTM
    r"googletagmanager\.com/gtag/js",         # подключение gtag.js
    r"googletagmanager\.com/ns\.html",        # noscript-iframe контейнера
    r"google-analytics\.com/analytics\.js",   # старый analytics.js
    r"gtag\(",                                # вызовы gtag(...) в inline-скриптах
]

# Полный список (для кода, которому не важна сила маркера).
HTML_GTM_PATTERNS = HTML_GTM_STRONG_PATTERNS + HTML_GTM_WEAK_PATTERNS

# ---------------------------------------------------------------------------
# Статусы классификации сайта
# ---------------------------------------------------------------------------

STATUS_WORKING = "WORKING"        # есть настоящие collect-хиты
STATUS_BROKEN = "BROKEN"          # хитов нет, но GA есть в вёрстке
STATUS_SUSPICIOUS = "SUSPICIOUS"  # только error-телеметрия, в вёрстке пусто
STATUS_NONE = "NONE"              # ни хитов, ни следов в вёрстке
STATUS_ERROR = "ERROR"            # страницу не удалось загрузить

# Статусы, при которых сайт считается имеющим Google Analytics.
GA_POSITIVE_STATUSES = {STATUS_WORKING, STATUS_BROKEN}

# ---------------------------------------------------------------------------
# Детализация BROKEN: почему тег есть, а аналитика не работает
# ---------------------------------------------------------------------------

# Причина определяется по HTTP-статусу запросов загрузки контейнера
# (googletagmanager.com/gtm.js, /gtag/js)
BROKEN_CONTAINER_DELETED = "container_deleted"  # gtm.js -> 404/410
BROKEN_LOADS_NO_HITS = "loads_no_hits"          # gtm.js -> 2xx, а хитов нет
BROKEN_NO_RESPONSE = "no_response"              # запрос ушёл, ответа нет
BROKEN_ALIVE_NOT_LOADED = "alive_not_loaded"    # зонд: контейнер жив, но
                                                # страница его не запрашивает
BROKEN_NOT_LOADED = "not_loaded"                # запроса к контейнеру не было
BROKEN_UA_SUNSET = "ua_sunset"                  # тег Universal Analytics,
                                                # системы больше не существует

BROKEN_REASON_LABELS = {
    BROKEN_CONTAINER_DELETED: "контейнер удалён (gtm.js → 404)",
    BROKEN_LOADS_NO_HITS: "контейнер загружается, но хиты не отправляются",
    BROKEN_NO_RESPONSE: "запрос к контейнеру ушёл, ответа нет",
    BROKEN_ALIVE_NOT_LOADED: "контейнер жив (по зонду), но страница его "
                             "не запрашивает — похоже, выключен конфигом",
    BROKEN_NOT_LOADED: "загрузка контейнера не зафиксирована "
                       "(выключен конфигом или не успел за окно ожидания)",
    BROKEN_UA_SUNSET: "устаревший тег Universal Analytics — Google остановил "
                      "сбор данных 01.07.2023 и удалил ресурсы 01.07.2024, "
                      "тег не работает по определению",
}

# ---------------------------------------------------------------------------
# Mattermost (Incoming Webhook)
# ---------------------------------------------------------------------------

# Адрес вебхука — секрет: он даёт право писать в канал любому, у кого есть
# доступ к репозиторию. Поэтому он не в коде, но и экспортировать его руками
# перед каждым запуском не нужно — ищем в двух местах по порядку:
#
#   1. переменная окружения GPC_MATTERMOST_WEBHOOK (удобно для CI);
#   2. файл secrets/mattermost_webhook.txt рядом с проектом — он закрыт
#      .gitignore, так что в репозиторий не попадёт.
#
# Ничего не нашлось — отчёт просто не отправляется, прогон не падает.
SECRETS_DIR = BASE_DIR / "secrets"
MATTERMOST_WEBHOOK_FILE = SECRETS_DIR / "mattermost_webhook.txt"


def _read_webhook() -> str:
    from_env = os.environ.get("GPC_MATTERMOST_WEBHOOK", "").strip()
    if from_env:
        return from_env
    try:
        # Первая непустая строка, не начинающаяся с #: файл можно
        # комментировать, не ломая чтение.
        for line in MATTERMOST_WEBHOOK_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    except OSError:
        pass
    return ""


MATTERMOST_WEBHOOK_URL = _read_webhook()
MATTERMOST_TIMEOUT = 10  # секунд на отправку

LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
