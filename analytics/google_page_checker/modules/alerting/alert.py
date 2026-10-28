"""Тексты сообщений в канал. Шапка - как у краткого отчёта, под ней что изменилось."""

from datetime import datetime
from typing import List, Optional

from ..config import STATUS_ERROR
from ..reporting.aggregator import has_network_evidence
from ..reporting.report_writer import summary_header
from .state import (EVENT_APPEARED, EVENT_LOST, due_for_heartbeat, mark_heartbeat,
                    now_msk)

_RULE = "-" * 60


def _with_ga(results: List[dict]) -> int:
    return sum(1 for r in results
               if r.get("status") != STATUS_ERROR and has_network_evidence(r))


def _line(e: dict) -> str:
    if e["event"] == EVENT_APPEARED:
        what = ", ".join(x for x in (e.get("status"), e.get("container")) if x)
        text = "  {} ({})".format(e["url"], what) if what else "  " + e["url"]
        if e.get("new"):
            text += " - новый в списке"
    else:
        was = e.get("was_container") or "без идентификатора"
        text = "  {} (было {}, сейчас {})".format(e["url"], was, e.get("status"))
    if e.get("from_errors"):
        text += " - из колонки ошибок"
    return text


def _block(title: str, items: List[dict]) -> List[str]:
    if not items:
        return []
    return ([title.format(len(items))]
            + [_line(e) for e in sorted(items, key=lambda e: e["url"])] + [_RULE])


def build_message(state: dict, events: List[dict], results: List[dict], first_run: bool,
                  guard_min_items: int, mass_ratio: float, heartbeat_hours: int,
                  now: Optional[datetime] = None) -> Optional[str]:
    """Текст для канала или None, если сообщать не о чем. Отправленное
    сообщение любого вида засчитывается как признак жизни."""
    now = now or now_msk()
    if first_run:
        body = ["🟢 GA-чекер начал следить за Google-аналитикой. Дальше в канал приходят",
                "только изменения: появилась аналитика или пропала.", _RULE]
    elif events:
        appeared = [e for e in events if e["event"] == EVENT_APPEARED]
        lost = [e for e in events if e["event"] == EVENT_LOST]
        with_ga = _with_ga(results)
        tracked_before = with_ga - len(appeared) + len(lost)
        body = []
        if len(lost) >= guard_min_items and len(lost) >= mass_ratio * max(tracked_before, 1):
            body += ["⚠️ Похоже, дело в самом чекере: Google пропал сразу у {} из {} сайтов."
                     .format(len(lost), tracked_before),
                     "Список не приводится: проверить сеть машины до Google и браузер.", _RULE]
            lost = []
        if len(appeared) >= guard_min_items and len(appeared) >= mass_ratio * max(with_ga, 1):
            body += ["🟢 Google снова виден сразу у {} сайтов: похоже, чекер снова видит сеть."
                     .format(len(appeared)), "Список не приводится.", _RULE]
            appeared = []
        body += (_block("🟢 Появилась Google-аналитика: {}", appeared)
                 + _block("🔴 Пропала Google-аналитика: {}", lost))
    elif due_for_heartbeat(state, heartbeat_hours, now):
        body = ["🟢 GA-чекер жив, изменений нет", _RULE]
    else:
        return None
    mark_heartbeat(state, now)
    return "```\n" + "\n".join(summary_header(results) + body + ["КОНЕЦ ОТЧЁТА"]) + "\n```"
