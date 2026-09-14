"""Что делать с результатом проверки.

Три вещи, и все три о людях, а не о сети. Память между запусками решает, кого
считать упавшим (два неудачных прогона подряд) и когда статус сменился. Тексты
сообщений собирают событие в читаемый вид, а предохранители не дают вывалить
в канал перечень из сотен адресов. Отправка уходит в Mattermost на stdlib.
"""

from .alert import (build_alert, build_event_alert, build_heartbeat,
                    group_by_reason, problems, send)
from .notifier import send_to_mattermost
from .state import (EVENT_DOWN, EVENT_UP, apply_results, due_for_heartbeat,
                    empty_state, load_state, mark_heartbeat, save_state)

__all__ = ["apply_results", "load_state", "save_state", "empty_state",
           "due_for_heartbeat", "mark_heartbeat", "EVENT_DOWN", "EVENT_UP",
           "build_alert", "build_event_alert", "build_heartbeat",
           "problems", "group_by_reason", "send", "send_to_mattermost"]
