"""Как проверяется домен.

Один HTTP-запрос на цель, пул потоков на список. Наружу отдаёт CheckResult
и правило "живой или нет", а также таксономию отказов: почему ответа
не случилось. Решений о том, кого считать упавшим и кому об этом сообщить,
здесь нет - это следующий слой.
"""

from .checker import check_all, reason_of
from .failures import failure_label

__all__ = ["check_all", "reason_of", "failure_label"]
