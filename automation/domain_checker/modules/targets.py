"""Загрузка списка доменов. Источник сменный, форма результата одна.

Сейчас источник один — Excel. Загрузчик всё равно спрятан за интерфейсом:
добавить другой источник можно, не трогая ни проверки, ни отчёт.
"""

import logging
import re
from typing import Iterable, List, Optional

from .config import (SOURCE_FILE, TARGET_SOURCE, INPUT_FILE,
                     EXCEL_COLUMN_NAME, EXCEL_SHEET_NAME,
                     EXCEL_EMPTY_ROW_BREAK)
from .excel_reader import read_urls_from_excel
from .urls import label_of, looks_like_url, normalize

logger = logging.getLogger(__name__)

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def _dedup_key(url: str) -> str:
    return _SCHEME_RE.sub("", url).lower().rstrip("/")


def _build_targets(raw_values: Iterable[str], source: str) -> dict:
    """Общая для всех источников сборка LoadResult.

    Отсев и дедупликация живут здесь, а не в загрузчиках: иначе список из
    Prometheus и список из Excel обрабатывались бы по-разному, и расхождение
    вылезло бы в день переключения источника.
    """
    targets: List[dict] = []
    skipped: List[str] = []
    seen = set()

    for raw in raw_values:
        url = normalize(raw)
        if not looks_like_url(url):
            skipped.append(str(raw))
            continue
        key = _dedup_key(url)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"url": url, "label": label_of(url)})

    logger.info("Источник %s: целей %d, отсеяно %d", source, len(targets),
                len(skipped))
    return {"targets": targets, "skipped": skipped, "source": source}


def load_from_excel(path=None, column_name: str = None, sheet_name=None,
                    empty_row_break: int = None) -> dict:
    path = INPUT_FILE if path is None else path
    column_name = EXCEL_COLUMN_NAME if column_name is None else column_name
    sheet_name = EXCEL_SHEET_NAME if sheet_name is None else sheet_name
    if empty_row_break is None:
        empty_row_break = EXCEL_EMPTY_ROW_BREAK
    rows = read_urls_from_excel(path, column_name=column_name,
                                sheet_name=sheet_name,
                                empty_row_break=empty_row_break)
    return _build_targets(rows, SOURCE_FILE)


def load_targets(source: str = None, limit: Optional[int] = None) -> dict:
    """Единая точка входа: LoadResult по выбранному источнику.

    limit обрезает список ПОСЛЕ дедупликации — иначе `--limit 5` на списке
    с повторами давал бы разное число целей от запуска к запуску.
    """
    source = TARGET_SOURCE if source is None else source
    if source != SOURCE_FILE:
        raise ValueError("неизвестный источник списка: {!r} "
                         "(доступен только {!r})".format(source, SOURCE_FILE))
    result = load_from_excel()
    if limit:
        result["targets"] = result["targets"][:limit]
    return result
