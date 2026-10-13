import sys
from pathlib import Path
from typing import List, Optional

from ..config import RUNLOG_FILE, RUNLOG_MAX_LINES, now_msk
from ..probe import reason_of

_FMT = "%Y-%m-%d %H:%M:%S"


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


def append(lines: List[str], path: Optional[Path] = None,
           max_lines: Optional[int] = None) -> bool:
    """False при ошибке записи; прогон продолжается."""
    path = RUNLOG_FILE if path is None else path
    max_lines = RUNLOG_MAX_LINES if max_lines is None else max_lines
    if not lines:
        return True
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))
    except OSError as e:
        print("журнал не записан ({}): {}".format(path, e), file=sys.stderr)
        return False
    _trim(path, max_lines)
    return True


def _trim(path: Path, max_lines: int) -> None:
    if max_lines <= 0:
        return
    # Запас, чтобы не переписывать файл на каждом прогоне.
    slack = max(1, max_lines // 10)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= max_lines + slack:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-max_lines:])
    except OSError as e:
        print("журнал не подрезан ({}): {}".format(path, e), file=sys.stderr)


def unsent_line(names: List[str]) -> str:
    return "{} unsent {} (повтор в следующем прогоне)".format(
        now_msk().strftime(_FMT), ", ".join(names))
