#!/usr/bin/env python3
"""Отправить готовый текст в Mattermost. Нужен обёртке run.sh.

    venv/bin/python tools/notify.py "текст"
    echo "текст" | venv/bin/python tools/notify.py

Зачем отдельно от main.py: сообщение о том, что прогон НЕ состоялся, не может
отправлять сам прогон - он в этот момент уже упал, а то и не дошёл до импорта.
Отправляет тот, кто его запускал.

Вебхук не задан - молча выходим с нулём: обёртка не должна падать из-за того,
что отправка не настроена.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.alerting import send_to_mattermost
from modules.config import MATTERMOST_TIMEOUT, MATTERMOST_WEBHOOK_URL


def main(argv) -> int:
    text = " ".join(argv[1:]).strip() if len(argv) > 1 else sys.stdin.read().strip()
    if not text:
        print("notify.py: нечего отправлять", file=sys.stderr)
        return 1
    if not MATTERMOST_WEBHOOK_URL:
        print("notify.py: вебхук не задан (DC_MATTERMOST_WEBHOOK), "
              "отправка пропущена", file=sys.stderr)
        return 0
    ok = send_to_mattermost(MATTERMOST_WEBHOOK_URL, text,
                            timeout=MATTERMOST_TIMEOUT)
    if not ok:
        print("notify.py: отправить не удалось", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
