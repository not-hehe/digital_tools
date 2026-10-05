"""Как проверяется домен.

Один HTTP-запрос на цель, пул потоков на список. Наружу отдаёт CheckResult
и правило "живой или нет", а также таксономию отказов: почему ответа
не случилось. Решений о том, кого считать упавшим и кому об этом сообщать,
здесь нет - это следующий слой.
"""

from .checker import (blank_result, check_all, check_domain, is_healthy,
                      reason_of)
from .failures import classify_exception, failure_label

__all__ = ["check_all", "check_domain", "blank_result", "is_healthy",
           "reason_of", "classify_exception", "failure_label"]
