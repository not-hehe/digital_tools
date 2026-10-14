"""Состояние между прогонами: строка на коллектор, `v7 down 2026-10-12T15:00:03`.
Отсутствующий файл считается первым запуском; ошибка записи завершает прогон кодом 1.
pending - лежит, но в чат ещё не сообщено: ждёт отметки таймера или повтора отправки.
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import STATE_FILE, now_msk
from ..probe import reason_of

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_PENDING = "pending"
_LYING = (STATUS_DOWN, STATUS_PENDING)

EVENT_DOWN = "down"
EVENT_STILL_DOWN = "still_down"
EVENT_UP = "up"
EVENT_QUIET_UP = "quiet_up"

_HEADER = "# collector status since(MSK); pending - лежит, в чат ещё не сообщено"


def parse_state(text: str) -> Dict[str, dict]:
    """Некорректная строка пропускается с предупреждением."""
    state = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        try:
            if len(parts) != 3 or parts[1] not in (STATUS_UP,) + _LYING:
                raise ValueError("ожидается '<имя> up|down|pending <время>'")
            state[parts[0]] = {"status": parts[1], "since": datetime.fromisoformat(parts[2])}
        except ValueError as e:
            print("состояние: строка {} пропущена ({}): {!r}".format(number, e, raw),
                  file=sys.stderr)
    return state


def load_state(path: Optional[Path] = None) -> Dict[str, dict]:
    path = STATE_FILE if path is None else Path(path)
    try:
        return parse_state(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print("состояние не найдено ({}): первый запуск, все коллекторы считаются "
              "живыми".format(path), file=sys.stderr)
    except OSError as e:
        print("состояние не прочитано ({}): {}. Начинаем с пустого".format(path, e),
              file=sys.stderr)
    return {}


def render_state(state: Dict[str, dict]) -> str:
    lines = [_HEADER]
    for name, entry in state.items():
        lines.append("{} {} {}".format(name, entry["status"],
                                       entry["since"].isoformat(timespec="seconds")))
    return "\n".join(lines) + "\n"


def save_state(state: Dict[str, dict], path: Optional[Path] = None) -> bool:
    """Запись через временный файл: сбой посреди записи не портит state.txt."""
    path = STATE_FILE if path is None else Path(path)
    tmp = str(path) + ".tmp"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(render_state(state))
        os.replace(tmp, path)
        return True
    except OSError as e:
        print("состояние не сохранено ({}): {}".format(path, e), file=sys.stderr)
        return False


def apply_results(state: Dict[str, dict], results: List[dict],
                  now: Optional[datetime] = None) -> Tuple[Dict[str, dict], List[dict]]:
    """Новое состояние по проверенным коллекторам и события. Новое падение - pending:
    сообщено ли о нём, решает reminder.settle после отправки."""
    now = now_msk() if now is None else now
    new_state = {}
    events = []
    for result in results:
        name = result["name"]
        previous = state.get(name)
        was = previous["status"] if previous is not None else STATUS_UP
        if result["ok"]:
            if was in _LYING:
                event = EVENT_UP if was == STATUS_DOWN else EVENT_QUIET_UP
                events.append({"event": event, "name": name, "since": now,
                               "reason": reason_of(result)})
                new_state[name] = {"status": STATUS_UP, "since": now}
            else:
                new_state[name] = previous or {"status": STATUS_UP, "since": now}
        elif was in _LYING:
            events.append({"event": EVENT_STILL_DOWN, "name": name,
                           "since": previous["since"], "reason": reason_of(result)})
            new_state[name] = dict(previous)
        else:
            events.append({"event": EVENT_DOWN, "name": name, "since": now,
                           "reason": reason_of(result)})
            new_state[name] = {"status": STATUS_PENDING, "since": now}
    return new_state, events


def lying(state: Dict[str, dict]) -> List[str]:
    return [name for name, entry in state.items() if entry["status"] in _LYING]


def keep_unreported(new_state: Dict[str, dict], old_state: Dict[str, dict],
                    names: List[str]) -> Dict[str, dict]:
    """Коллекторы с неотправленным сообщением возвращаются к прошлому статусу;
    следующий прогон отправит его снова."""
    result = dict(new_state)
    for name in names:
        if name in old_state:
            result[name] = old_state[name]
        else:
            result.pop(name, None)
    return result
