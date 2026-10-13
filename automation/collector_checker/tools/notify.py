#!/usr/bin/env python3
"""Отправка готового текста в Telegram; используется run.sh для сообщений о сбое прогона.

    venv/bin/python tools/notify.py "текст"
    echo "текст" | venv/bin/python tools/notify.py

Без секретов: код 0 и строка в stderr. Ошибка отправки: код 1.
"""

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules.config import TELEGRAM_CHAT_VAR, TELEGRAM_TOKEN_VAR  # noqa: E402


def _load_notifier():
    # Загрузка по пути, минуя пакет alerting с зависимостью от requests.
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
