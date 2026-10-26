"""Текстовый лог прогонов и событий: что было, когда канал промолчал.

Ни канал, ни файл состояния не отвечают на вопрос "что было вчера". В канал
уходит только то, о чём решено сообщать, а состояние хранит сегодняшний срез
и перезаписывается каждым прогоном. Здесь остаётся и то, и другое: строка
на каждый боевой прогон (по ним видно, все ли 288 прогонов в сутки состоялись,
а логов ВМ у нас нет) и строка на каждое событие смены статуса.

Формат - обычный текст, по строке на запись, читается глазами и grep'ом:

    2026-10-02 13:35:12  прогон    проверено 126, не ответили 2: old.example.com (TLS...), shop.example.com/ (HTTP 404)
    2026-10-02 13:35:12  упал      old.example.com - TLS-рукопожатие не состоялось (...)
    2026-10-02 13:35:12  поднялся  shop.example.com/ - не отвечал с 2026-09-21 12:05

Запись сюда никогда не роняет прогон: проверка доменов важнее истории о них.
Любая ошибка файла только пишется в лог, и прогон продолжается.
"""

import logging
from typing import List

from ..config import RUNLOG_FILE, RUNLOG_MAX_LINES, now_msk, short_stamp
from ..probe import reason_of
from .state import EVENT_DOWN

logger = logging.getLogger(__name__)

KIND_RUN = "прогон"
KIND_DOWN = "упал"
KIND_UP = "поднялся"
KIND_REVIVED = "ожил"


def _stamp() -> str:
    return now_msk().strftime("%Y-%m-%d %H:%M:%S")


def _line(kind: str, body: str) -> str:
    # Ширина колонки под самое длинное слово "поднялся": колонки ровные,
    # и grep по виду записи не цепляет текст справа.
    return "{}  {:<8}  {}".format(_stamp(), kind, body)


def run_line(results: List[dict]) -> str:
    """Строка о прогоне: сколько проверено и кто именно не ответил.

    Имена попадают сюда независимо от того, ушло ли сообщение: одиночный
    отказ, не дошедший до порога подтверждения, виден только здесь.
    """
    failing = [r for r in results if not r.get("ok")]
    # Колонка ошибок лежит каждый прогон: её имена раздули бы каждую строку
    # на сотню адресов, а журнал обрезается по числу строк, не по размеру.
    named = [r for r in failing if not r.get("known_bad")]
    body = "проверено {}, не ответили {}".format(len(results), len(failing))
    if len(named) < len(failing):
        body += " (из списка ошибок {})".format(len(failing) - len(named))
    if named:
        body += ": " + ", ".join(
            "{} ({})".format(r.get("label") or r.get("url"), reason_of(r))
            for r in named)
    return _line(KIND_RUN, body)


def event_lines(events: List[dict]) -> List[str]:
    lines = []
    for e in events:
        if e.get("event") == EVENT_DOWN:
            lines.append(_line(KIND_DOWN, "{} - {}".format(
                e.get("label"), e.get("reason") or "причина не определена")))
        elif e.get("revived"):
            lines.append(_line(KIND_REVIVED, "{} (из списка ошибок)".format(
                e.get("label"))))
        else:
            lines.append(_line(KIND_UP, "{} - не отвечал с {}".format(
                e.get("label"), short_stamp(e.get("since")) or "неизвестно")))
    return lines


def append(lines: List[str], path=None, max_lines: int = None) -> bool:
    """Дописывает строки и подрезает файл. False - записать не удалось."""
    path = RUNLOG_FILE if path is None else path
    max_lines = RUNLOG_MAX_LINES if max_lines is None else max_lines
    if not lines:
        return True
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
    except OSError as e:
        logger.error("Лог прогонов не записан (%s): %s", path, e)
        return False
    _trim(path, max_lines)
    return True


def _trim(path, max_lines: int) -> None:
    if max_lines <= 0:
        return
    slack = max(1, max_lines // 10)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= max_lines + slack:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])
    except OSError as e:
        logger.error("Лог прогонов не подрезан (%s): %s", path, e)
