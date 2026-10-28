"""Сборка списка целей из колонок таблицы: отсев мусора, дедупликация, колонка ошибок."""

from typing import Dict, Iterable, List, Set, Tuple

from .sheet import COLUMN_ERRORS, contour_column
from ..urls import looks_like_url, normalize, site_key


def clean_urls(raw_urls: Iterable[str], seen: Set[str] = None) -> Tuple[List[str], int]:
    """Отсеивает мусор, нормализует и убирает дубли, порядок сохраняется.

    Возвращает (адреса, число отсеянного мусора); seen — общий набор ключей,
    чтобы вторая колонка не повторяла сайты первой.
    """
    seen = set() if seen is None else seen
    urls, skipped = [], 0
    for raw in raw_urls:
        if not looks_like_url(raw):
            skipped += 1
            continue
        url = normalize(raw.strip())
        key = site_key(url)
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls, skipped


def build_targets(columns: Dict[str, List[str]], contour) -> Tuple[List[str], Set[str], int]:
    """Цели из колонок таблицы: контур плюс «ошибки».

    Сайт и в контуре, и в «ошибках» считается контурным. Возвращает
    (адреса, множество адресов из «ошибок», число отсеянного мусора).
    """
    seen: Set[str] = set()
    urls, skipped = clean_urls(columns[contour_column(contour)], seen)
    error_urls, skipped_errors = clean_urls(columns[COLUMN_ERRORS], seen)
    return urls + error_urls, set(error_urls), skipped + skipped_errors
