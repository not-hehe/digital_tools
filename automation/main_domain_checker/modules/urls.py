"""Нормализация адреса и отсев строк, адресами не являющихся."""

import re
from urllib.parse import urlsplit

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Доменная зона — буквенная (ru, com, рф) либо punycode (xn--p1ai). Ячейки
# вроде «нет» и «уточняется» до сети доходить не должны: они дают отказ,
# неотличимый в алерте от настоящей недоступности.
_LOOKS_LIKE_HOST_RE = re.compile(
    r"^(?:[\w-]+\.)+(?:xn--[a-z0-9-]+|[A-Za-zА-Яа-я]{2,})\.?$",
    re.UNICODE | re.IGNORECASE)


def normalize(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if not _SCHEME_RE.match(url):
        url = "https://" + url
    return url


def looks_like_url(url: str) -> bool:
    if not url or not url.strip():
        return False
    host = urlsplit(normalize(url)).hostname or ""
    if not host:
        return False
    if host == "localhost" or re.fullmatch(r"[\d.]+", host):
        return True
    return bool(_LOOKS_LIKE_HOST_RE.match(host))


def label_of(url: str) -> str:
    """Адрес без схемы — то, что видит человек в сообщении."""
    return _SCHEME_RE.sub("", normalize(url))
