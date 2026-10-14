#!/usr/bin/env python3
"""Один прогон: список коллекторов, GET на каждый, сравнение с прошлым состоянием,
сообщения. Планировщика внутри нет, расписание даёт cron через run.sh.

Код возврата: 0 прогон состоялся, 1 не состоялся или результат не сохранён,
2 ошибка в аргументах.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional

from modules.alerting import (apply_results, attempts_word, delivered, drop_stale, event_lines,
                              load_state, load_timer, make_sender, plan_batches,
                              printing_sender, prune_daily_logs, run_line, runlog_append, save_state,
                              save_timer, send_batches, sent_line, settle, timer_path,
                              unsent_line)
from modules.config import (ATTEMPTS, COLLECTORS_FILE, LOGS_DIR, REQUEST_TIMEOUT, RETRY_PAUSE,
                            RUNLOG_FILE, STATE_FILE, TELEGRAM_CHAT_VAR, TELEGRAM_TOKEN_VAR,
                            now_msk)
from modules.probe import check_all, reason_of
from modules.sources import BadCollectorLine, EmptyCollectorList, load_collectors


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Проверка коллекторов, один прогон.")
    parser.add_argument("--no-notify", action="store_true",
                        help="печатать сообщения вместо отправки")
    parser.add_argument("--collectors", default=None, metavar="ФАЙЛ",
                        help="список коллекторов, по умолчанию {}".format(COLLECTORS_FILE))
    parser.add_argument("--state", default=None, metavar="ФАЙЛ",
                        help="файл состояния, по умолчанию {}".format(STATE_FILE))
    parser.add_argument("--runlog", default=None, metavar="ФАЙЛ",
                        help="журнал прогонов, по умолчанию {}".format(RUNLOG_FILE))
    parser.add_argument("--logs", default=LOGS_DIR, metavar="ДИР", type=Path,
                        help="папка суточных логов, по умолчанию {}".format(LOGS_DIR))
    return parser.parse_args(argv)


def choose_sender(no_notify: bool) -> Optional[Callable[[str], bool]]:
    """None, если отправка включена, а секреты не заданы; проверяется до запросов."""
    if no_notify:
        return printing_sender
    # strip: .env с CRLF оставляет \r в значениях.
    values = {name: (os.environ.get(name) or "").strip()
              for name in (TELEGRAM_TOKEN_VAR, TELEGRAM_CHAT_VAR)}
    missing = [name for name, value in values.items() if not value]
    if missing:
        print("не заданы переменные окружения: {}. Для прогона без отправки "
              "есть --no-notify".format(", ".join(missing)), file=sys.stderr)
        return None
    return make_sender(values[TELEGRAM_TOKEN_VAR], values[TELEGRAM_CHAT_VAR])


def run(args: argparse.Namespace) -> int:
    sender = choose_sender(args.no_notify)
    if sender is None:
        return 1
    prune_daily_logs(args.logs)
    try:
        collectors = load_collectors(args.collectors)
    except (EmptyCollectorList, BadCollectorLine, FileNotFoundError,
            IsADirectoryError, PermissionError) as e:
        print("список не загружен: {}".format(e), file=sys.stderr)
        return 1
    except UnicodeDecodeError as e:
        print("список не загружен: файл не в UTF-8 ({}), сохраните его в UTF-8".format(
            e.reason), file=sys.stderr)
        return 1

    previous = load_state(args.state)
    timer_file = timer_path(args.state or STATE_FILE)
    timer = drop_stale(load_timer(timer_file), previous)
    # Отметки таймера считаются по минуте запуска, а не по концу проверок.
    now = now_msk()
    # Результаты печатаются после всех попыток; строка показывает, что прогон идёт.
    print("Проверяю {} коллекторов, при неответе до {} с...".format(
        len(collectors), ATTEMPTS * REQUEST_TIMEOUT + (ATTEMPTS - 1) * RETRY_PAUSE), flush=True)
    results = check_all(collectors)
    for r in results:
        print("{:<4} {:<5} {:<11} {} {}, {} мс".format(
            r["name"], "ok" if r["ok"] else "down", reason_of(r),
            len(r["attempts"]), attempts_word(len(r["attempts"])),
            r["attempts"][-1]["elapsed_ms"] if r["attempts"] else "-"))

    state, events = apply_results(previous, results, now)
    batches = plan_batches(state, events, timer, now)
    unsent = send_batches(batches, sender)
    # Недоставленное отправится в следующем прогоне. Сбой канала не даёт код 1.
    state, timer = settle(state, previous, timer, batches, unsent, now)
    if not batches:
        print("Сообщать не о чем.")
    lines = [run_line(results)] + event_lines(events)
    lines += [sent_line(b) for b in batches if delivered(b, unsent)]
    if unsent:
        lines.append(unsent_line(unsent))
        print("не отправлено: {}; повтор в следующем прогоне".format(", ".join(unsent)),
              file=sys.stderr)

    if not runlog_append(lines, path=args.runlog, now=now):
        print("журнал не записан", file=sys.stderr)

    # Таймер пишется вторым: без сохранённого state.txt он бы указывал на чужое прошлое.
    saved = save_state(state, args.state) and save_timer(timer, timer_file)
    print("Проверено {}, лежит {}, сообщений {}, не отправлено {}".format(
        len(results), sum(1 for r in results if not r["ok"]), len(batches), len(unsent)))
    return 0 if saved else 1


def main(argv: Optional[List[str]] = None) -> int:
    return run(parse_args(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
