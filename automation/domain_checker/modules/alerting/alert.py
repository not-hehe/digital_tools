"""Тексты сообщений для Mattermost и их отправка.

Два вида сообщений. Сводка обо всех проблемных доменах нужна при ручном
прогоне: посмотреть, что сейчас с парком. Сообщение о смене состояния
уходит в канал при работе по расписанию: раз в пять минут сводка
превратилась бы в поток одинаковых сообщений.

Перед обоими стоят одни и те же предохранители. Если разом легла или вернулась
большая часть списка либо целиком один контур, перечень из сотен адресов только
мешает, и вместо него уходит одна строка о том, что дело, похоже, в самой
машине проверки.
"""

import logging
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Callable, Dict, List, Optional

from ..probe import reason_of
from ..config import (GUARD_MIN_ITEMS, MASS_FAILURE_RATIO, MATTERMOST_TIMEOUT,
                     now_msk)
from .notifier import send_to_mattermost

logger = logging.getLogger(__name__)

_CONTOUR_NAMES = {"internal": "внутренним контуром",
                  "external": "внешним контуром"}

_NO_LIST = "Список не приводится: он не помогает."


# ---------------------------------------------------------------------------
# Сборка текста
# ---------------------------------------------------------------------------

def _group(items: List[dict], key: Callable) -> "OrderedDict[str, List[dict]]":
    """Раскладывает по группам, крупные сверху: чинить начинают с массового."""
    buckets: Dict[str, List[dict]] = {}
    for item in items:
        buckets.setdefault(key(item), []).append(item)
    return OrderedDict(sorted(buckets.items(),
                              key=lambda kv: (-len(kv[1]), kv[0])))


def _render_groups(groups: "OrderedDict[str, List[dict]]") -> List[str]:
    """Строки вида "причина - N" и адреса под ней с отступом."""
    lines = []
    for reason, items in groups.items():
        lines.append("{} - {}".format(reason, len(items)))
        for item in items:
            lines.append("  {}".format(item["label"]))
        lines.append("")
    return lines


def _footer(checked: int, checked_at: datetime, extra: str = "") -> str:
    return "Проверено {}{} · {} МСК".format(
        checked, extra, checked_at.strftime("%Y-%m-%d %H:%M"))


def _short_alert(headline: str, body: str, checked: int,
                 checked_at: datetime) -> str:
    """Сообщение предохранителя: заголовок, объяснение и никакого перечня."""
    return "{}\n\n{}\n\n{}\n\n{}\n".format(
        headline, body, _NO_LIST, _footer(checked, checked_at))


def problems(results: List[dict]) -> List[dict]:
    return [r for r in results if not r.get("ok")]


def group_by_reason(results: List[dict]) -> "OrderedDict[str, List[dict]]":
    return _group(results, reason_of)


def build_alert(results: List[dict], checked_at: datetime = None,
                contour_counts: Dict[str, int] = None) -> Optional[str]:
    """Сводка обо всех проблемных доменах. None, если проблемных нет.

    None это сигнал вызывающему, что отправлять нечего: тихий канал при
    здоровых доменах и есть требование этой версии.
    """
    bad = problems(results)
    if not bad:
        return None
    checked_at = checked_at or now_msk()

    # Предохранители нужны и здесь. Ручной прогон по всему списку с машины,
    # у которой отвалилась сеть, давал сообщение на 263 строки с перечнем
    # 236 адресов - тот самый случай, ради которого их и заводили.
    guard = _guards(bad, [], len(results), checked_at, contour_counts,
                    down_lead="Не отвечают")
    if guard is not None:
        return guard

    lines = ["🔴 Домены: проблемных {} из {}".format(len(bad), len(results)),
             ""]
    lines += _render_groups(group_by_reason(bad))
    lines.append(_footer(len(results), checked_at,
                         " · проблемных {}".format(len(bad))))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Предохранители
# ---------------------------------------------------------------------------

_ROUTE_TAIL = "Дело, скорее всего, в маршруте с машины проверки, а не в сайтах."

_MASS_DOWN_TAIL = ("Массовое падение обычно означает отказ сети или DNS "
                   "на машине\nпроверки, а не столько же независимых аварий. "
                   "Начните с неё.")


def _contour_alert(items: List[dict], contour_counts: Dict[str, int],
                   checked: int, checked_at: datetime, headline: str,
                   lead: str, tail: str,
                   mention_others: bool = True) -> Optional[str]:
    """Целиком один контур: и когда лёг, и когда вернулся.

    Нужен отдельно от общего порога, потому что общий порог этот случай
    пропускает. Внутренних доменов в списке около четверти: маршрут до
    внутренней сети может отказать целиком, и до половины списка отказ
    не дотянет. В канал ушли бы восемь десятков отдельных сообщений
    вместо одного про маршрут.
    """
    if not items or not contour_counts or len(items) < GUARD_MIN_ITEMS:
        return None
    hit = Counter(e.get("contour") or "unknown" for e in items)
    # Задеты разные контуры - маршрут ни при чём, разбираемся обычно.
    if len(hit) != 1:
        return None
    contour, count = next(iter(hit.items()))
    name = _CONTOUR_NAMES.get(contour)
    if name is None:
        return None
    total = contour_counts.get(contour) or 0
    if not total or count / total < MASS_FAILURE_RATIO:
        return None

    body = "{} {} из {} доменов этого контура.".format(lead, count, total)
    others = sum(v for k, v in contour_counts.items() if k != contour)
    if mention_others and others:
        body += "\nДомены другого контура при этом отвечают ({} шт).".format(others)
    body += "\n" + tail
    return _short_alert(headline.format(name), body, checked, checked_at)


