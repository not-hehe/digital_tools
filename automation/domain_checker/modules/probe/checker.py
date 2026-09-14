import concurrent.futures
import logging
import time
from typing import List, Optional
from urllib.parse import urljoin

import requests

from .failures import (classify_exception, failure_label, FAIL_REDIRECT_LOOP,
                       FAIL_INVALID_URL, FAIL_OTHER)
from ..urls import label_of, looks_like_url, normalize
from ..config import (REQUEST_TIMEOUT, MAX_REDIRECTS, MAX_WORKERS, USER_AGENT,
                     TRUST_ENV, HEALTHY_MIN, HEALTHY_MAX, now_msk)

logger = logging.getLogger(__name__)


def blank_result(url: str, label: str = None) -> dict:
    """Каркас результата: одинаковый набор ключей для всех исходов."""
    return {
        "url": url,
        "label": label or label_of(url),
        # Проставляется из цели в check_all. Нужна не проверке, а разбору
        # результата: массовый отказ одного контура это отказ маршрута,
        # а не столько же независимых аварий.
        "contour": "unknown",
        "ok": False,
        "status_code": None,
        "final_url": None,
        "redirects": [],
        "elapsed_ms": None,
        "failure_kind": None,
        "failure_label": None,
        "error": None,
        "checked_at": now_msk(),
    }


def is_healthy(status_code: Optional[int]) -> bool:
    return status_code is not None and HEALTHY_MIN <= status_code < HEALTHY_MAX


def reason_of(result: dict) -> str:
    """Человекочитаемая причина: код ответа, если он есть, иначе вид отказа.

    Живёт рядом с моделью результата, а не в отчётах: сообщение о событии
    и сводка о прогоне описывают одно и то же одинаково.
    """
    if result.get("status_code") is not None:
        return "HTTP {}".format(result["status_code"])
    return result.get("failure_label") or "причина не определена"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = TRUST_ENV
    session.headers["User-Agent"] = USER_AGENT
    return session


def check_domain(url: str, label: str = None, timeout: int = None,
                 max_redirects: int = None, session=None) -> dict:
    """Снимает HTTP-ответ по адресу. Наружу исключений не поднимает.

    Цепочка редиректов проходится вручную: при allow_redirects=True requests
    отдаёт только итоговый ответ, а промежуточные адреса нужны в результате.
    """
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    max_redirects = MAX_REDIRECTS if max_redirects is None else max_redirects

    url = normalize(url)
    result = blank_result(url, label)

    if not looks_like_url(url):
        result.update({"failure_kind": FAIL_INVALID_URL,
                       "failure_label": failure_label(FAIL_INVALID_URL),
                       "error": "строка не является адресом сайта"})
        return result

    own_session = session is None
    session = session or _new_session()
    started = time.monotonic()
    current = url
    try:
        for _ in range(max_redirects + 1):
            # stream=True: читаем заголовки и не качаем тело. Нужен только
            # код ответа, а при 288 прогонах в сутки скачанные страницы —
            # это десятки гигабайт трафика впустую.
            response = session.get(current, timeout=timeout,
                                   allow_redirects=False, stream=True)
            response.close()
            if not response.is_redirect:
                result.update({
                    "status_code": response.status_code,
                    "ok": is_healthy(response.status_code),
                    "final_url": current,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
                return result
            current = urljoin(current, response.headers["Location"])
            result["redirects"].append(current)
        result.update({"failure_kind": FAIL_REDIRECT_LOOP,
                       "failure_label": failure_label(FAIL_REDIRECT_LOOP),
                       "final_url": current,
                       "error": f"более {max_redirects} редиректов подряд"})
    except Exception as e:
        kind = classify_exception(e)
        result.update({"failure_kind": kind,
                       "failure_label": failure_label(kind),
                       "error": str(e),
                       "elapsed_ms": int((time.monotonic() - started) * 1000)})
    finally:
        if own_session:
            session.close()
    return result


def check_all(targets: List[dict], max_workers: int = None,
              timeout: int = None) -> List[dict]:
    """Проверяет список целей пулом потоков.

    Порядок результатов повторяет входной список, а не порядок завершения:
    сообщение читают рядом с исходным списком.
    """
    max_workers = MAX_WORKERS if max_workers is None else max_workers
    if not targets:
        return []

    def run_one(target):
        # Сессия создаётся внутри задачи: requests.Session не потокобезопасна,
        # общая на пул давала бы гонки на пуле соединений.
        try:
            with _new_session() as session:
                result = check_domain(target["url"], target.get("label"),
                                      timeout=timeout, session=session)
            result["contour"] = target.get("contour") or "unknown"
            return result
        except Exception as e:
            # Сбой ВНЕ check_domain (битая цель, отказ создания сессии).
            # Без этого ex.map поднял бы исключение при чтении результатов,
            # и одна битая цель обрушила бы весь прогон.
            logger.warning("Проверка цели %r сорвалась: %s", target, e)
            result = blank_result(str(target))
            result.update({"failure_kind": FAIL_OTHER,
                           "failure_label": failure_label(FAIL_OTHER),
                           "error": str(e)})
            return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(run_one, targets))

    logger.info("Проверено %d, проблемных %d", len(results),
                sum(1 for r in results if not r["ok"]))
    return results
