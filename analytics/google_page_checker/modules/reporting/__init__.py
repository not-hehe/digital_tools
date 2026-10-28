"""Что делать с результатами.

Слияние прогонов по силе улик, расширенный и краткий отчёты, отправка
краткого в Mattermost на stdlib.
"""

from .aggregator import has_network_evidence, merge_runs
from .notifier import send_to_mattermost
from .report_writer import ReportWriter

__all__ = ["merge_runs", "has_network_evidence", "ReportWriter",
           "send_to_mattermost"]