def _mass_alert(items: List[dict], checked: int, checked_at: datetime,
                lead: str, tail: str) -> Optional[str]:
    """Разом задета большая доля всего списка.

    Такое почти всегда означает отказ сети или DNS на машине проверки,
    а не столько же независимых аварий. Перечень из сотен адресов в такой
    момент длинный, одинаковый и уводит от настоящей причины.
    """
    if not checked or MASS_FAILURE_RATIO <= 0 or len(items) < GUARD_MIN_ITEMS:
        return None
    if len(items) / checked < MASS_FAILURE_RATIO:
        return None
    body = "{} {} доменов из {} ({:.0f}%).\n{}".format(
        lead, len(items), checked, 100 * len(items) / checked, tail)
    return _short_alert("⚠️ Похоже, дело в самом чекере", body,
                        checked, checked_at)


def _guards(fell: List[dict], rose: List[dict], checked: int,
            checked_at: datetime, contour_counts: Dict[str, int],
            down_lead: str = "Одновременно перестали отвечать") -> Optional[str]:
    """Первое сработавшее сообщение предохранителя или None.

    Сначала более точный признак - целый контур. Общий порог идёт вторым:
    он ловит то, что в один контур не укладывается. Восстановление проверяется
    наравне с падением: маршрут, вернувшийся к жизни, даёт ровно такой же
    поток строк, что и отказавший.

    `down_lead` меняется ради ручного прогона: там сводка снимается на момент
    времени, и «перестали отвечать» обещало бы переход, которого никто
    не наблюдал.
    """
    return (_contour_alert(fell, contour_counts, checked, checked_at,
                           "⚠️ Похоже, пропала связь с {}",
                           down_lead, _ROUTE_TAIL)
            or _contour_alert(rose, contour_counts, checked, checked_at,
                              "🟢 Похоже, восстановилась связь с {}",
                              "Одновременно снова отвечают",
                              "Скорее всего, починился маршрут с машины "
                              "проверки.", mention_others=False)
            or _mass_alert(fell, checked, checked_at, down_lead,
                           _MASS_DOWN_TAIL)
            or _mass_alert(rose, checked, checked_at,
                           "Одновременно снова отвечают",
                           "Похоже, связь с машины проверки восстановилась."))


def build_event_alert(events: List[dict], checked: int,
                      checked_at: datetime = None,
                      contour_counts: Dict[str, int] = None) -> Optional[str]:
    """Сообщение о смене состояния: кто упал и кто поднялся.

    None, если событий нет. Именно это сообщение уходит в канал при работе
    по расписанию.
    """
    if not events:
        return None
    checked_at = checked_at or now_msk()
    fell = [e for e in events if e.get("event") == "down"]
    rose = [e for e in events if e.get("event") == "up"]

    guard = _guards(fell, rose, checked, checked_at, contour_counts)
    if guard is not None:
        return guard

    lines = []
    if fell:
        lines.append("🔴 Упали: {}".format(len(fell)))
        lines.append("")
        lines += _render_groups(_group(
            fell, lambda e: e.get("reason") or "причина не определена"))
    if rose:
        lines.append("🟢 Поднялись: {}".format(len(rose)))
        for e in rose:
            since = (e.get("since") or "").replace("T", " ")[:16]
            lines.append("  {}{}".format(
                e["label"],
                " (не отвечал с {})".format(since) if since else ""))
        lines.append("")
    lines.append(_footer(checked, checked_at))
    return "\n".join(lines) + "\n"


def build_heartbeat(checked: int, problems_count: int,
                    checked_at: datetime = None) -> str:
    """Признак жизни: короткая строка о том, что прогон состоялся.

    Уходит, даже когда всё хорошо, - в этом весь смысл. Пропажа такого
    сообщения сама становится сигналом, потому что молчание канала иначе
    означает и исправную работу, и незапустившийся прогон.
    """
    checked_at = checked_at or now_msk()
    body = "Проверено {} доменов, не отвечают {}.".format(checked, problems_count)
    return "🟢 Чекер жив\n\n{}\n\n{}\n".format(
        body, _footer(checked, checked_at))


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def send(text: Optional[str], webhook_url: str, sender=None) -> bool:
    """Отправляет готовый текст, молчит если текста нет.

    Отправитель принимается параметром, чтобы тест мог подставить свой
    и посчитать вызовы: проверить отсутствие отправки иначе нечем.
    """
    if text is None:
        logger.info("Сообщать не о чем, отправку пропускаем")
        return False
    if not webhook_url:
        logger.info("Вебхук не задан (DC_MATTERMOST_WEBHOOK), "
                    "сообщение не отправляем")
        return False
    sender = send_to_mattermost if sender is None else sender
    # Тройные кавычки сохраняют моноширинное выравнивание: без них Mattermost
    # съедает отступы, и группировка по причинам разваливается.
    return bool(sender(webhook_url, "```\n" + text + "```",
                       timeout=MATTERMOST_TIMEOUT))
