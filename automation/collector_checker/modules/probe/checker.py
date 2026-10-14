"""GET на коллектор с повторами внутри прогона."""

import concurrent.futures
import queue
import threading
import time
from typing import List, Optional

import requests

from ..config import (ATTEMPT_DEADLINE, ATTEMPTS, HEALTHY_CODE, REQUEST_TIMEOUT,
                     RETRY_PAUSE, USER_AGENT, now_msk)
from .failures import FAIL_OTHER, FAIL_TIMEOUT, classify_exception


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    # Прокси из окружения не используется: коллекторы доступны только напрямую.
    session.trust_env = False
    return session


def _attempt(session: requests.Session, url: str, timeout: int) -> dict:
    started = time.monotonic()
    try:
        # stream=True: нужен только код ответа. Таймаут на соединение и на ожидание байтов.
        response = session.get(url, timeout=(timeout, timeout), allow_redirects=False, stream=True)
        response.close()
        return {"status_code": response.status_code, "failure_kind": None,
                "error": None, "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except requests.RequestException as e:
        return {"status_code": None, "failure_kind": classify_exception(e),
                "error": str(e), "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _attempt_capped(session: requests.Session, url: str, timeout: int, deadline: float) -> dict:
    """Попытка в отдельном потоке с пределом по времени: таймаут requests не ограничивает
    запрос целиком. При превышении поток остаётся без ожидания."""
    box = queue.Queue(maxsize=1)
    worker = threading.Thread(target=lambda: box.put(_attempt(session, url, timeout)), daemon=True)
    worker.start()
    try:
        return box.get(timeout=deadline)
    except queue.Empty:
        return {"status_code": None, "failure_kind": FAIL_TIMEOUT,
                "error": "попытка длилась дольше {} с, оборвана".format(deadline),
                "elapsed_ms": int(deadline * 1000), "capped": True}


def check_collector(collector: dict, *, timeout: Optional[int] = None,
                    attempts: Optional[int] = None, pause: Optional[float] = None,
                    deadline: Optional[float] = None, sleep=time.sleep,
                    session: Optional[requests.Session] = None) -> dict:
    """Исключений наружу не поднимает. sleep подменяется в тестах."""
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    attempts = ATTEMPTS if attempts is None else attempts
    pause = RETRY_PAUSE if pause is None else pause
    deadline = ATTEMPT_DEADLINE if deadline is None else deadline

    healthy = tuple(collector.get("healthy") or (HEALTHY_CODE,))
    result = {"name": collector["name"], "url": collector["url"], "ok": False,
              "status_code": None, "failure_kind": None, "error": None,
              "attempts": [], "checked_at": now_msk()}
    own_session = session is None
    session = session or _new_session()
    try:
        for number in range(1, attempts + 1):
            attempt = _attempt_capped(session, collector["url"], timeout, deadline)
            result["attempts"].append(
                {k: attempt[k] for k in ("status_code", "failure_kind", "elapsed_ms")})
            result.update({"status_code": attempt["status_code"],
                           "failure_kind": attempt["failure_kind"],
                           "error": attempt["error"]})
            if attempt.get("capped"):
                break
            if attempt["status_code"] in healthy:
                result["ok"] = True
                break
            if number < attempts:
                sleep(pause)
    finally:
        if own_session:
            session.close()
    return result


def check_all(collectors: List[dict], max_workers: Optional[int] = None, **kwargs) -> List[dict]:
    """Результаты в порядке конфига. По умолчанию поток на цель: глухие цели
    ждут таймаут одновременно, и прогон укладывается в одну попытку."""
    if not collectors:
        return []
    max_workers = len(collectors) if max_workers is None else max_workers

    def run_one(collector):
        # Сессия на поток: Session не потокобезопасна.
        try:
            with _new_session() as session:
                return check_collector(collector, session=session, **kwargs)
        except Exception as e:  # ошибка одной цели не прерывает остальные
            return {"name": collector.get("name"), "url": collector.get("url"), "ok": False,
                    "status_code": None, "failure_kind": FAIL_OTHER, "error": str(e),
                    "attempts": [], "checked_at": now_msk()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(run_one, collectors))


def reason_of(result: dict) -> str:
    # Причина для журнала и вывода; в сообщения не попадает.
    if result.get("status_code") is not None:
        return "HTTP {}".format(result["status_code"])
    return result.get("failure_kind") or "причина не определена"
