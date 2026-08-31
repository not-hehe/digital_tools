# Модуль перенесён из analytics/google_page_checker как есть. Там он работает
# на 3.10+, где `str | Path` в аннотации вычисляется штатно; целевой рантайм
# здесь 3.9 (R-3), и без отложенных аннотаций импорт падает на TypeError.
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Generator, List

import pandas as pd

logger = logging.getLogger(__name__)


def _is_empty(value) -> bool:
    """Пустая ячейка: NaN/None или строка из одних пробелов."""
    return pd.isna(value) or not str(value).strip()

# Доменная зона — либо буквенная (ru, com, рф), либо punycode (xn--p1ai).
# Без варианта xn-- живой адрес https://xn--80aswg.xn--p1ai обрезался до
# «https://xn--80aswg.xn» и стабильно уходил в ERROR как несуществующий.
# Хвост начинается с /, ? или #, иначе у адресов без пути терялся query.
_URL_TOKEN_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)+(?:xn--[a-z0-9-]+|[A-Za-zА-Яа-я]{2,})"
    r"(?::\d+)?(?:[/?#]\S*)?",
    re.IGNORECASE,
)


def _extract_urls(raw) -> List[str]:
    """
    Если ни одного URL-токена не нашлось (в ячейке нет ни одного слова
    с доменом), возвращаем содержимое одной строкой БЕЗ пробелов —
    такая запись честно дойдёт до браузера и попадёт в секцию ERROR,
    а не потеряется молча.
    """
    text = str(raw).strip()
    found = [m.group(0).rstrip(",;.") for m in _URL_TOKEN_RE.finditer(text)]
    if found:
        return found
    collapsed = "".join(text.split())
    return [collapsed] if collapsed else []


_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


def _dedup_key(url: str) -> str:
    """Ключ дедупликации: один сайт — один ключ.
    """
    return _SCHEME_RE.sub("", url).lower()


def read_urls_from_excel(
    file_path: str | Path,
    column_name: str = "url",
    sheet_name: int | str | None = None,
    skip_empty: bool = True,
    unique: bool = True,
    empty_row_break: int = 25,
) -> Generator[str, None, None]:

    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Excel‑файл не найден: {file_path}")

    logger.info(
        "Чтение URL из файла: %s (листы: %s, колонка: %s)",
        file_path, "все" if sheet_name is None else sheet_name, column_name,
    )

    sheets = pd.read_excel(file_path, sheet_name=sheet_name, engine="openpyxl")
    if not isinstance(sheets, dict):
        sheets = {sheet_name: sheets}

    # Колонка должна найтись хотя бы на одном листе — иначе это не тот файл.
    sheets_with_column = {name: df for name, df in sheets.items()
                          if column_name in df.columns}
    if not sheets_with_column:
        available = ", ".join(
            f"{name}: [{', '.join(map(str, df.columns))}]"
            for name, df in sheets.items())
        raise ValueError(
            f"Колонка '{column_name}' не найдена ни на одном листе. "
            f"Доступные колонки: {available}")

    seen = set()
    total = 0
    for name, df in sheets.items():
        if name not in sheets_with_column:
            logger.warning("Лист '%s': колонки '%s' нет — лист пропущен",
                           name, column_name)
            continue

        sheet_count = 0
        empty_streak = 0
        stopped_by_gap = False
        for value in df[column_name]:
            if _is_empty(value):
                empty_streak += 1
                if empty_streak >= empty_row_break:
                    # Длинный разрыв — данные листа закончились,
                    # переходим к следующему листу.
                    stopped_by_gap = True
                    break
                if skip_empty:
                    continue
                candidates = [""]
            else:
                empty_streak = 0
                # Ячейка может содержать несколько URL и лишние пробелы —
                # разбираем её на чистые адреса.
                candidates = _extract_urls(value)

            for url in candidates:
                if not url and skip_empty:
                    continue
                if unique:
                    # сравниваем без схемы и регистра:
                    # https://a.ru, a.ru и A.RU — один сайт
                    key = _dedup_key(url)
                    if key in seen:
                        continue
                    seen.add(key)
                sheet_count += 1
                total += 1
                yield url

        logger.info(
            "Лист '%s': прочитано URL: %d (%s)", name, sheet_count,
            f"остановлено разрывом {empty_row_break}+ пустых строк"
            if stopped_by_gap else "дочитан до конца",
        )

    logger.info("Завершено чтение URL со всех листов. Всего: %d", total)
