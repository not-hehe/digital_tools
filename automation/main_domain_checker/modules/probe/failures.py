"""Виды отказов и распознавание их по исключениям requests.

Состав видов взят из analytics/google_page_checker, распознавание написано
заново: там вид определялся регулярками по тексту ошибки Chromium
(`net::ERR_NAME_NOT_RESOLVED`), а requests таких строк не выдаёт.
"""

import requests

FAIL_DNS = "dns"
FAIL_TLS = "tls"
FAIL_TIMEOUT = "timeout"
FAIL_REFUSED = "refused"
FAIL_REDIRECT_LOOP = "redirect_loop"
FAIL_PROXY = "proxy"
FAIL_INVALID_URL = "invalid_url"
FAIL_OTHER = "other"

FAILURE_LABELS = {
    FAIL_DNS: "домен не резолвится (нет записи DNS или другой контур)",
    FAIL_TLS: "TLS-рукопожатие не состоялось (устаревший протокол или шифр)",
    FAIL_TIMEOUT: "сервер не ответил за отведённое время",
    FAIL_REFUSED: "соединение отвергнуто или оборвано",
    FAIL_REDIRECT_LOOP: "бесконечная цепочка редиректов",
    FAIL_PROXY: "прокси не пропустил соединение",
    FAIL_INVALID_URL: "строка не является адресом (мусор во входных данных)",
    FAIL_OTHER: "прочая ошибка загрузки",
}

# socket.gaierror формулируется по-разному в Linux и macOS, а до текста
# исключения requests доносит его целиком.
_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated with hostname",
)


def classify_exception(exc: BaseException) -> str:
    """Вид отказа по исключению requests.

    Порядок проверок задан иерархией классов: ProxyError и SSLError —
    подклассы ConnectionError, а ConnectTimeout — подкласс сразу обоих
    (ConnectionError и Timeout). Проверка ConnectionError первой схлопнула бы
    в «соединение отвергнуто» и таймауты, и отказы TLS.
    """
    if isinstance(exc, requests.exceptions.ProxyError):
        return FAIL_PROXY
    if isinstance(exc, requests.exceptions.SSLError):
        return FAIL_TLS
    if isinstance(exc, requests.exceptions.Timeout):
        return FAIL_TIMEOUT
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return FAIL_REDIRECT_LOOP
    if isinstance(exc, (requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema,
                        requests.exceptions.InvalidURL,
                        requests.exceptions.URLRequired)):
        return FAIL_INVALID_URL
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return FAIL_DNS
        return FAIL_REFUSED
    return FAIL_OTHER


def failure_label(kind: str) -> str:
    return FAILURE_LABELS.get(kind, FAILURE_LABELS[FAIL_OTHER])
