"""Как проверяется сайт.

Один Chromium на прогон, страница на сайт: сетевые улики, маркеры вёрстки,
зонд контейнера, статус и вид отказа. Решений о том, как слить прогоны
и что писать в отчёт, здесь нет - это следующий слой.
"""

from .browser_manager import BrowserManager
from .page_worker import PageWorker, build_error_result

__all__ = ["BrowserManager", "PageWorker", "build_error_result"]
