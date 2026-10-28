"""Память между запусками: какие сайты шлют запросы в Google.

Запись на сайт: ga - True, False или None (ни разу не открылся, неизвестно),
misses - сколько прогонов подряд открылся без Google, хотя числился с Google,
since - с какого прогона текущее ga. Сайт, который не открылся, память
не меняет: по нему нечего сказать.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import STATUS_ERROR
from ..reporting.aggregator import has_network_evidence

logger = logging.getLogger(__name__)

STATE_VERSION = 1
EVENT_APPEARED = "appeared"
EVENT_LOST = "lost"
MSK = timezone(timedelta(hours=3))


def now_msk() -> datetime:
    return datetime.now(MSK).replace(tzinfo=None)


def _stamp(now: Optional[datetime] = None) -> str:
    return (now or now_msk()).isoformat(timespec="seconds")


def empty_state() -> dict:
    return {"version": STATE_VERSION, "updated_at": None, "last_heartbeat": None,
            "baseline_at": None, "sheet_alert": None, "sites": {}}


def load_state(path: Path) -> Tuple[dict, bool]:
    """(память, первый ли это запуск). Битый файл - как его отсутствие:
    чекер начинает заново и сообщает об этом, а не падает каждую неделю.
    Первый запуск - пока не было ни одного полного прогона: одна лишь отметка
    о сбое таблицы наблюдения не начинает."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_state(), True
    except (OSError, ValueError) as e:
        logger.warning("Память %s не читается (%s), начинаю наблюдение заново", path, e)
        return empty_state(), True
    if not isinstance(data, dict) or not isinstance(data.get("sites"), dict):
        logger.warning("Память %s не того формата, начинаю наблюдение заново", path)
        return empty_state(), True
    return data, not data.get("baseline_at")


def save_state(state: dict, path: Path) -> bool:
    path = Path(path)
    state["updated_at"] = _stamp()
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.error("Память не сохранена в %s: %s", path, e)
        return False
    return True


def _event(kind: str, r: dict, rec: dict, new: bool = False, revived: bool = False) -> dict:
    return {"event": kind, "url": r.get("url"), "status": r.get("status"),
            "container": r.get("container_id"), "was_container": rec.get("container"),
            "from_errors": bool(r.get("from_errors")), "new": new, "revived": revived,
            "returned": kind == EVENT_APPEARED and bool(rec.get("lost_at"))}


def apply_results(state: dict, results: List[dict], first_run: bool,
                  confirm_lost: int, now: Optional[datetime] = None) -> Tuple[dict, List[dict]]:
    """Сравнивает результаты прогона с памятью и возвращает события.

    Появление Google сообщается сразу: оно не бывает случайным. Пропажа -
    после confirm_lost прогонов подряд без Google: антибот или баннер
    согласия может один раз скрыть счётчик.
    """
    stamp = _stamp(now)
    if first_run:
        state["baseline_at"] = stamp
    sites: Dict[str, dict] = state.setdefault("sites", {})
    events: List[dict] = []
    seen = set()
    for r in results:
        url = r.get("url")
        if not url:
            continue
        seen.add(url)
        opened = r.get("status") != STATUS_ERROR
        ga = has_network_evidence(r) if opened else None
        rec = sites.get(url)
        if rec is None:
            rec = {"ga": ga, "misses": 0, "since": stamp, "container": None}
            sites[url] = rec
            if ga and first_run and r.get("from_errors"):
                # Как «Ожили из списка ошибок» у domain_checker: о живом сайте из колонки
                # ошибок сообщается и в первом прогоне.
                events.append(_event(EVENT_APPEARED, r, rec, revived=True))
            elif ga and not first_run:
                events.append(_event(EVENT_APPEARED, r, rec, new=True))
        elif not opened:
            continue
        elif ga:
            if rec.get("ga") is False:
                events.append(_event(EVENT_APPEARED, r, rec))
                rec["since"] = stamp
                rec.pop("lost_at", None)
            elif rec.get("ga") is None:
                # Сайт из колонки ошибок лежал и ожил сразу с Google - сообщаем, как domain.
                # Обычный сайт, лежавший при первом прогоне, молчит: Google у него, скорее
                # всего, был и раньше.
                if r.get("from_errors"):
                    events.append(_event(EVENT_APPEARED, r, rec, revived=True))
                rec["since"] = stamp
            rec["ga"], rec["misses"] = True, 0
        elif rec.get("ga") is True:
            rec["misses"] = rec.get("misses", 0) + 1
            if rec["misses"] >= confirm_lost:
                events.append(_event(EVENT_LOST, r, rec))
                rec["ga"], rec["misses"], rec["since"] = False, 0, stamp
                # Вернётся - это «Google снова виден», а не новое появление: по этой
                # отметке защита от лавины отличает возвращение сети от выкатки GA.
                rec["lost_at"] = stamp
        elif rec.get("ga") is None:
            rec["ga"], rec["since"] = False, stamp
        if opened:
            rec["status"] = r.get("status")
            if r.get("container_id"):
                rec["container"] = r.get("container_id")
    # Сайт убрали из таблицы - забываем: вернётся, начнёт заново без события.
    for url in [u for u in sites if u not in seen]:
        del sites[url]
    return state, events


def mark_sheet_failure(state: dict, reason: str) -> bool:
    """True, если о сбое таблицы ещё не сообщали: как у domain_checker, сообщение
    одно, дальше тишина до починки."""
    if state.get("sheet_alert"):
        return False
    state["sheet_alert"] = {"since": _stamp(), "reason": reason}
    return True


def clear_sheet_failure(state: dict) -> Optional[str]:
    """С какого времени таблица не читалась, если об этом сообщали; отметка снимается."""
    alert = state.get("sheet_alert")
    state["sheet_alert"] = None
    return alert.get("since") if alert else None


def due_for_heartbeat(state: dict, every_hours: int, now: Optional[datetime] = None) -> bool:
    if every_hours <= 0:
        return False
    last = state.get("last_heartbeat")
    if not last:
        return True
    try:
        then = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (now or now_msk()) - then >= timedelta(hours=every_hours)


def mark_heartbeat(state: dict, now: Optional[datetime] = None) -> None:
    state["last_heartbeat"] = _stamp(now)
