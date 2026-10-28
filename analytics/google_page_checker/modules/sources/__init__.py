"""Откуда берётся список сайтов.

Читает Google-таблицу CSV-выгрузкой, отсеивает мусор, схлопывает дубли,
отделяет колонку ошибок. Что внутри таблицы и как она устроена, за пределы
слоя не выходит.
"""

from .sheet import COLUMN_NAMES, SheetUnavailable, contour_column, read_sheet
from .targets import build_targets, clean_urls

__all__ = ["read_sheet", "contour_column", "COLUMN_NAMES", "SheetUnavailable",
           "build_targets", "clean_urls"]
