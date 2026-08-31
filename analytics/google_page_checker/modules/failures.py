import re

# ---------------------------------------------------------------------------
# Виды отказов
# ---------------------------------------------------------------------------

FAIL_DNS = "dns"                    # домен не резолвится
FAIL_TLS = "tls"                    # рукопожатие TLS не состоялось
FAIL_TIMEOUT = "timeout"            # сервер не ответил за отведённое время
FAIL_REFUSED = "refused"            # соединение отвергнуто/оборвано
FAIL_REDIRECT_LOOP = "redirect_loop"  # бесконечная цепочка редиректов
FAIL_AUTH_REQUIRED = "auth_required"  # сервер требует учётные данные
FAIL_PROXY = "proxy"                # не удалось пройти через прокси
FAIL_INVALID_URL = "invalid_url"    # адрес синтаксически не является URL
FAIL_HTTP = "http_error"            # сервер ответил кодом >= 400
FAIL_BROWSER = "browser"            # сбой самого Playwright/Chromium
FAIL_OTHER = "other"                # не распознано

FAILURE_LABELS = {
    FAIL_DNS: "домен не резолвится (нет записи DNS или другой контур)",
    FAIL_TLS: "TLS-рукопожатие не состоялось (устаревший протокол или шифр)",
    FAIL_TIMEOUT: "сервер не ответил за отведённое время",
    FAIL_REFUSED: "соединение отвергнуто или оборвано",
    FAIL_REDIRECT_LOOP: "бесконечная цепочка редиректов",
    FAIL_AUTH_REQUIRED: "требуются учётные данные (HTTP-авторизация)",
    FAIL_PROXY: "прокси не пропустил соединение",
    FAIL_INVALID_URL: "строка не является адресом (мусор во входных данных)",
    FAIL_HTTP: "сервер ответил кодом ошибки",
    FAIL_BROWSER: "сбой браузера при обработке страницы",
    FAIL_OTHER: "прочая ошибка загрузки",
}

# Короткая подсказка «что с этим делать» — идёт в отчёт рядом с группой.
FAILURE_HINTS = {
    FAIL_DNS: "проверить по второму контуру или убрать из списка",
    FAIL_TLS: "сервер поддерживает только устаревший TLS — вопрос к владельцу",
    FAIL_TIMEOUT: "проверить доступность вручную; возможен фильтр по адресу",
    FAIL_REFUSED: "порт закрыт или доступ ограничен сетевой политикой",
    FAIL_REDIRECT_LOOP: "чаще всего цикл авторизации — нужна ручная проверка",
    FAIL_AUTH_REQUIRED: "закрыто HTTP-авторизацией, нужны учётные данные",
    FAIL_PROXY: "адрес не входит в разрешённые прокси — проверить контур",
    FAIL_INVALID_URL: "исправить ячейку во входном Excel",
    FAIL_HTTP: "сервер отвечает, но кодом ошибки — проверить маршрут",
    FAIL_BROWSER: "транзиентный сбой, повторить прогон",
    FAIL_OTHER: "разобрать вручную",
}

# Порядок вывода групп в отчёте: сверху то, что чинится нашими силами.
FAILURE_ORDER = (
    FAIL_INVALID_URL,
    FAIL_HTTP,
    FAIL_AUTH_REQUIRED,
    FAIL_PROXY,
    FAIL_TLS,
    FAIL_REDIRECT_LOOP,
    FAIL_TIMEOUT,
    FAIL_REFUSED,
    FAIL_BROWSER,
    FAIL_DNS,
    FAIL_OTHER,
)

# ---------------------------------------------------------------------------
# Распознавание по тексту ошибки
# ---------------------------------------------------------------------------

