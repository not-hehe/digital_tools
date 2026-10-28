"""Тексты сообщений в канал. Шапка - как у краткого отчёта, под ней что изменилось."""

from datetime import datetime
from typing import List, Optional

from ..reporting.report_writer import summary_header
from .state import (EVENT_APPEARED, EVENT_LOST, due_for_heartbeat, mark_heartbeat,
                    now_msk)

_RULE = "-" * 60


def _line(e: dict) -> str:
    if e["event"] == EVENT_APPEARED:
        what = ", ".join(x for x in (e.get("status"), e.get("container")) if x)
        text = "  {} ({})".format(e["url"], what) if what else "  " + e["url"]
        if e.get("new"):
            text += " - новый в списке"
    else:
        was = e.get("was_container") or "без идентификатора"
        text = "  {} (было {}, сейчас {})".format(e["url"], was, e.get("status"))
    if e.get("from_errors") and not e.get("revived"):
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
    # Ожившие из колонки ошибок не прячутся за защитой от лавины: их немного,
    # и человек должен перенести их в таблице.
    revived = [e for e in events if e.get("revived")]
    events = [e for e in events if not e.get("revived")]
    revived_block = (_block("🟢 Ожили из списка ошибок, Google есть: {}", revived)[:-1]
                     + ['Перенесите их в таблице из колонки "ошибки" в колонку контура.', _RULE]
                     if revived else [])
    if first_run:
        body = ["🟢 GA-чекер начал следить за Google-аналитикой. Дальше в канал приходят",
                "только изменения: появилась аналитика или пропала.", _RULE] + revived_block
    elif events or revived:
        appeared = [e for e in events if e["event"] == EVENT_APPEARED]
        lost = [e for e in events if e["event"] == EVENT_LOST]
        # По памяти, а не по прогону: сайт с Google, который сейчас не открылся,
        # тоже числится с Google.
        ga_now = sum(1 for s in state.get("sites", {}).values() if s.get("ga") is True)
        tracked_before = ga_now - len(appeared) - len(revived) + len(lost)
        # Возвращением сети считаются только сайты, которые раньше теряли Google;
        # новые в таблице и впервые получившие Google всегда называются списком.
        returning = [e for e in appeared if e.get("returned")]
        new_sites = [e for e in appeared if not e.get("returned")]
        body = []
        if len(lost) >= guard_min_items and len(lost) >= mass_ratio * max(tracked_before, 1):
            body += ["⚠️ Похоже, дело в самом чекере: Google пропал сразу у {} из {} сайтов."
                     .format(len(lost), tracked_before),
                     "Список не приводится: проверить сеть машины до Google и браузер.", _RULE]
            lost = []
        if len(returning) >= guard_min_items and len(returning) >= mass_ratio * max(ga_now, 1):
            body += ["🟢 Google снова виден сразу у {} сайтов: похоже, чекер снова видит сеть."
                     .format(len(returning)), "Список не приводится.", _RULE]
            returning = []
        appeared = returning + new_sites
        body += (_block("🟢 Появилась Google-аналитика: {}", appeared)
                 + _block("🔴 Пропала Google-аналитика: {}", lost) + revived_block)
    elif due_for_heartbeat(state, heartbeat_hours, now):
        body = ["🟢 GA-чекер жив, изменений нет", _RULE]
    else:
        return None
    mark_heartbeat(state, now)
    return _wrap(results, body)


def _wrap(results: List[dict], body: List[str]) -> str:
    return "```\n" + "\n".join(summary_header(results) + body + ["КОНЕЦ ОТЧЁТА"]) + "\n```"


def build_state_failure(results: List[dict], path) -> str:
    """Память не записалась: без неё каждый прогон был бы «первым» и изменения терялись бы."""
    return _wrap(results, ["⚠️ GA-чекер: память не сохранена в {}.".format(path),
                           "Смена состояния сайтов не отслеживается: проверить права на папку data/.",
                           _RULE])


def build_sheet_back(since: str) -> str:
    return "```\n🟢 GA-чекер: Google-таблица снова читается (не читалась с {})\n```".format(
        since.replace("T", " ")[:16])
