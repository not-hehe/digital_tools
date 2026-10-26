"""Что делать с результатом проверки.

Три вещи, и все три о людях, а не о сети. Память между запусками решает, кого
считать упавшим (несколько неудачных прогонов подряд) и когда статус сменился. Тексты
сообщений собирают событие в читаемый вид, а предохранители не дают вывалить
в канал перечень из сотен адресов. Отправка уходит в Mattermost на stdlib.
"""

from .alert import (build_alert, build_event_alert, build_heartbeat,
                    build_sheet_down, build_sheet_up, send)
from .runlog import append as runlog_append
from .runlog import event_lines, run_line
from .notifier import send_to_mattermost
from .state import (EVENT_DOWN, EVENT_UP, apply_results, clear_sheet_alert,
                    due_for_heartbeat, load_sheet_alert, load_state,
                    mark_heartbeat, pick_alerts, save_sheet_alert, save_state)

__all__ = ["apply_results", "pick_alerts", "load_state", "save_state",
           "due_for_heartbeat", "mark_heartbeat",
           "load_sheet_alert", "save_sheet_alert", "clear_sheet_alert",
           "EVENT_DOWN", "EVENT_UP",
           "build_alert", "build_event_alert", "build_heartbeat",
           "build_sheet_down", "build_sheet_up", "send", "send_to_mattermost",
           "runlog_append", "run_line", "event_lines"]
