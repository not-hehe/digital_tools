"""Тексты сообщений по правилам заказчика и отправка списка."""

from typing import Callable, List, Optional

from ..config import GROUP_MIN_DOWN
from .state import EVENT_DOWN, EVENT_UP

DOWN_MARK = "\U0001F534"   # красный кружок
UP_MARK = "\U0001F7E2"     # зелёный кружок


def build_batches(events: List[dict], group_min: Optional[int] = None) -> List[dict]:
    """Сообщения с именами коллекторов, о которых они говорят: по именам
    неотправленное сообщение возвращается в следующий прогон.

    Сообщение уходит один раз на падение и один раз на подъём (Ваня, 2026-10-13);
    still_down остаётся только в журнале. От group_min упавших или поднявшихся
    в один прогон - одно сводное сообщение. Сначала падения, потом подъёмы."""
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
    # Только тексты: удобно в тестах, в прогоне нужны пакеты с именами.
    return [b["text"] for b in build_batches(events, group_min)]


def send_batches(batches: List[dict], sender: Callable[[str], bool]) -> List[str]:
    """Имена коллекторов из неотправленных сообщений. После неудачи
    не останавливается: сообщения независимы."""
    unsent = []
    for batch in batches:
        if not sender(batch["text"]):
            unsent.extend(batch["names"])
    return unsent
