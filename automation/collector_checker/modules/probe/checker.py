"""GET на коллектор с повторами внутри прогона."""

import concurrent.futures
import time
from typing import List, Optional

import requests

from ..config import (ATTEMPT_DEADLINE, ATTEMPTS, HEALTHY_CODE, MAX_WORKERS, REQUEST_TIMEOUT,
                     RETRY_PAUSE, USER_AGENT, now_msk)
from .failures import FAIL_OTHER, FAIL_TIMEOUT, classify_exception


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    # Прокси из окружения, если его зададут ради Telegram, увёл бы и проверки
    # коллекторов за рубеж, а оттуда они не отвечают (S-02). Коллекторы всегда напрямую.
    session.trust_env = False
    return session


def _attempt(session: requests.Session, url: str, timeout: int) -> dict:
    started = time.monotonic()
    try:
        # stream=True и close(): нужен код, а не gif-пиксель в теле.
        response = session.get(url, timeout=(timeout, timeout), allow_redirects=False, stream=True)
        response.close()
        return {"status_code": response.status_code, "failure_kind": None,
                "error": None, "elapsed_ms": int((time.monotonic() - started) * 1000)}
    except requests.RequestException as e:
        return {"status_code": None, "failure_kind": classify_exception(e),
                "error": str(e), "elapsed_ms": int((time.monotonic() - started) * 1000)}


def check_collector(collector: dict, *, timeout: Optional[int] = None,
                    attempts: Optional[int] = None, pause: Optional[float] = None,
                    sleep=time.sleep, session: Optional[requests.Session] = None) -> dict:
    """Наружу исключений не поднимает. `sleep` параметром - чтобы тесты не ждали."""
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    attempts = ATTEMPTS if attempts is None else attempts
    pause = RETRY_PAUSE if pause is None else pause

    result = {"name": collector["name"], "url": collector["url"], "ok": False,
              "status_code": None, "failure_kind": None, "error": None,
              "attempts": [], "checked_at": now_msk()}
    own_session = session is None
    session = session or _new_session()
    try:
        for number in range(1, attempts + 1):
            began = time.monotonic()
            attempt = _attempt(session, collector["url"], timeout)
            overran = time.monotonic() - began > ATTEMPT_DEADLINE
            result["attempts"].append(
                {k: attempt[k] for k in ("status_code", "failure_kind", "elapsed_ms")})
            result.update({"status_code": attempt["status_code"],
                           "failure_kind": attempt["failure_kind"],
                           "error": attempt["error"]})
            if overran:
                result.update({"status_code": None, "failure_kind": FAIL_TIMEOUT,
                               "error": "попытка длилась дольше {} с".format(ATTEMPT_DEADLINE)})
                break
            if attempt["status_code"] == HEALTHY_CODE:
                result["ok"] = True
                break
            if number < attempts:
                sleep(pause)
    finally:
        if own_session:
            session.close()
    return result


def check_all(collectors: List[dict], max_workers: Optional[int] = None, **kwargs) -> List[dict]:
    """Порядок результатов повторяет порядок конфига, а не порядок завершения."""
    max_workers = MAX_WORKERS if max_workers is None else max_workers
    if not collectors:
        return []

    def run_one(collector):
        # Сессия на задачу: requests.Session не потокобезопасна.
        try:
            with _new_session() as session:
                return check_collector(collector, session=session, **kwargs)
        except Exception as e:  # сбой вне check_collector не должен ронять прогон
            return {"name": collector.get("name"), "url": collector.get("url"), "ok": False,
                    "status_code": None, "failure_kind": FAIL_OTHER, "error": str(e),
                    "attempts": [], "checked_at": now_msk()}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(run_one, collectors))


def reason_of(result: dict) -> str:
    if result.get("status_code") is not None:
        return "HTTP {}".format(result["status_code"])
    return result.get("failure_kind") or "причина не определена"
