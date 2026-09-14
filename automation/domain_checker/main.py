#!/usr/bin/env python3
"""Один прогон проверки доменов: список, обход, состояние, отчёт в Mattermost.

Запускается по расписанию через `run.sh` - никакого планировщика внутри нет.
Код возврата: 0 - прогон состоялся, 1 - прогон не состоялся или его результат
не сохранён. Ненулевой код это единственный внешний признак того, что чекер
работает вхолостую, поэтому наружу он выставляется честно.

Полный прогон помнит состояние между запусками и шлёт сообщение только
на смену статуса. Выборка (`--limit N`) состояние не трогает вовсе и печатает
сводку о том, что не отвечает сейчас: иначе ручная проверка пяти адресов
объявила бы поднявшимися все остальные домены списка.
"""

import argparse
import logging
import sys

from modules.alerting import (EVENT_DOWN, EVENT_UP, apply_results,
                              build_alert, build_event_alert, build_heartbeat,
                              due_for_heartbeat, load_state, mark_heartbeat,
                              save_state, send)
from modules.config import (INPUT_FILE, LOG_FORMAT, LOG_LEVEL,
                            MATTERMOST_WEBHOOK_URL, STATE_FILE)
from modules.probe import check_all
from modules.sources import EmptyTargetList, load_targets

logger = logging.getLogger(__name__)


def _non_negative(value: str) -> int:
    """Отрицательный лимит молча срезал бы конец списка вместо ошибки."""
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(
            "лимит не может быть отрицательным: {}".format(number))
    return number


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Проверка доступности доменов, один прогон.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--limit", type=_non_negative, default=0, metavar="N",
        help="проверить только первые N адресов; такая выборка не трогает "
             "состояние. 0 - весь список, боевой режим")
    parser.add_argument(
        "--input", default=None, metavar="ФАЙЛ",
        help="откуда взять список, по умолчанию {}".format(INPUT_FILE.name))
    parser.add_argument(
        "--no-notify", action="store_true",
        help="не отправлять в Mattermost, даже если вебхук задан")
    return parser.parse_args(argv)


def setup_logging():
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format=LOG_FORMAT, stream=sys.stdout)
    # На каждый домен с несовпадающим сертификатом urllib3 пишет предупреждение
    # в семь сотен символов. При 288 прогонах в сутки это основной объём лога,
    # а на решение "жив или нет" оно не влияет: сертификаты мы не проверяем.
    logging.getLogger("urllib3").setLevel(logging.ERROR)


def _report_sending(text, no_notify: bool) -> bool:
    """Печатает, что стало с отправкой, и отправляет, если есть что и куда.

    Про отправку говорим вслух в любом случае: молчание после сводки
    с проблемами неотличимо от неудавшейся отправки.
    """
    if text is None:
        print("Сообщать не о чем - в канал ничего не уходит.")
        return False
    if no_notify:
        print("Отправка отключена флагом --no-notify.")
        return False
    if not MATTERMOST_WEBHOOK_URL:
        print("Вебхук не задан (DC_MATTERMOST_WEBHOOK), отправка пропущена.")
        return False
    if send(text, MATTERMOST_WEBHOOK_URL):
        print("Отправлено в Mattermost.")
        return True
    print("Отправить в Mattermost не удалось, подробности в логе выше.")
    return False


def run_sample(loaded: dict, no_notify: bool) -> int:
    """Выборка: сводка о текущих проблемах, состояние не трогаем."""
    results = check_all(loaded["targets"])
    text = build_alert(results, contour_counts=loaded["counts"])
    print()
    if text is not None:
        print(text)
    print("Выборка {} адресов: состояние не трогаю.".format(len(results)))
    _report_sending(text, no_notify)
    return 0


def run_full(loaded: dict, no_notify: bool) -> int:
    """Боевой прогон: состояние между запусками и сообщение на смену статуса."""
    results = check_all(loaded["targets"])

    problems_count = sum(1 for r in results if not r.get("ok"))

    state = load_state()
    state, events = apply_results(state, results)

    # Признак жизни считаем до сохранения, чтобы отметка легла в тот же файл.
    beat = due_for_heartbeat(state)
    if beat:
        mark_heartbeat(state)
    saved = save_state(state)

    text = build_event_alert(events, len(results),
                             contour_counts=loaded["counts"])
    print()
    if text is not None:
        print(text)
    notified = _report_sending(text, no_notify)

    if beat:
        # Уходит отдельным сообщением, а не строкой в событийном: раз в сутки
        # это одно лишнее сообщение, зато его формат не зависит от того,
        # случились ли события.
        _report_sending(build_heartbeat(len(results), problems_count),
                        no_notify)

    print("Проверено {}, проблемных {}, упало {}, поднялось {}, "
          "отправлено {}".format(
              len(results), problems_count,
              sum(1 for e in events if e["event"] == EVENT_DOWN),
              sum(1 for e in events if e["event"] == EVENT_UP),
              "да" if notified else "нет"))

    # Проверка идёт после отправки: события этого прогона уже посчитаны,
    # и терять их из-за недоступного диска незачем. А дальше - ненулевой код.
    # Несохранённое состояние означает, что счётчик отказов начнёт следующий
    # прогон с нуля и до CONFIRM_FAILS не дойдёт никогда: событий не будет
    # вообще, канал замолчит, и это неотличимо от исправной работы.
    if not saved:
        logger.error("Состояние не сохранено (%s): следующий прогон начнёт "
                     "с чистого листа, подтверждение падений не сработает",
                     STATE_FILE)
        return 1
    return 0


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    setup_logging()

    try:
        loaded = load_targets(limit=args.limit, path=args.input)
    except (EmptyTargetList, FileNotFoundError) as e:
        # Трассировка стека здесь ничего не добавляет к тому, что файла нет
        # или он пуст, а код возврата и так скажет вызывающему всё нужное.
        logger.error("Список не загружен: %s", e)
        return 1

    counts = loaded["counts"]
    print("Целей: {} (внутренних {}, внешних {}, без метки {}), "
          "отсеяно мусорных строк: {}".format(
              len(loaded["targets"]), counts["internal"], counts["external"],
              counts["unknown"], len(loaded["skipped"])))

    if args.limit:
        return run_sample(loaded, args.no_notify)
    return run_full(loaded, args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
