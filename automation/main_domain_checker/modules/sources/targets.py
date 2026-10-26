"""Сборка целей из Google-таблицы: отсев мусора, дедупликация, колонка ошибок.

Чтение отделено от сборки целей: что внутри таблицы и как она скачивается,
за пределы `sheet.py` не выходит.
"""

import logging
from typing import Dict, Iterable, List, Optional, Tuple

from ..config import CONTOUR, SHEET_URL
from .sheet import (COLUMN_ERRORS, COLUMN_EXTERNAL, COLUMN_INTERNAL,
                    COLUMN_NAMES, SheetUnavailable, contour_column, read_sheet)
from ..urls import label_of, looks_like_url, normalize

logger = logging.getLogger(__name__)


def _dedup_key(url: str) -> str:
    """Один сайт - один ключ: без схемы, регистра и хвостового слэша.

    Схему снимает label_of, чтобы правило "как выглядит адрес без протокола"
    жило в одном месте - в modules/urls.py.
    """
    return label_of(url).lower().rstrip("/")


def _build_targets(rows: Iterable[Tuple[str, str, bool]]) -> dict:
    """Сборка LoadResult: отсев мусора, дедупликация, метки контуров.

    Отсев и дедупликация живут здесь, а не в читалке: место, где домен может
    пропасть, должно быть одно, иначе искать пропажу придётся в двух.
    """
    targets: List[dict] = []
    skipped: List[str] = []
    seen = set()

    for raw_url, contour, known_bad in rows:
        url = normalize(raw_url)
        if not looks_like_url(url):
            skipped.append(str(raw_url))
            continue
        key = _dedup_key(url)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"url": url, "label": label_of(url),
                        "contour": contour, "known_bad": known_bad})

    counts = count_by_contour(targets)
    known_bad_count = sum(1 for t in targets if t["known_bad"])
    logger.info("Целей %d (внутренних %d, внешних %d, из списка ошибок %d), "
                "отсеяно %d", len(targets), counts[COLUMN_INTERNAL],
                counts[COLUMN_EXTERNAL], known_bad_count, len(skipped))
    return {"targets": targets, "skipped": skipped, "counts": counts,
            "known_bad": known_bad_count}


def count_by_contour(targets: List[dict]) -> Dict[str, int]:
    """Сайты из колонки ошибок не считаются: предохранитель по контуру
    сравнивает упавших с контуром, а они лежат заранее и событий не дают."""
    counts = {COLUMN_INTERNAL: 0, COLUMN_EXTERNAL: 0}
    for t in targets:
        if not t.get("known_bad"):
            counts[t["contour"]] += 1
    return counts


def load_from_sheet(url: str = None, contour=None, opener=None) -> dict:
    """Колонка выбранного контура и колонка ошибок.

    Контур идёт первым: сайт, стоящий в обеих колонках, дедупликация оставит
    контурным, и о его падениях канал узнает как обычно.
    """
    column = contour_column(CONTOUR if contour is None else contour)
    url = SHEET_URL if url is None else url
    if not url:
        raise SheetUnavailable("ссылка на таблицу не задана (DC_SHEET_URL в .env)")
    columns = read_sheet(url, opener=opener)
    rows = ([(u, column, False) for u in columns[column]]
            + [(u, column, True) for u in columns[COLUMN_ERRORS]])
    result = _build_targets(rows)
    # Пустой контур - сбой таблицы, а не пустой список: так о нём сообщат
    # один раз, а не каждые пять минут сообщением о непрошедшем прогоне.
    if not any(not t["known_bad"] for t in result["targets"]):
        raise SheetUnavailable('в колонке "{}" нет ни одного адреса'.format(
            COLUMN_NAMES[column]))
    return result


def load_targets(limit: Optional[int] = None) -> dict:
    """Единая точка входа: LoadResult из таблицы.

    limit обрезает список ПОСЛЕ дедупликации — иначе `--limit 5` на списке
    с повторами давал бы разное число целей от запуска к запуску.
    """
    result = load_from_sheet()
    if limit:
        result["targets"] = result["targets"][:limit]
        # Счётчики пересчитываем после обрезки: сводка должна описывать то,
        # что реально проверено, а не то, что лежало в таблице.
        result["counts"] = count_by_contour(result["targets"])
        result["known_bad"] = sum(1 for t in result["targets"] if t["known_bad"])
    return result