# Порядок важен: более узкие признаки идут раньше общих.
_PATTERNS = (
    (FAIL_DNS, re.compile(r"ERR_NAME_NOT_RESOLVED|ERR_NAME_RESOLUTION_FAILED"
                          r"|ERR_ICANN_NAME_COLLISION")),
    (FAIL_AUTH_REQUIRED, re.compile(r"ERR_INVALID_AUTH_CREDENTIALS")),
    (FAIL_REDIRECT_LOOP, re.compile(r"ERR_TOO_MANY_REDIRECTS")),
    # Соединение не дошло даже до сервера: его не пропустил прокси.
    # Отдельная группа нужна, чтобы «сайт лежит» и «сайт не разрешён
    # в этом контуре» не смешивались в отчёте.
    (FAIL_PROXY, re.compile(r"ERR_TUNNEL_CONNECTION_FAILED"
                            r"|ERR_PROXY_CONNECTION_FAILED"
                            r"|ERR_PROXY_AUTH_REQUESTED"
                            r"|ERR_PROXY_CERTIFICATE_INVALID"
                            r"|ERR_MANDATORY_PROXY_CONFIGURATION_FAILED")),
    (FAIL_TLS, re.compile(r"ERR_SSL|ERR_CERT|ERR_BAD_SSL|ERR_TLS")),
    (FAIL_TIMEOUT, re.compile(r"ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT"
                              r"|Timeout \d+ms exceeded")),
    # Сервер ответил, но кодом ошибки, — это не отказ соединения.
    (FAIL_HTTP, re.compile(r"ERR_HTTP_RESPONSE_CODE_FAILURE")),
    (FAIL_REFUSED, re.compile(r"ERR_CONNECTION_REFUSED|ERR_CONNECTION_RESET"
                              r"|ERR_CONNECTION_CLOSED|ERR_CONNECTION_ABORTED"
                              r"|ERR_EMPTY_RESPONSE"
                              r"|ERR_SOCKET_NOT_CONNECTED"
                              r"|ERR_ADDRESS_UNREACHABLE"
                              r"|ERR_NETWORK_CHANGED"
                              r"|ERR_INTERNET_DISCONNECTED"
                              r"|ERR_HTTP2_PROTOCOL_ERROR"
                              r"|ERR_QUIC_PROTOCOL_ERROR")),
    (FAIL_INVALID_URL, re.compile(r"Cannot navigate to invalid URL"
                                  r"|Protocol error \(Page\.navigate\)"
                                  r"|invalid URL")),
    # Известный дефект playwright-python 1.40: при разборе ответа на уже
    # освобождённый запрос Response.request падает на сыром словаре
    # инициализатора. Лечится обновлением библиотеки, но встречать его
    # мы должны как транзиентный сбой браузера, а не как «сайт недоступен».
    (FAIL_BROWSER, re.compile(r"'dict' object has no attribute '_object'"
                              r"|Target (page|closed|crashed)"
                              r"|Target closed"
                              r"|browser has been closed"
                              r"|Execution context was destroyed"
                              r"|Page\.content: Unable to retrieve content"
                              # страница ещё коммитит chrome-error от
                              # предыдущей неудачной навигации, когда мы
                              # уже уходим на альтернативный адрес
                              r"|interrupted by another navigation"
                              r"|chrome-error://chromewebdata"
                              # ERR_ABORTED — это НЕ «порт закрыт»:
                              # навигацию прервали (ответ ушёл в загрузку
                              # файла, JS-редирект, наш же about:blank).
                              # В группе «соединение отвергнуто» такой сайт
                              # получал и повторы, и перебор адресов,
                              # и неверный совет «проверьте порт».
                              r"|ERR_ABORTED")),
)

# Отказы, которые повторная попытка в принципе может исправить.
# Всё остальное детерминировано: DNS-записи и мусор в Excel за пять секунд
# не меняются, а три попытки по 45 секунд на каждый такой адрес и съедали
# основную часть времени прогона.
RETRYABLE = frozenset({FAIL_TIMEOUT, FAIL_REFUSED, FAIL_BROWSER, FAIL_PROXY,
                       FAIL_OTHER})

# Отказы, при которых имеет смысл попробовать другой вариант адреса
# (http вместо https, с www или без) — см. modules/url_variants.py.
# FAIL_TIMEOUT здесь потому, что молча дропнутый на файрволе 443-й порт
# выглядит именно как таймаут, а 80-й при этом часто открыт; таймауты —
# вторая по размеру группа отказов в боевом логе.
WORTH_ALTERNATE_URL = frozenset({FAIL_DNS, FAIL_TLS, FAIL_REFUSED,
                                 FAIL_REDIRECT_LOOP, FAIL_TIMEOUT})


def classify_failure(error_text: str) -> str:
    """Определяет вид отказа по тексту ошибки навигации."""
    if not error_text:
        return FAIL_OTHER
    for kind, regex in _PATTERNS:
        if regex.search(error_text):
            return kind
    return FAIL_OTHER


def failure_label(kind: str) -> str:
    """Человекочитаемое описание вида отказа для отчёта."""
    return FAILURE_LABELS.get(kind, FAILURE_LABELS[FAIL_OTHER])


def failure_hint(kind: str) -> str:
    """Подсказка «что с этим делать» для отчёта."""
    return FAILURE_HINTS.get(kind, FAILURE_HINTS[FAIL_OTHER])


def is_retryable(kind: str) -> bool:
    """Стоит ли повторять попытку загрузки при таком отказе."""
    return kind in RETRYABLE


def worth_alternate_url(kind: str) -> bool:
    """Стоит ли пробовать альтернативный вариант адреса при таком отказе."""
    return kind in WORTH_ALTERNATE_URL
