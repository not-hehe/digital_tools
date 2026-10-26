import concurrent.futures
import logging
from typing import List, Optional

import requests

from .failures import classify_exception, failure_label, FAIL_OTHER
from ..urls import label_of, normalize
from ..config import (REQUEST_TIMEOUT, MAX_REDIRECTS, MAX_WORKERS, USER_AGENT,
                     TRUST_ENV, HEALTHY_MIN, HEALTHY_MAX)

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
        "failure_kind": None,
        "error": None,
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
    if result.get("failure_kind"):
        return failure_label(result["failure_kind"])
    return "причина не определена"


def _new_session(max_redirects: int) -> requests.Session:
    session = requests.Session()
    session.trust_env = TRUST_ENV
    session.max_redirects = max_redirects
    session.headers["User-Agent"] = USER_AGENT
    return session


def check_domain(url: str, label: str = None, timeout: int = None,
                 max_redirects: int = None) -> dict:
    """Снимает HTTP-ответ по адресу. Наружу исключений не поднимает."""
    timeout = REQUEST_TIMEOUT if timeout is None else timeout
    max_redirects = MAX_REDIRECTS if max_redirects is None else max_redirects

    url = normalize(url)
    result = blank_result(url, label)
    try:
        # Сессия создаётся на каждую проверку: requests.Session не потокобезопасна,
        # общая на пул давала бы гонки на пуле соединений.
        with _new_session(max_redirects) as session:
            # stream=True: читаем заголовки и не качаем тело. Нужен только
            # код ответа, а при 288 прогонах в сутки скачанные страницы —
            # это десятки гигабайт трафика впустую.
            response = session.get(url, timeout=timeout, stream=True)
            response.close()
        result.update({"status_code": response.status_code,
                       "ok": is_healthy(response.status_code)})
    except Exception as e:
        result.update({"failure_kind": classify_exception(e), "error": str(e)})
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
        try:
            result = check_domain(target["url"], target.get("label"),
                                  timeout=timeout)
            result["contour"] = target.get("contour") or "unknown"
            result["known_bad"] = bool(target.get("known_bad"))
            return result
        except Exception as e:
            # Сбой ВНЕ check_domain (битая цель без адреса). Без этого ex.map
            # поднял бы исключение при чтении результатов, и одна битая цель
            # обрушила бы весь прогон.
            logger.warning("Проверка цели %r сорвалась: %s", target, e)
            result = blank_result(str(target))
            result.update({"failure_kind": FAIL_OTHER, "error": str(e)})
            return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        results = list(ex.map(run_one, targets))

    logger.info("Проверено %d, проблемных %d", len(results),
                sum(1 for r in results if not r["ok"]))
    return results
