"""Загрузка списка доменов из Excel: отсев мусора, дедупликация, контуры.

Файл остаётся источником правды по списку. Чтение отделено от сборки целей,
так что заменить Excel на другой формат можно, не трогая ни проверки,
ни отчёт.
"""

import logging
from typing import Dict, Iterable, List, Optional

from ..config import (INPUT_FILE, EXCEL_COLUMN_NAME, EXCEL_SHEET_NAME,
                     EXCEL_EMPTY_ROW_BREAK, EXCEL_CONTOUR_COLUMN, SHEET_URL,
                     CONTOUR)
from .excel_reader import read_urls_from_excel
from .sheet import (COLUMN_ERRORS, COLUMN_NAMES, SheetUnavailable,
                    contour_column, read_sheet)
from ..urls import label_of, looks_like_url, normalize

logger = logging.getLogger(__name__)

CONTOUR_INTERNAL = "internal"
CONTOUR_EXTERNAL = "external"
CONTOUR_UNKNOWN = "unknown"

# Что люди пишут в колонке contour на практике. Всё нераспознанное считается
# неизвестным: молча приписать домен не тому контуру хуже, чем признать,
# что мы не знаем.
_CONTOUR_ALIASES = {
    "internal": CONTOUR_INTERNAL, "int": CONTOUR_INTERNAL,
    "in": CONTOUR_INTERNAL, "внутренний": CONTOUR_INTERNAL,
    "внутр": CONTOUR_INTERNAL, "вн": CONTOUR_INTERNAL,
    "external": CONTOUR_EXTERNAL, "ext": CONTOUR_EXTERNAL,
    "out": CONTOUR_EXTERNAL, "внешний": CONTOUR_EXTERNAL,
    "внеш": CONTOUR_EXTERNAL,
}


def normalize_contour(value) -> str:
    if not value:
        return CONTOUR_UNKNOWN
    return _CONTOUR_ALIASES.get(str(value).strip().lower(), CONTOUR_UNKNOWN)


def _dedup_key(url: str) -> str:
    """Один сайт - один ключ: без схемы, регистра и хвостового слэша.

    Схему снимает label_of, чтобы правило "как выглядит адрес без протокола"
    жило в одном месте - в modules/urls.py.
    """
    return label_of(url).lower().rstrip("/")


def _build_targets(raw_values: Iterable[str]) -> dict:
    """Сборка LoadResult: отсев мусора, дедупликация, метки контуров.

    Отсев и дедупликация живут здесь, а не в читалке: место, где домен может
    пропасть, должно быть одно, иначе искать пропажу придётся в двух.
    """
    targets: List[dict] = []
    skipped: List[str] = []
    seen = set()

    for raw in raw_values:
        # Читалка отдаёт адрес, пару с меткой контура или тройку с признаком
        # "из колонки ошибок".
        if not isinstance(raw, tuple):
            raw = (raw,)
        raw_url, raw_contour, known_bad = (raw + (None, False))[:3]
        url = normalize(raw_url)
        if not looks_like_url(url):
            skipped.append(str(raw_url))
            continue
        key = _dedup_key(url)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"url": url, "label": label_of(url),
                        "contour": normalize_contour(raw_contour),
                        "known_bad": bool(known_bad)})

    counts = count_by_contour(targets)
    known_bad_count = sum(1 for t in targets if t["known_bad"])
    logger.info("Целей %d (внутренних %d, внешних %d, без метки %d, "
                "из списка ошибок %d), отсеяно %d", len(targets),
                counts[CONTOUR_INTERNAL], counts[CONTOUR_EXTERNAL],
                counts[CONTOUR_UNKNOWN], known_bad_count, len(skipped))
    # Без единой метки предохранитель по контуру не срабатывает никогда:
    # отказ маршрута внутрь кладёт четверть списка и общий порог его пропустит.
    # Молча жить с выключенным предохранителем хуже, чем без него.
    if targets and counts[CONTOUR_UNKNOWN] == len(targets):
        logger.warning("Ни одна цель не размечена по контурам (колонка %r): "
                       "предохранитель по контуру выключен",
                       EXCEL_CONTOUR_COLUMN)
    return {"targets": targets, "skipped": skipped, "counts": counts,
            "known_bad": known_bad_count}


