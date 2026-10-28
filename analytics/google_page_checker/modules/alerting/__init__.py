"""Что сообщать в канал.

Как у domain_checker: память между запусками и сообщение только о смене
состояния. Сайт либо начал отправлять запросы в Google, либо перестал;
остальные прогоны канал не трогают, кроме признака жизни раз в сутки.
"""

from .alert import build_message, build_sheet_back, build_state_failure
from .state import (EVENT_APPEARED, EVENT_LOST, apply_results, clear_sheet_failure,
                    empty_state, load_state, mark_sheet_failure, save_state)

__all__ = ["apply_results", "build_message", "build_sheet_back", "build_state_failure",
           "clear_sheet_failure", "empty_state", "load_state", "mark_sheet_failure",
           "save_state", "EVENT_APPEARED", "EVENT_LOST"]
