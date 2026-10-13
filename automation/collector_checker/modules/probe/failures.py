"""Вид отказа по исключению requests. Нужен только журналу: в алерте причины нет."""

import requests

FAIL_DNS = "dns"
FAIL_TLS = "tls"
FAIL_TIMEOUT = "timeout"
FAIL_CONNECTION = "connection"
FAIL_OTHER = "other"

# socket.gaierror формулируется по-разному в Linux и macOS, requests доносит
# текст целиком.
_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated with hostname",
)


def classify_exception(exc: BaseException) -> str:
    # SSLError и ConnectTimeout наследуются от ConnectionError, поэтому
    # ConnectionError проверяется последним.
    if isinstance(exc, requests.exceptions.SSLError):
        return FAIL_TLS
    if isinstance(exc, requests.exceptions.Timeout):
        return FAIL_TIMEOUT
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if any(marker in text for marker in _DNS_MARKERS):
            return FAIL_DNS
        return FAIL_CONNECTION
    return FAIL_OTHER
