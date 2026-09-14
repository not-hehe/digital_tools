"""Откуда берётся список доменов.

Читает Excel, отсеивает мусор, схлопывает дубли, раздаёт метки контуров.
Наружу отдаёт LoadResult - словарь с целями, отсеянными строками и счётчиками
по контурам. Что внутри Excel и как он устроен, за пределы слоя не выходит.
"""

from .targets import (CONTOUR_EXTERNAL, CONTOUR_INTERNAL, CONTOUR_UNKNOWN,
                      EmptyTargetList, count_by_contour, load_from_excel,
                      load_targets, normalize_contour)

__all__ = ["load_targets", "load_from_excel", "EmptyTargetList",
           "count_by_contour", "normalize_contour",
           "CONTOUR_INTERNAL", "CONTOUR_EXTERNAL", "CONTOUR_UNKNOWN"]
