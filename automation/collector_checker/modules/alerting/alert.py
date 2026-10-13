"""Тексты сообщений и отправка списка."""

from typing import Callable, List, Optional

from ..config import GROUP_MIN_DOWN
from .state import EVENT_DOWN, EVENT_UP

DOWN_MARK = "\U0001F534"   # красный кружок
UP_MARK = "\U0001F7E2"     # зелёный кружок


def build_batches(events: List[dict], group_min: Optional[int] = None) -> List[dict]:
    """Сообщения с именами коллекторов; по именам неотправленное повторяется в следующем
    прогоне. Одно сообщение на падение и одно на подъём, still_down не сообщается.
    От group_min событий за прогон формируется одно сообщение списком.
    Сначала падения, затем подъёмы."""
    group_min = GROUP_MIN_DOWN if group_min is None else group_min
    down = [e["name"] for e in events if e["event"] == EVENT_DOWN]
    up = [e["name"] for e in events if e["event"] == EVENT_UP]

    batches = []
    if len(down) >= group_min:
        batches.append({"text": "{} Обнаружено падение:\n{}".format(
            DOWN_MARK, "\n".join("- Коллектор {}".format(name) for name in down)), "names": down})
    else:
        batches.extend({"text": "{} Обнаружено падение, {}".format(DOWN_MARK, name), "names": [name]}
                       for name in down)
    if len(up) >= group_min:
        batches.append({"text": "{} Коллектор снова работает:\n{}".format(
            UP_MARK, "\n".join("- Коллектор {}".format(name) for name in up)), "names": up})
    else:
        batches.extend({"text": "{} Коллектор {} снова работает".format(UP_MARK, name), "names": [name]}
                       for name in up)
    return batches


def build_messages(events: List[dict], group_min: Optional[int] = None) -> List[str]:
    # Только тексты, для тестов.
    return [b["text"] for b in build_batches(events, group_min)]


def send_batches(batches: List[dict], sender: Callable[[str], bool]) -> List[str]:
    """Имена из неотправленных сообщений. Перебор не прерывается после неудачи."""
    unsent = []
    for batch in batches:
        if not sender(batch["text"]):
            unsent.extend(batch["names"])
    return unsent
