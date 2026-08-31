"""Текст сообщения для Mattermost: только проблемные домены."""

from collections import OrderedDict
from datetime import datetime
from typing import Dict, List, Optional

from .config import MSK


def problems(results: List[dict]) -> List[dict]:
    return [r for r in results if not r.get("ok")]


def _reason_of(result: dict) -> str:
    """Заголовок группы: код ответа, если он есть, иначе вид отказа."""
    if result.get("status_code") is not None:
        return "HTTP {}".format(result["status_code"])
    return result.get("failure_label") or "причина не определена"


def group_by_reason(results: List[dict]) -> "OrderedDict[str, List[dict]]":
    """Группы по причине, крупные сверху: чинить начинают с массового."""
    buckets: Dict[str, List[dict]] = {}
    for r in results:
        buckets.setdefault(_reason_of(r), []).append(r)
    return OrderedDict(sorted(buckets.items(),
                              key=lambda kv: (-len(kv[1]), kv[0])))


def build_alert(results: List[dict],
                checked_at: datetime = None) -> Optional[str]:
    """Текст сообщения либо None, если проблемных нет.

    None — сигнал вызывающему, что отправлять нечего: тихий канал при
    здоровых доменах и есть требование этой версии.
    """
    bad = problems(results)
    if not bad:
        return None
    checked_at = checked_at or datetime.now(tz=MSK).replace(tzinfo=None)

    lines = ["🔴 Домены: проблемных {} из {}".format(len(bad), len(results)),
             ""]
    for reason, items in group_by_reason(bad).items():
        lines.append("{} — {}".format(reason, len(items)))
        for r in items:
            lines.append("  {}".format(r["label"]))
        lines.append("")
    lines.append("Проверено {} · проблемных {} · {} МСК".format(
        len(results), len(bad), checked_at.strftime("%Y-%m-%d %H:%M")))
    return "\n".join(lines) + "\n"
