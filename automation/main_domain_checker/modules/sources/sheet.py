"""Список адресов из Google-таблицы, открытой по ссылке на просмотр.

Таблица скачивается CSV-выгрузкой без ключей и сторонних библиотек. Один лист,
три колонки: ошибки, внутренний контур, внешний контур; колонка находится
по началу заголовка, так что "Внешний контур" тоже подходит.

Любая неудача чтения - SheetUnavailable с причиной простыми словами: текст
уходит в канал как есть, и читать его будут не только те, кто знает HTTP.
"""

import csv
import io
import re
import socket
import urllib.error
import urllib.request
from typing import Dict, List

COLUMN_ERRORS = "errors"
COLUMN_INTERNAL = "internal"
COLUMN_EXTERNAL = "external"

COLUMN_NAMES = {COLUMN_ERRORS: "ошибки", COLUMN_INTERNAL: "внутренний",
                COLUMN_EXTERNAL: "внешний"}
HEADER_PREFIXES = {"ошиб": COLUMN_ERRORS, "внутр": COLUMN_INTERNAL,
                   "внеш": COLUMN_EXTERNAL}

CONTOURS = {1: COLUMN_EXTERNAL, 2: COLUMN_INTERNAL}

SHEET_TIMEOUT = 30

_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_GID_RE = re.compile(r"[?#&]gid=(\d+)")


class SheetUnavailable(Exception):
    """Таблицу не удалось прочитать; str() - причина для сообщения в канал."""


def contour_column(value) -> str:
    """Колонка таблицы по переключателю: 1 - внешний контур, 2 - внутренний."""
    try:
        return CONTOURS[int(str(value).strip())]
    except (KeyError, ValueError):
        raise ValueError("CONTOUR должен быть 1 (внешний) или 2 (внутренний), "
                         "задано: {!r}".format(value))


def export_url(sheet_url: str) -> str:
    """Ссылка из адресной строки браузера -> адрес CSV-выгрузки того же листа."""
    match = _ID_RE.search(sheet_url or "")
    if not match:
        raise SheetUnavailable("ссылка не похожа на Google-таблицу: "
                               "нужен адрес вида docs.google.com/spreadsheets/d/...")
    gid = _GID_RE.search(sheet_url)
    return ("https://docs.google.com/spreadsheets/d/{}/export?format=csv&gid={}"
            .format(match.group(1), gid.group(1) if gid else "0"))


def fetch_csv(sheet_url: str, timeout: int = SHEET_TIMEOUT, opener=None) -> str:
    """Скачивает лист. Редирект на googleusercontent.com - штатная часть выгрузки."""
    opener = urllib.request.urlopen if opener is None else opener
    request = urllib.request.Request(export_url(sheet_url),
                                     headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener(request, timeout=timeout) as response:
            # Закрытая таблица отвечает не ошибкой, а страницей входа в аккаунт.
            if response.headers.get_content_type() != "text/csv":
                raise SheetUnavailable(_CLOSED)
            body = response.read()
    except SheetUnavailable:
        raise
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SheetUnavailable("таблица не найдена (404) - проверьте ссылку")
        if e.code in (401, 403):
            raise SheetUnavailable(_CLOSED)
        raise SheetUnavailable("Google ответил ошибкой {}".format(e.code))
    except (socket.timeout, TimeoutError):
        raise SheetUnavailable(_TIMEOUT.format(timeout))
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise SheetUnavailable(_TIMEOUT.format(timeout))
        raise SheetUnavailable("нет связи с docs.google.com: {}".format(e.reason))
    except OSError as e:
        raise SheetUnavailable("нет связи с docs.google.com: {}".format(e))

    text = body.decode("utf-8-sig", errors="replace")
    if not text.strip():
        raise SheetUnavailable("таблица пуста")
    return text


_CLOSED = ('таблица закрыта - откройте доступ "все, у кого есть ссылка" '
           "на просмотр")
_TIMEOUT = "Google не ответил за {} секунд"


def parse_columns(text: str) -> Dict[str, List[str]]:
    """Три колонки по заголовкам первой строки, пустые ячейки пропущены."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SheetUnavailable("таблица пуста")
    index = {}
    for i, cell in enumerate(rows[0]):
        head = (cell or "").strip().lower()
        for prefix, column in HEADER_PREFIXES.items():
            if head.startswith(prefix) and column not in index:
                index[column] = i
    for column in (COLUMN_ERRORS, COLUMN_INTERNAL, COLUMN_EXTERNAL):
        if column not in index:
            raise SheetUnavailable('в таблице нет колонки "{}" (первая строка - '
                                   'заголовки)'.format(COLUMN_NAMES[column]))
    columns = {column: [] for column in index}
    for row in rows[1:]:
        for column, i in index.items():
            if i < len(row) and row[i].strip():
                columns[column].append(row[i].strip())
    return columns


def read_sheet(sheet_url: str, opener=None) -> Dict[str, List[str]]:
    return parse_columns(fetch_csv(sheet_url, opener=opener))
