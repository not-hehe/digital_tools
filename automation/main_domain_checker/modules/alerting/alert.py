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

from collections import Counter, OrderedDict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from ..probe import failure_label, reason_of
from ..probe.failures import (FAIL_DNS, FAIL_TLS, FAIL_TIMEOUT, FAIL_REFUSED,
                              FAIL_REDIRECT_LOOP, FAIL_PROXY, FAIL_INVALID_URL,
                              FAIL_OTHER)
from ..config import (GROUP_WINDOW_MINUTES, GUARD_MIN_ITEMS, LONE_DOWN_HOURS,
                     MASS_FAILURE_RATIO, MATTERMOST_TIMEOUT, REQUEST_TIMEOUT,
                     now_msk, short_stamp)
from .notifier import send_to_mattermost
from .state import ALERT_GROUP, ALERT_LONE, EVENT_DOWN, EVENT_UP

_CONTOUR_NAMES = {"internal": "внутренним контуром",
                  "external": "внешним контуром"}

_NO_LIST = "Список не приводится: он не помогает."

_HTTP_PLAIN = {
    400: "сервер не понял запрос",
    401: "нужен вход по логину и паролю",
    403: "доступ запрещён",
    404: "страница не найдена",
    429: "сервер ограничил число запросов",
    500: "внутренняя ошибка сервера",
    502: "промежуточный сервер получил от сайта неверный ответ",
    503: "сервер временно недоступен (перегружен или на обслуживании)",
    504: "промежуточный сервер не дождался ответа от сайта",
}

_KIND_PLAIN = {
    FAIL_TIMEOUT: ("Тайм-аут", "не открылся: сервер не ответил за {} "
                   "секунд".format(REQUEST_TIMEOUT)),
    FAIL_DNS: ("DNS", "не открылся: адрес не найден - домена нет в DNS "
               "или он виден только из внутренней сети"),
    FAIL_TLS: ("TLS", "не открылся: защищённое соединение не установилось - "
               "не подошли сертификат или шифрование"),
    FAIL_REFUSED: ("Отказ соединения",
                   "не открылся: сервер отклонил или оборвал соединение"),
    FAIL_REDIRECT_LOOP: ("Редиректы по кругу",
                         "не открылся: сайт бесконечно перенаправляет "
                         "на другие адреса"),
    FAIL_PROXY: ("Прокси", "не открылся: прокси на машине проверки "
                 "не пропустил запрос"),
    FAIL_INVALID_URL: ("Не адрес", "не проверялся: в списке строка, "
                       "которая не является адресом сайта"),
    FAIL_OTHER: ("Прочая ошибка", "не открылся по другой причине"),
}

# В state.json и runs.log причина хранится строкой из reason_of, поэтому
# пояснение ищется по ней, а не по виду отказа.
_PLAIN_BY_LABEL = {failure_label(k): v for k, v in _KIND_PLAIN.items()}


# ---------------------------------------------------------------------------
# Сборка текста
# ---------------------------------------------------------------------------

def _explain(reason: str) -> Tuple[str, Optional[str]]:
    """Короткое имя причины и пояснение простыми словами для канала."""
    if reason.startswith("HTTP "):
        try:
            code = int(reason[5:])
        except ValueError:
            return reason, None
        plain = _HTTP_PLAIN.get(code) or (
            "сбой на стороне сервера" if code >= 500
            else "сервер отказал в запросе")
        return reason, "открылся с ошибкой: " + plain
    return _PLAIN_BY_LABEL.get(reason, (reason, None))


def _group(items: List[dict], key: Callable) -> "OrderedDict[str, List[dict]]":
    """Раскладывает по группам, крупные сверху: чинить начинают с массового."""
    buckets: Dict[str, List[dict]] = {}
    for item in items:
        buckets.setdefault(key(item), []).append(item)
    return OrderedDict(sorted(buckets.items(),
                              key=lambda kv: (-len(kv[1]), kv[0])))


def _render_groups(groups: "OrderedDict[str, List[dict]]") -> List[str]:
    """Строки вида "причина - N", пояснение и адреса под ней с отступом."""
    lines = []
    for reason, items in groups.items():
        title, plain = _explain(reason)
        lines.append("{} - {}".format(title, len(items)))
        if plain:
            lines.append("  {}".format(plain))
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


def build_alert(results: List[dict], checked_at: datetime = None,
                contour_counts: Dict[str, int] = None) -> Optional[str]:
    """Сводка обо всех проблемных доменах. None, если проблемных нет.

    None это сигнал вызывающему, что отправлять нечего: тихий канал при
    здоровых доменах и есть требование этой версии.
    """
    main = [r for r in results if not r.get("known_bad")]
    bad = [r for r in main if not r.get("ok")]
    revived = [r for r in results if r.get("known_bad") and r.get("ok")]
    if not bad and not revived:
        return None
    checked_at = checked_at or now_msk()

    lines = []
    if bad:
        # Предохранители нужны и здесь. Ручной прогон по всему списку
        # с машины, у которой отвалилась сеть, давал сообщение на 263 строки
        # с перечнем 236 адресов - тот самый случай, ради которого их заводили.
        guard = _guards(bad, [], len(main), checked_at, contour_counts,
                        down_lead="Не отвечают")
        if guard is not None:
            return guard + _revived_block(revived)
        lines += ["🔴 Домены: проблемных {} из {}".format(len(bad), len(main)),
                  ""]
        lines += _render_groups(_group(bad, reason_of))
    if revived:
        lines += _revived_lines(revived)
    lines.append(_footer(len(results), checked_at,
                         " · проблемных {}".format(len(bad))))
    return "\n".join(lines) + "\n"


