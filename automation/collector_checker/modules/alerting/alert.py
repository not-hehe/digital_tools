"""Тексты сообщений по правилам заказчика и отправка списка."""

from typing import Callable, List, Optional

from ..config import GROUP_MIN_DOWN
from .state import EVENT_DOWN, EVENT_UP

DOWN_MARK = "\U0001F534"   # красный кружок
UP_MARK = "\U0001F7E2"     # зелёный кружок


def build_messages(events: List[dict], group_min: Optional[int] = None) -> List[str]:
    """Сообщение уходит один раз на падение и один раз на подъём (Ваня, 2026-10-13);
    still_down остаётся только в журнале. Упавшие в этот прогон - по одному или сводно
    от group_min, поднявшиеся - всегда по одному. Сначала падения, потом подъёмы."""
    group_min = GROUP_MIN_DOWN if group_min is None else group_min
    down = [e["name"] for e in events if e["event"] == EVENT_DOWN]
    up = [e["name"] for e in events if e["event"] == EVENT_UP]

    messages = []
    if len(down) >= group_min:
        messages.append("{} Обнаружено падение: {}".format(DOWN_MARK, ", ".join(down)))
    else:
        messages.extend("{} Обнаружено падение, {}".format(DOWN_MARK, name) for name in down)
    messages.extend("{} Коллектор {} снова работает".format(UP_MARK, name) for name in up)
    return messages


def send_all(messages: List[str], sender: Callable[[str], bool]) -> int:
    """Возвращает число неотправленных. После неудачи не останавливается:
    сообщения независимы."""
    return sum(1 for text in messages if not sender(text))