def count_by_contour(targets: List[dict]) -> Dict[str, int]:
    """Сайты из колонки ошибок не считаются: предохранитель по контуру
    сравнивает упавших с контуром, а они лежат заранее и событий не дают."""
    counts = {CONTOUR_INTERNAL: 0, CONTOUR_EXTERNAL: 0, CONTOUR_UNKNOWN: 0}
    for t in targets:
        if not t.get("known_bad"):
            counts[t.get("contour") or CONTOUR_UNKNOWN] += 1
    return counts


def load_from_excel(path=None, column_name: str = None, sheet_name=None,
                    empty_row_break: int = None) -> dict:
    path = INPUT_FILE if path is None else path
    column_name = EXCEL_COLUMN_NAME if column_name is None else column_name
    sheet_name = EXCEL_SHEET_NAME if sheet_name is None else sheet_name
    if empty_row_break is None:
        empty_row_break = EXCEL_EMPTY_ROW_BREAK
    rows = read_urls_from_excel(path, column_name=column_name,
                                sheet_name=sheet_name,
                                empty_row_break=empty_row_break,
                                extra_column=EXCEL_CONTOUR_COLUMN)
    result = _build_targets(rows)
    result["source"] = "excel"
    return result


def load_from_sheet(url: str = None, contour=None, opener=None) -> dict:
    """Колонка выбранного контура и колонка ошибок.

    Контур идёт первым: сайт, стоящий в обеих колонках, дедупликация оставит
    контурным, и о его падениях канал узнает как обычно.
    """
    column = contour_column(CONTOUR if contour is None else contour)
    columns = read_sheet(SHEET_URL if url is None else url, opener=opener)
    rows = ([(u, column) for u in columns[column]]
            + [(u, column, True) for u in columns[COLUMN_ERRORS]])
    result = _build_targets(rows)
    # Пустой контур - сбой таблицы, а не пустой список: так о нём сообщат
    # один раз, а не каждые пять минут сообщением о непрошедшем прогоне.
    if not any(not t["known_bad"] for t in result["targets"]):
        raise SheetUnavailable('в колонке "{}" нет ни одного адреса'.format(
            COLUMN_NAMES[column]))
    result["source"] = "sheet"
    return result


class EmptyTargetList(Exception):
    """Список отдал ноль целей.

    Отдельный тип, чтобы точка входа могла отличить это от любой другой
    ошибки чтения: проверять нечего - не то же самое, что проверено и всё
    хорошо, а обходится это разными способами.
    """


def load_targets(limit: Optional[int] = None, path=None) -> dict:
    """Единая точка входа: LoadResult из Excel.

    limit обрезает список ПОСЛЕ дедупликации — иначе `--limit 5` на списке
    с повторами давал бы разное число целей от запуска к запуску.

    Пустой список это отказ, а не повод для тихого прогона: проверять было бы
    нечего, событий не возникло бы, канал промолчал бы, а состояние при этом
    обнулилось бы целиком и память обо всех упавших доменах пропала. Молчание
    в таком случае неотличимо от исправной работы, поэтому здесь исключение.
    """
    if path is None and SHEET_URL:
        result = load_from_sheet()
    else:
        result = load_from_excel(path=path)
    if not result["targets"]:
        raise EmptyTargetList(
            "список целей пуст (отсеяно строк: {}). Проверять нечего, "
            "прогон прекращён".format(len(result["skipped"])))
    if limit:
        result["targets"] = result["targets"][:limit]
        # Счётчики пересчитываем после обрезки: сводка должна описывать то,
        # что реально проверено, а не то, что лежало в файле.
        result["counts"] = count_by_contour(result["targets"])
    return result