_COLUMN_OF_CONTOUR = {"external": "внешний", "internal": "внутренний"}


def _revived_lines(items: List[dict]) -> List[str]:
    """Сайты из колонки ошибок, которые снова отвечают.

    В предохранители не входят: список того, что пора перенести в таблице,
    нельзя заменять строкой "дело в машине проверки".
    """
    lines = ["🟢 Ожили из списка ошибок: {}".format(len(items))]
    lines += ["  {}".format(item["label"]) for item in items]
    column = _COLUMN_OF_CONTOUR.get(items[0].get("contour"))
    lines.append('Перенесите их в таблице из колонки "ошибки" в "{}".'.format(
        column) if column else
        'Перенесите их в таблице из колонки "ошибки" в колонку контура.')
    lines.append("")
    return lines


def _revived_block(items: List[dict]) -> str:
    return "\n" + "\n".join(_revived_lines(items)) if items else ""


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
    if not checked or len(items) < GUARD_MIN_ITEMS:
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
    revived = [e for e in events if e.get("revived")]
    fell = [e for e in events if e.get("event") == EVENT_DOWN]
    rose = [e for e in events
            if e.get("event") == EVENT_UP and not e.get("revived")]

    guard = _guards(fell, rose, checked, checked_at, contour_counts)
    if guard is not None:
        return guard + _revived_block(revived)

    lines = []
    # Долгие одиночные падения и массовые отдельно: у них разный смысл.
    # Массовое на нынешних данных почти всегда сбой самой машины проверки,
    # и читатель должен видеть это первой строкой, а не вычислять по списку.
    lone = [e for e in fell if e.get("alert") == ALERT_LONE]
    rest = [e for e in fell if e.get("alert") != ALERT_LONE]
    if rest:
        lines.append("🔴 Упали: {}".format(len(rest)))
        if any(e.get("alert") == ALERT_GROUP for e in rest):
            lines.append("Разом, в пределах {} минут. Несколько независимых "
                         "аварий одновременно - редкость,\nначните с машины "
                         "проверки.".format(GROUP_WINDOW_MINUTES))
        lines.append("")
        lines += _render_groups(_group(
            rest, lambda e: e.get("reason") or "причина не определена"))
    if lone:
        lines.append("🔴 Не отвечают дольше {} часов: {}".format(
            LONE_DOWN_HOURS, len(lone)))
        lines.append("")
        for e in lone:
            since = short_stamp(e.get("since"))
            title, plain = _explain(e.get("reason") or "причина не определена")
            lines.append("  {} - {}{}".format(
                e["label"], title,
                " (с {})".format(since) if since else ""))
            if plain:
                lines.append("    {}".format(plain))
        lines.append("")
    if rose:
        lines.append("🟢 Поднялись: {}".format(len(rose)))
        for e in rose:
            since = short_stamp(e.get("since"))
            lines.append("  {}{}".format(
                e["label"],
                " (не отвечал с {})".format(since) if since else ""))
        lines.append("")
    if revived:
        lines += _revived_lines(revived)
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


def build_sheet_down(reason: str, checked_at: datetime = None) -> str:
    checked_at = checked_at or now_msk()
    return ("⚠️ Не удалось прочитать Google-таблицу со списком доменов\n\n"
            "Причина: {}\n\n"
            "Домены не проверяются, пока таблица не прочитается. Повторять это\n"
            "сообщение не буду, напишу, когда таблица снова прочитается.\n\n"
            "{} МСК\n".format(reason, checked_at.strftime("%Y-%m-%d %H:%M")))


def build_sheet_up(since: str = None, checked_at: datetime = None) -> str:
    checked_at = checked_at or now_msk()
    since = short_stamp(since)
    return ("🟢 Google-таблица снова читается, проверка доменов продолжается.\n"
            "{}\n{} МСК\n".format(
                "Не читалась с {}.".format(since) if since else "",
                checked_at.strftime("%Y-%m-%d %H:%M")))


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def send(text: str, webhook_url: str) -> bool:
    """Отправляет готовый текст. Есть ли что и куда слать, решает вызывающий."""
    # Тройные кавычки сохраняют моноширинное выравнивание: без них Mattermost
    # съедает отступы, и группировка по причинам разваливается.
    return send_to_mattermost(webhook_url, "```\n" + text + "```",
                              timeout=MATTERMOST_TIMEOUT)
