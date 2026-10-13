"""Отправка в Telegram на stdlib: у проекта нет причин тянуть ради одного POST requests."""

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Callable

from ..config import TELEGRAM_API


def _err(message: str, token: str) -> None:
    # Исключения urllib несут полный URL, а в нём токен: режем всё, что печатаем.
    if token:
        message = message.replace(token, "***")
    print(message, file=sys.stderr)


def _description(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def send_to_telegram(token: str, chat_id: str, text: str, timeout: int = 10,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
    # .env с CRLF оставляет \r в значениях; с ним URL невалиден, а токен в тексте
    # ошибки записан через repr и не вырезается.
    token, chat_id = token.strip(), str(chat_id).strip()
    url = "{}/bot{}/sendMessage".format(TELEGRAM_API, token)
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "disable_web_page_preview": True}).encode("utf-8")

    # Ровно две попытки: повтор только после 429, дальше лимит это уже не наш случай.
    for attempt in (1, 2):
        request = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as e:
            try:
                body = _description(e.read())
            except Exception:
                body = {}
            if e.code == 429 and attempt == 1:
                params = body.get("parameters")
                retry_after = params.get("retry_after", 1) if isinstance(params, dict) else 1
                # Telegram может попросить подождать и час; прогон столько ждать не должен.
                retry_after = min(retry_after, 60)
                _err("Telegram 429, повтор через {} с".format(retry_after), token)
                sleep(retry_after)
                continue
            _err("Telegram HTTP {}: {}".format(
                e.code, body.get("description", "без описания")), token)
            return False
        except Exception as e:
            _err("Telegram недоступен: {}: {}".format(type(e).__name__, e), token)
            return False

        body = _description(raw)
        if status == 200 and body.get("ok") is True:
            return True
        _err("Telegram ответил без ok: HTTP {}, {}".format(
            status, body.get("description", "тело не разобрано")), token)
        return False
    return False


def make_sender(token: str, chat_id: str) -> Callable[[str], bool]:
    def sender(text: str) -> bool:
        return send_to_telegram(token, chat_id, text)
    return sender


def printing_sender(text: str) -> bool:
    print("[сообщение] " + text)
    return True
