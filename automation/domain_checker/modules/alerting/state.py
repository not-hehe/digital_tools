"""Память о доменах между запусками.

Нужна ради двух вещей, без которых чекер непригоден при цикле в пять минут:
падение подтверждается двумя неудачными прогонами подряд, а сообщение уходит
только в момент смены состояния.

Хранилище это файл JSON рядом с проектом, путь можно сменить переменной
DC_STATE_FILE. У выбора есть цена: если файл теряется между запусками,
счётчик отказов каждый раз начинается с нуля и до порога не доходит никогда -
чекер замолкает совсем, и это неотличимо от исправной работы. Поэтому запись
и чтение разведены: нечитаемый файл прогон переживает, несохранённый - роняет.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from ..probe import reason_of
from ..config import (CONFIRM_FAILS, HEARTBEAT_EVERY_HOURS, STATE_FILE,
                      now_msk)

logger = logging.getLogger(__name__)

STATE_VERSION = 1

EVENT_DOWN = "down"
EVENT_UP = "up"


def _stamp() -> str:
    """Отметка времени для хранения: строкой, чтобы пережить JSON."""
    return now_msk().isoformat(timespec="seconds")


def empty_state() -> dict:
    return {"version": STATE_VERSION, "updated_at": None, "domains": {}}


def load_state(path=None) -> dict:
    """Читает состояние. Любая проблема с файлом даёт пустое состояние.

    Прогон не должен падать из-за хранилища: проверка доменов важнее памяти
    о них. Но отсутствие файла логируется предупреждением, и это единственный
    внешний признак того, что состояние не переживает запуски. Если такое
    предупреждение появляется в каждом прогоне, хранилище не работает.
    """
    path = STATE_FILE if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        logger.warning("Состояние не найдено (%s): считаем, что все домены "
                       "были живы. В первом запуске это норма, в остальных "
                       "признак того, что файл не сохраняется", path)
        return empty_state()
    except (OSError, ValueError) as e:
        logger.error("Состояние не прочитано (%s): %s. Считаем, что все "
                     "домены были живы", path, e)
        return empty_state()

    if not isinstance(state, dict) or "domains" not in state:
        logger.error("Состояние %s непригодно, начинаем с пустого", path)
        return empty_state()
    return state


def save_state(state: dict, path=None) -> bool:
    """Пишет состояние через временный файл.

    Прямая запись поверх оставила бы обрезанный JSON, если процесс умрёт
    посередине, и следующий прогон начал бы с пустого состояния.
    """
    path = STATE_FILE if path is None else path
    tmp = str(path) + ".tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        import os
        os.replace(tmp, path)
        return True
    except OSError as e:
        logger.error("Состояние не сохранено (%s): %s. Следующий прогон "
                     "начнёт с чистого листа, подтверждение падений "
                     "не сработает", path, e)
        return False


def apply_results(state: dict, results: List[dict],
                  confirm_fails: int = None) -> Tuple[dict, List[dict]]:
    """Обновляет состояние результатами прогона и возвращает события.

    Событие рождается только на смене статуса: домен признан упавшим или
    снова отвечает. Пока статус не менялся, событий нет и сообщать нечего.

    Домены, которых больше нет во входном списке, из состояния выпадают:
    иначе файл растёт бесконечно и хранит записи о том, что давно не
    проверяется.
    """
    confirm_fails = CONFIRM_FAILS if confirm_fails is None else confirm_fails
    old = state.get("domains", {}) if isinstance(state, dict) else {}
    domains: Dict[str, dict] = {}
    events: List[dict] = []
    now = _stamp()

    for result in results:
        key = result.get("label") or result.get("url")
        prev = old.get(key) or {"fails": 0, "down": False, "since": None}
        was_down = bool(prev.get("down"))

        if result.get("ok"):
            if was_down:
                events.append({"event": EVENT_UP, "label": key,
                               "url": result.get("url"),
                               "contour": result.get("contour") or "unknown",
                               "since": prev.get("since"), "reason": None})
            domains[key] = {"fails": 0, "down": False, "since": None}
            continue

        fails = int(prev.get("fails") or 0) + 1
        is_down = was_down or fails >= confirm_fails
        since = prev.get("since")
        if is_down and not was_down:
            since = now
            events.append({"event": EVENT_DOWN, "label": key,
                           "url": result.get("url"), "since": since,
                           "contour": result.get("contour") or "unknown",
                           "reason": reason_of(result)})
        domains[key] = {"fails": fails, "down": is_down, "since": since}

    new_state = {"version": STATE_VERSION, "updated_at": now,
                 "domains": domains}
    # Отметку о последнем признаке жизни переносим: без этого она терялась бы
    # каждый прогон, и heartbeat уходил бы в канал каждые пять минут.
    if state.get("last_heartbeat") if isinstance(state, dict) else None:
        new_state["last_heartbeat"] = state["last_heartbeat"]
    logger.info("События: упало %d, поднялось %d",
                sum(1 for e in events if e["event"] == EVENT_DOWN),
                sum(1 for e in events if e["event"] == EVENT_UP))
    return new_state, events


def due_for_heartbeat(state: dict, every_hours: int = None) -> bool:
    """Пора ли присылать признак жизни.

    Отметки нет - пора: это первый прогон после выката, и подтверждение
    того, что чекер вообще дошёл до отправки, нужно сразу.
    """
    every_hours = HEARTBEAT_EVERY_HOURS if every_hours is None else every_hours
    if every_hours <= 0:
        return False
    stamp = (state or {}).get("last_heartbeat")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        # Битую отметку считаем отсутствующей: лучше лишнее сообщение,
        # чем молчание из-за испорченной строки в файле.
        return True
    return now_msk() - last >= timedelta(hours=every_hours)


def mark_heartbeat(state: dict) -> dict:
    state["last_heartbeat"] = _stamp()
    return state
