"""Таймер напоминаний о лежащих (data/state_down.txt) и выбор сообщений прогона.

Таймер запускает первое падение, когда до него все были живы; дальше отметки идут
ровным шагом REMIND_EVERY_MIN от этого падения. Пока таймер идёт, новые падения
ждут ближайшей отметки и уходят в общем напоминании (Ваня, 2026-10-14).
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import REMIND_EVERY_MIN, STATE_DOWN_NAME
from .alert import (KIND_FALL, KIND_REMIND, delivered, fall_batches, remind_batches,
                    up_batches)
from .state import EVENT_UP, STATUS_DOWN, STATUS_PENDING, keep_unreported, lying

_KEYS = ("last_change", "alert_last_sent")
_HEADER = ("# таймер напоминаний (MSK): last_change - падение, с которого идёт таймер;\n"
           "# alert_last_sent - последнее сообщение о падении\n")


def timer_path(state_path) -> Path:
    return Path(state_path).with_name(STATE_DOWN_NAME)


def load_timer(path) -> Optional[dict]:
    """Нет файла - таймер не идёт. Кривой файл тоже None: при лежащих это даст
    напоминание сразу, лишнее сообщение вместо потерянного."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as e:
        print("таймер не прочитан ({}): {}".format(path, e), file=sys.stderr)
        return None
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    try:
        return {key: datetime.fromisoformat(values[key]) for key in _KEYS}
    except (KeyError, ValueError) as e:
        print("таймер не разобран ({}), считается сброшенным: {!r}".format(path, e),
              file=sys.stderr)
        return None


def save_timer(timer: Optional[dict], path) -> bool:
    """None удаляет файл. Запись через временный файл, как у state.txt."""
    path = Path(path)
    try:
        if timer is None:
            path.unlink(missing_ok=True)
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(_HEADER + "".join("{}={}\n".format(key, timer[key].isoformat(timespec="seconds"))
                                      for key in _KEYS))
        os.replace(tmp, path)
        return True
    except OSError as e:
        print("таймер не сохранён ({}): {}".format(path, e), file=sys.stderr)
        return False


def drop_stale(timer: Optional[dict], previous: Dict[str, dict]) -> Optional[dict]:
    # Таймер без лежащих остаётся от несохранённого сброса; с ним новое падение
    # ушло бы напоминанием вместо сообщения о падении.
    return timer if timer is not None and lying(previous) else None


def _slot(timer: dict, moment: datetime, every_min: int) -> int:
    # По минуте запуска: прогон в 20:05:20 после падения в 20:00:40 - уже отметка 20:05.
    start = timer["last_change"].replace(second=0, microsecond=0)
    minutes = int((moment.replace(second=0, microsecond=0) - start).total_seconds() // 60)
    return minutes // every_min


def is_due(timer: dict, now: datetime, every_min: Optional[int] = None) -> bool:
    every_min = REMIND_EVERY_MIN if every_min is None else every_min
    current = _slot(timer, now, every_min)
    return current >= 1 and current > _slot(timer, timer["alert_last_sent"], every_min)


def plan_batches(state: Dict[str, dict], events: List[dict], timer: Optional[dict],
                 now: datetime, group_min: Optional[int] = None) -> List[dict]:
    """state - после apply_results. Сначала падение или напоминание, затем подъёмы."""
    names = lying(state)
    batches = []
    if names:
        if timer is None:
            fresh = all(state[name]["status"] == STATUS_PENDING for name in names)
            # Таймера нет, а о ком-то уже сообщали - таймер потерян: напоминание сразу.
            batches += (fall_batches if fresh else remind_batches)(names, group_min)
        elif is_due(timer, now):
            batches += remind_batches(names, group_min)
    batches += up_batches([e["name"] for e in events if e["event"] == EVENT_UP], group_min)
    return batches


def settle(state: Dict[str, dict], previous: Dict[str, dict], timer: Optional[dict],
           batches: List[dict], unsent: List[str], now: datetime
           ) -> Tuple[Dict[str, dict], Optional[dict]]:
    """Состояние и таймер после отправки: сообщённые становятся down, неотправленный
    подъём откатывается к прошлой записи и повторится в следующем прогоне."""
    result = dict(state)
    told = False
    remind_failed = False
    unsent_up = []
    for batch in batches:
        ok = delivered(batch, unsent)
        if batch["kind"] in (KIND_FALL, KIND_REMIND):
            if ok:
                told = True
                for name in batch["names"]:
                    result[name] = {"status": STATUS_DOWN, "since": result[name]["since"]}
            elif batch["kind"] == KIND_REMIND:
                remind_failed = True
        elif not ok:
            unsent_up += batch["names"]
    result = keep_unreported(result, previous, unsent_up)

    if not lying(result):
        return result, None
    if timer is None:
        if told:
            timer = {"last_change": now, "alert_last_sent": now}
    elif told and not remind_failed:
        # Хоть одно напоминание не ушло - отметка не сдвигается, следующий прогон повторит все.
        timer = {"last_change": timer["last_change"], "alert_last_sent": now}
    return result, timer
