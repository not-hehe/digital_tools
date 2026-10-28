"""Нормализация адреса, отсев строк, адресами не являющихся, ключ сайта."""

import re
from urllib.parse import urlsplit

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)

# Строка похожа на адрес, если в ней есть точка и доменная зона: либо
# буквенная (ru, com, рф), либо punycode-представление национальной зоны
# (xn--p1ai). Ячейки вроде «нет», «уточняется», «—» до браузера доходить
# не должны: в прошлом прогоне они честно отрабатывали три попытки
# и ложились в ERROR, раздувая счётчик недоступных сайтов.
_LOOKS_LIKE_HOST_RE = re.compile(
    r"^(?:[\w-]+\.)+(?:xn--[a-z0-9-]+|[A-Za-zА-Яа-я]{2,})\.?$",
    re.UNICODE | re.IGNORECASE)


def normalize(url: str) -> str:
    """Приводит адрес к каноничному виду: добавляет https://, если схемы нет."""
    url = (url or "").strip()
    if not url:
        return url
    if not _SCHEME_RE.match(url):
        url = "https://" + url
    return url


def site_key(url: str) -> str:
    """Ключ сайта для отсева дублей: без схемы, в нижнем регистре, без «/» в конце."""
    return _SCHEME_RE.sub("", normalize(url)).lower().rstrip("/")


def looks_like_url(url: str) -> bool:
    """Похожа ли строка на адрес сайта.

    Проверяется только хост: путь, порт и параметры могут быть любыми.
    ``localhost`` и голые IP-адреса считаются валидными.
    """
    if not url or not url.strip():
        return False
    parts = urlsplit(normalize(url))
    host = parts.hostname or ""
    if not host:
        return False
    if host == "localhost" or re.fullmatch(r"[\d.]+", host):
        return True
    return bool(_LOOKS_LIKE_HOST_RE.match(host))
