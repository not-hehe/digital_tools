#!/usr/bin/env python3
"""Отправить готовый текст в Telegram. Нужен обёртке run.sh.

    venv/bin/python tools/notify.py "текст"
    echo "текст" | venv/bin/python tools/notify.py

Зачем отдельно от main.py: сообщение о том, что прогон НЕ состоялся, не может
отправлять сам прогон, он в этот момент уже упал. Отправляет тот, кто его запускал.

Секреты не заданы: пишем в stderr и выходим нулём, обёртка не должна падать
из-за ненастроенной отправки. Отправка не удалась: код 1.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.config import TELEGRAM_CHAT_VAR, TELEGRAM_TOKEN_VAR
from modules.alerting import send_to_telegram


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
    if not send_to_telegram(token, chat_id, text):
        print("notify.py: отправить не удалось", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
