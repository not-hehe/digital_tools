"""Состояние между прогонами: текстовый файл, строка на коллектор.

    v1 up 2026-10-12T16:00:05
    v7 down 2026-10-12T15:00:03

Нечитаемый файл это первый запуск, незаписанный роняет прогон кодом 1: без
памяти чекер перестаёт отличать "упал" от "лежит" и теряет сообщение о подъёме.
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

EVENT_DOWN = "down"
EVENT_STILL_DOWN = "still_down"
EVENT_UP = "up"

_HEADER = "# collector status since(MSK)"


def parse_state(text: str) -> Dict[str, dict]:
    state = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3 or parts[1] not in (STATUS_UP, STATUS_DOWN):
            raise ValueError("строка состояния не разбирается: {!r}".format(raw))
        state[parts[0]] = {"status": parts[1], "since": datetime.fromisoformat(parts[2])}
    return state


def load_state(path: Optional[Path] = None) -> Dict[str, dict]:
    path = STATE_FILE if path is None else Path(path)
    try:
        return parse_state(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print("состояние не найдено ({}): первый запуск, все коллекторы считаются "
              "живыми".format(path), file=sys.stderr)
    except (OSError, ValueError) as e:
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
    """Через временный файл: обрезанный state.txt после сбоя посреди записи
    на следующем прогоне выглядел бы как первый запуск."""
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
    """Новое состояние только по проверенным коллекторам и события к отправке."""
    now = now_msk() if now is None else now
    new_state = {}
    events = []
    for result in results:
        name = result["name"]
        previous = state.get(name)
        was_down = previous is not None and previous["status"] == STATUS_DOWN
        if result["ok"]:
            if was_down:
                events.append({"event": EVENT_UP, "name": name, "since": now,
                               "reason": reason_of(result)})
                new_state[name] = {"status": STATUS_UP, "since": now}
            else:
                new_state[name] = previous or {"status": STATUS_UP, "since": now}
        else:
            since = previous["since"] if was_down else now
            events.append({"event": EVENT_STILL_DOWN if was_down else EVENT_DOWN,
                           "name": name, "since": since, "reason": reason_of(result)})
            new_state[name] = {"status": STATUS_DOWN, "since": since}
    return new_state, events


def keep_unreported(new_state: Dict[str, dict], old_state: Dict[str, dict],
                    names: List[str]) -> Dict[str, dict]:
    """Откатывает коллекторы, о которых сообщение не ушло, к прошлому статусу:
    следующий прогон увидит ту же смену и отправит его снова. Иначе сообщение
    терялось бы навсегда, а состояние уже считало его сообщённым."""
    result = dict(new_state)
    for name in names:
        if name in old_state:
            result[name] = old_state[name]
        else:
            result.pop(name, None)
    return result
