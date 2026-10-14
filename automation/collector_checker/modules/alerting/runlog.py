import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from ..config import LOG_KEEP_DAYS, RUNLOG_FILE, now_msk
from ..probe import reason_of

_FMT = "%Y-%m-%d %H:%M:%S"
_TRIM_SLACK = timedelta(hours=1)
_DAILY = re.compile(r"collector_checker_(\d{4}-\d{2}-\d{2})\.log")


def attempts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "попытка"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "попытки"
    return "попыток"


def _attempts(r: dict) -> str:
    return "{} {}".format(len(r["attempts"]), attempts_word(len(r["attempts"])))


def run_line(results: List[dict]) -> str:
    failing = [r for r in results if not r["ok"]]
    # Цели, ответившие не с первой попытки.
    flaky = [r for r in results if r["ok"] and len(r["attempts"]) > 1]
    line = "{} run checked={} down={}".format(
        now_msk().strftime(_FMT), len(results), len(failing))
    parts = ["{} {} ({})".format(r["name"], reason_of(r), _attempts(r)) for r in failing]
    parts += ["{} ok ({})".format(r["name"], _attempts(r)) for r in flaky]
    if parts:
        line += " | " + ", ".join(parts)
    return line


def event_lines(events: List[dict]) -> List[str]:
    stamp = now_msk().strftime(_FMT)
    return ["{} event {} {} since {} {}".format(
        stamp, e["event"], e["name"], e["since"].strftime(_FMT), e["reason"])
        for e in events]


def append(lines: List[str], path: Optional[Path] = None, now: Optional[datetime] = None,
           keep_days: Optional[int] = None) -> bool:
    """False при ошибке записи; прогон продолжается."""
    path = RUNLOG_FILE if path is None else path
    keep_days = LOG_KEEP_DAYS if keep_days is None else keep_days
    if not lines:
        return True
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
    except OSError as e:
        print("журнал не записан ({}): {}".format(path, e), file=sys.stderr)
        return False
    _trim(path, now_msk() if now is None else now, keep_days)
    return True


def _stamp(line: str) -> Optional[datetime]:
    try:
        return datetime.strptime(line[:19], _FMT)
    except ValueError:
        return None


def _trim(path: Path, now: datetime, keep_days: int) -> None:
    cutoff = now - timedelta(days=keep_days)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        # Запас в час: иначе файл переписывался бы каждую минуту. Первая строка без метки
        # не должна вечно блокировать обрезку, поэтому смотрим на первую с меткой.
        first = next((t for t in map(_stamp, lines) if t is not None), None)
        if first is None or first >= cutoff - _TRIM_SLACK:
            return
        # Срез идёт до первой свежей строки: строки без метки среди старых уходят с ними.
        keep_from = next((i for i, line in enumerate(lines)
                          if (_stamp(line) or datetime.min) >= cutoff), len(lines))
        # Строка без метки не может быть «свежей»: после первой свежей не трогаем ничего.
        # Через .tmp, как state.txt: сбой посреди перезаписи не теряет журнал.
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines[keep_from:])
        os.replace(tmp, path)
    except (OSError, UnicodeDecodeError) as e:
        print("журнал не подрезан ({}): {}".format(path, e), file=sys.stderr)


def prune_daily_logs(logs_dir: Path, today: Optional[date] = None,
                     keep_days: Optional[int] = None) -> int:
    """Удаляет суточные логи старше срока; чужие файлы не трогает. Возвращает число удалённых."""
    today = date.today() if today is None else today
    keep_days = LOG_KEEP_DAYS if keep_days is None else keep_days
    cutoff = today - timedelta(days=keep_days)
    removed = 0
    try:
        names = sorted(os.listdir(str(logs_dir)))
    except FileNotFoundError:
        return 0   # папки ещё нет - нечего подрезать
    except OSError as e:
        print("логи не подрезаны ({}): {}".format(logs_dir, e), file=sys.stderr)
        return 0
    for name in names:
        m = _DAILY.fullmatch(name)
        if m is None:
            continue
        try:
            day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        try:
            os.remove(os.path.join(str(logs_dir), name))
            removed += 1
        except OSError as e:
            print("лог не удалён ({}): {}".format(name, e), file=sys.stderr)
    return removed


def sent_line(batch: dict) -> str:
    return "{} sent {} {}".format(now_msk().strftime(_FMT), batch["kind"], ", ".join(batch["names"]))


def unsent_line(names: List[str]) -> str:
    return "{} unsent {} (повтор в следующем прогоне)".format(
        now_msk().strftime(_FMT), ", ".join(names))
