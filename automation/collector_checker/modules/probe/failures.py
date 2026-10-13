"""Вид отказа по исключению requests, только для журнала."""

import requests

FAIL_DNS = "dns"
FAIL_TLS = "tls"
FAIL_TIMEOUT = "timeout"
FAIL_CONNECTION = "connection"
FAIL_OTHER = "other"

# Текст DNS-ошибки разный на Linux и macOS.
_DNS_MARKERS = (
    "name or service not known",
    "nodename nor servname",
    "getaddrinfo failed",
    "temporary failure in name resolution",
    "no address associated with hostname",
)


def classify_exception(exc: BaseException) -> str:
    # SSLError и ConnectTimeout наследуются от ConnectionError, он проверяется последним.
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
