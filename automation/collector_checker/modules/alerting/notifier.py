"""Отправка в Telegram на stdlib: tools/notify.py загружает этот файл по пути и работает
без requests. Адреса перебираются по очереди: IP с заголовком Host, затем имя.
Сертификат при соединении по IP проверяется вручную (без SNI сервер отдаёт
*.telegram.org), отключается TELEGRAM_VERIFY_CERT.
"""

import http.client
import json
import socket
import ssl
import sys
import time
from typing import Callable, List, Optional, Tuple

from modules.config import (TELEGRAM_ENDPOINT_PAUSE, TELEGRAM_HOST, TELEGRAM_IPS,
                            TELEGRAM_TIMEOUT, TELEGRAM_VERIFY_CERT)


class NetworkError(Exception):
    """Нет соединения или ответа; следующий адрес."""


def _err(message: str, token: str) -> None:
    # В тексте ошибок бывает URL с токеном.
    print(message.replace(token, "***") if token else message, file=sys.stderr)


def _parse_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


def _name_matches(cert: dict, host: str) -> bool:
    """Проверка имени по SAN сертификата: точное совпадение или маска *.домен."""
    for kind, name in cert.get("subjectAltName", ()):
        if kind != "DNS":
            continue
        if name == host:
            return True
        if name.startswith("*.") and host.split(".", 1)[1] == name[2:]:
            return True
    return False


def _connection(endpoint: str, timeout: int) -> http.client.HTTPSConnection:
    """По имени стандартный TLS. По IP без SNI: цепочку проверяет ssl, имя проверяется вручную."""
    if endpoint == TELEGRAM_HOST:
        return http.client.HTTPSConnection(endpoint, 443, timeout=timeout)
    context = ssl.create_default_context()
    context.check_hostname = False
    if not TELEGRAM_VERIFY_CERT:
        context.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(endpoint, 443, timeout=timeout, context=context)
    conn.connect()
    if TELEGRAM_VERIFY_CERT and not _name_matches(conn.sock.getpeercert(), TELEGRAM_HOST):
        conn.close()
        raise NetworkError("сертификат на {} не для {} (отключить проверку: CC_TELEGRAM_VERIFY=0)".format(
            endpoint, TELEGRAM_HOST))
    return conn


def _post(endpoint: str, path: str, payload: bytes, timeout: int) -> Tuple[int, bytes]:
    """Один POST. Сетевые ошибки -> NetworkError, любой ответ -> (код, тело)."""
    try:
        conn = _connection(endpoint, timeout)
        conn.request("POST", path, body=payload,
                     headers={"Host": TELEGRAM_HOST, "Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, response.read()
    except NetworkError:
        raise
    except (OSError, socket.timeout, http.client.HTTPException, ssl.SSLError) as e:
        raise NetworkError("{}: {}".format(type(e).__name__, e))


def endpoints() -> List[str]:
    return list(TELEGRAM_IPS) + [TELEGRAM_HOST]


def send_to_telegram(token: str, chat_id: str, text: str, timeout: Optional[int] = None,
                     sleep: Callable[[float], None] = time.sleep) -> bool:
    """True только при HTTP 200 и "ok": true. Любой HTTP-ответ, кроме 429, завершает
    перебор адресов."""
    if not TELEGRAM_VERIFY_CERT:
        _err("Telegram: проверка сертификата выключена (CC_TELEGRAM_VERIFY=0)", "")
    timeout = TELEGRAM_TIMEOUT if timeout is None else timeout
    # strip: .env с CRLF оставляет \r в значениях.
    token, chat_id = token.strip(), str(chat_id).strip()
    path = "/bot{}/sendMessage".format(token)
    payload = json.dumps({"chat_id": chat_id, "text": text,
                          "disable_web_page_preview": True}).encode("utf-8")

    rate_limited = False
    targets = endpoints()
    for index, endpoint in enumerate(targets, start=1):
        try:
            status, raw = _post(endpoint, path, payload, timeout)
        except NetworkError as e:
            _err("Telegram через {} ({}/{}) недоступен: {}".format(endpoint, index, len(targets), e), token)
            if index < len(targets):
                sleep(TELEGRAM_ENDPOINT_PAUSE)
            continue

        body = _parse_body(raw)
        if status == 200 and body.get("ok") is True:
            if index > 1:
                _err("Telegram: ушло через {}".format(endpoint), token)
            return True
        if status == 429 and not rate_limited:
            rate_limited = True
            params = body.get("parameters")
            retry_after = params.get("retry_after", 1) if isinstance(params, dict) else 1
            # retry_after ограничен сверху.
            sleep(min(retry_after, 60))
            try:
                status, raw = _post(endpoint, path, payload, timeout)
            except NetworkError as e:
                _err("Telegram через {} после 429 недоступен: {}".format(endpoint, e), token)
                continue
            body = _parse_body(raw)
            if status == 200 and body.get("ok") is True:
                return True
        _err("Telegram HTTP {}: {}".format(status, body.get("description", "без описания")), token)
        return False

    _err("Telegram недоступен ни по одному из {} адресов".format(len(targets)), token)
    return False


def make_sender(token: str, chat_id: str) -> Callable[[str], bool]:
    def sender(text: str) -> bool:
        return send_to_telegram(token, chat_id, text)
    return sender


def printing_sender(text: str) -> bool:
    print("[сообщение] " + text)
    return True
