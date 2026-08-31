"""
Варианты адреса: вторая попытка достучаться до сайта другим путём.

Часть «недоступных» сайтов недоступна не сама по себе, а по конкретному
адресу, которым мы к ним ходим. Типовые случаи из боевого лога:

  * сервер держит только устаревший TLS — ``https://`` не открывается,
    ``http://`` открывается;
  * запись DNS есть у ``www.site.ru``, но нет у голого ``site.ru``
    (или наоборот);
  * соединение по 443 закрыто, а 80-й порт отвечает.

Модуль не ходит в сеть — он только строит упорядоченный список
кандидатов. Решение, какой вариант пробовать, принимает page_worker
на основании ВИДА отказа (см. modules/failures.py): гонять все четыре
варианта для каждого сайта было бы вчетверо дороже и без пользы.
"""

import re
from typing import List
from urllib.parse import urlsplit, urlunsplit

from .failures import (FAIL_DNS, FAIL_TLS, FAIL_REFUSED, FAIL_REDIRECT_LOOP,
                       FAIL_TIMEOUT)

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


def _with_scheme(url: str, scheme: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query,
                       parts.fragment))


def _toggle_www(url: str) -> str:
    """https://site.ru → https://www.site.ru и обратно.

    Для IP-адресов и адресов с логином/паролем возвращает исходную строку:
    www.10.0.0.5 не резолвится никогда, а «www.» перед userinfo просто
    ломает адрес. Такой кандидат отсеивается вызывающим кодом как дубль.
    """
    parts = urlsplit(url)
    netloc = parts.netloc
    host = parts.hostname or ""
    if "@" in netloc or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return url
    if netloc.lower().startswith("www."):
        netloc = netloc[4:]
    else:
        netloc = "www." + netloc
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query,
                       parts.fragment))


def alternate_urls(url: str, failure_kind: str) -> List[str]:
    """Кандидаты для повторной попытки после отказа данного вида.

    Возвращает адреса в порядке убывания шанса на успех. Список может быть
    пустым — значит, менять адрес смысла нет (например, сервер ответил,
    но слишком медленно: другой протокол этого не изменит).
    """
    url = normalize(url)
    parts = urlsplit(url)
    candidates: List[str] = []

    if failure_kind == FAIL_DNS:
        # DNS не зависит от протокола: меняем только имя хоста.
        candidates.append(_toggle_www(url))
    elif failure_kind in (FAIL_TLS, FAIL_REFUSED, FAIL_REDIRECT_LOOP):
        # 443 не отвечает или TLS не согласуется — пробуем 80-й порт,
        # затем то же самое для второго варианта имени.
        if parts.scheme == "https":
            candidates.append(_with_scheme(url, "http"))
        candidates.append(_toggle_www(url))
    elif failure_kind == FAIL_TIMEOUT:
        # Таймаут на 443 иногда означает молча дропнутый пакет на файрволе,
        # тогда как 80-й порт открыт.
        if parts.scheme == "https":
            candidates.append(_with_scheme(url, "http"))

    # Убираем повторы и совпадения с исходным адресом, сохраняя порядок.
    seen = {url}
    unique = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique
