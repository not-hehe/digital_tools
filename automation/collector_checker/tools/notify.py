#!/usr/bin/env python3
"""Отправить готовый текст в Telegram. Нужен обёртке run.sh.

    venv/bin/python tools/notify.py "текст"
    echo "текст" | venv/bin/python tools/notify.py

Зачем отдельно от main.py: сообщение о том, что прогон НЕ состоялся, не может
отправлять сам прогон, он в этот момент уже упал. Отправляет тот, кто его запускал.

Секреты не заданы: пишем в stderr и выходим нулём, обёртка не должна падать
из-за ненастроенной отправки. Отправка не удалась: код 1.
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.config import TELEGRAM_CHAT_VAR, TELEGRAM_TOKEN_VAR  # noqa: E402


def _load_notifier():
    # По пути, а не через пакет alerting: пакет тянет requests, а здесь venv может
    # быть сломан, и тогда сообщение о сбое ушло бы в никуда.
    spec = importlib.util.spec_from_file_location(
        "collector_notifier", ROOT / "modules" / "alerting" / "notifier.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv) -> int:
    text = " ".join(argv[1:]).strip() if len(argv) > 1 else sys.stdin.read().strip()
    if not text:
        print("notify.py: нечего отправлять", file=sys.stderr)
        return 1
    token = os.environ.get(TELEGRAM_TOKEN_VAR, "")
    chat_id = os.environ.get(TELEGRAM_CHAT_VAR, "")
    if not token or not chat_id:
        print("notify.py: не заданы {} / {}, отправка пропущена".format(
            TELEGRAM_TOKEN_VAR, TELEGRAM_CHAT_VAR), file=sys.stderr)
        return 0
    if not _load_notifier().send_to_telegram(token, chat_id, text):
        print("notify.py: отправить не удалось", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
