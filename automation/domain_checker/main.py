#!/usr/bin/env python3
"""Ручной прогон: список доменов → проверка → текст сообщения в stdout."""

import logging
import sys

from modules.alert import build_alert
from modules.checker import check_all
from modules.config import LOG_FORMAT, LOG_LEVEL
from modules.targets import load_targets

DEFAULT_LIMIT = 15


def main(argv):
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format=LOG_FORMAT, stream=sys.stdout)
    limit = int(argv[1]) if len(argv) > 1 else DEFAULT_LIMIT

    loaded = load_targets(limit=limit)
    print("Целей: {} (отсеяно мусорных строк: {})".format(
        len(loaded["targets"]), len(loaded["skipped"])))
    results = check_all(loaded["targets"])

    text = build_alert(results)
    print()
    if text is None:
        print("Проблем нет — сообщение не отправляется.")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
