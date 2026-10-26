"""Откуда берётся список доменов.

Читает Google-таблицу, отсеивает мусор, схлопывает дубли, раздаёт метки
контуров и признак "из колонки ошибок". Наружу отдаёт LoadResult - словарь
с целями, отсеянными строками и счётчиками по контурам. Что внутри таблицы
и как она устроена, за пределы слоя не выходит.
"""

from .sheet import SheetUnavailable, contour_column
from .targets import load_targets

__all__ = ["load_targets", "SheetUnavailable", "contour_column"]
