"""Что сообщать в канал.

Как у domain_checker: память между запусками и сообщение только о смене
состояния. Сайт либо начал отправлять запросы в Google, либо перестал;
остальные прогоны канал не трогают, кроме признака жизни раз в сутки.
"""

from .alert import build_message
from .state import (EVENT_APPEARED, EVENT_LOST, apply_results, empty_state,
                    load_state, save_state)

__all__ = ["apply_results", "build_message", "empty_state", "load_state",
           "save_state", "EVENT_APPEARED", "EVENT_LOST"]
