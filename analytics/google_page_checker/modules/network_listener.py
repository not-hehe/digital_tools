import logging
import re
import time
from typing import Any, Dict, List, Optional, Callable

from playwright.async_api import Page, Request, Response

from .config import GA_COLLECT_PATTERNS, GA_ERROR_MARKERS

logger = logging.getLogger(__name__)

# Типы (kind) пойманных Google-запросов.
KIND_COLLECT = "collect"                  # настоящий хит аналитики
KIND_ERROR_TELEMETRY = "error_telemetry"  # служебный отчёт библиотеки об ошибке
KIND_OTHER = "other_google"               # прочее: загрузка gtm.js, gtag/js и т.п.


class NetworkListener:
    """
    Перехватывает сетевую активность страницы и фиксирует запросы
    к Google Analytics.
    Слушает два события:
      - 'request'  — момент ОТПРАВКИ запроса. Именно здесь запрос
        фиксируется: сама попытка отправить хит — уже сигнал наличия
        аналитики, даже если ответ так и не пришёл (endpoint недоступен,
        запрос заблокирован или прерван);
      - 'response' — момент получения ответа. Используется только для
        дозаполнения HTTP-статуса уже зафиксированного запроса.
        Если ответа не было, в поле 'status' остаётся None.
    Слушатель только собирает и размечает данные. Логика принятия решения в другом месте
    """

    def __init__(self, page: Page, ga_patterns: List[str]):
        self.page = page
        self.ga_patterns = [re.compile(p) for p in ga_patterns]
        self._collect_patterns = [re.compile(p) for p in GA_COLLECT_PATTERNS]
        self._requests: List[dict] = []
        # Ключ — САМ объект Request, а не его id(). Раньше здесь лежал
        # id(request): ссылку на объект мы не держали, Playwright мог его
        # освободить, а CPython — переиспользовать адрес под новый запрос,
        # и HTTP-статус приписывался чужой записи. Объект как ключ заодно
        # удерживает ссылку, так что переиспользования адреса не будет.
        self._by_request: Dict[Any, dict] = {}
        # Запасной индекс по URL: нужен, когда до response.request
        # добраться не удалось (см. _handle_response).
        self._by_url: Dict[str, List[dict]] = {}
        self._request_handler: Optional[Callable] = None
        self._response_handler: Optional[Callable] = None

    def start(self):
        if self._request_handler:
            return
        self._requests.clear()
        self._by_request.clear()
        self._by_url.clear()
        self._request_handler = self._handle_request
        self._response_handler = self._handle_response
        self.page.on("request", self._request_handler)
        self.page.on("response", self._response_handler)

    def stop(self):
        if self._request_handler:
            self.page.remove_listener("request", self._request_handler)
            self._request_handler = None
        if self._response_handler:
            self.page.remove_listener("response", self._response_handler)
            self._response_handler = None

    def _classify_url(self, url: str) -> str:
        """Определяет тип Google-запроса по его URL."""
        if all(marker in url for marker in GA_ERROR_MARKERS):
            return KIND_ERROR_TELEMETRY
        if any(p.search(url) for p in self._collect_patterns):
            return KIND_COLLECT
        return KIND_OTHER

    async def _handle_request(self, request: Request):
        try:
            url = request.url
        except Exception:
            # Объект запроса пришёл в непригодном виде (известный дефект
            # playwright-python 1.40). Улику теряем, но прогон не роняем.
            return
        if not any(p.search(url) for p in self.ga_patterns):
            return
        req = {
            "url": url,
            "status": None,  # заполнится в _handle_response, если ответ придёт
            "method": request.method,
            "timestamp": time.time(),
            "kind": self._classify_url(url),
        }
        try:
            post_data = request.post_data
            if post_data:
                req["post_data"] = post_data
        except Exception:
            pass
        self._requests.append(req)
        try:
            self._by_request[request] = req
        except TypeError:
            # Объект нехешируемый — обойдёмся индексом по URL.
            pass
        self._by_url.setdefault(url, []).append(req)
        logger.debug("Google-запрос (%s): %s", req["kind"], url)

    async def _handle_response(self, response: Response):
        """Дозаполняет HTTP-статус ранее зафиксированного запроса.

        ``response.request`` в playwright-python 1.40 умеет падать с
        ``'dict' object has no attribute '_object'``, если объект запроса
        уже освобождён. В боевом логе это ловилось десяток раз за прогон и
        роняло всю попытку загрузки сайта. Здесь падение локализовано:
        не получилось достать запрос — сопоставляем по URL ответа.
        """
        req = None
        try:
            req = self._by_request.get(response.request)
        except Exception as e:
            logger.debug("response.request недоступен (%s) — "
                         "сопоставляем по URL", e)
        if req is None:
            try:
                url = response.url
            except Exception:
                return
            # Первая запись с этим URL, у которой статуса ещё нет:
            # ответы приходят в том же порядке, что и запросы.
            for candidate in self._by_url.get(url, ()):
                if candidate["status"] is None:
                    req = candidate
                    break
        if req is None:
            return
        try:
            req["status"] = response.status
        except Exception:
            pass

    def get_ga_requests(self) -> List[dict]:
        """Все Google-запросы, КРОМЕ error-телеметрии (прежнее поведение)."""
        return [r for r in self._requests if r["kind"] != KIND_ERROR_TELEMETRY]

    def get_collect_requests(self) -> List[dict]:
        """Только настоящие хиты аналитики."""
        return [r for r in self._requests if r["kind"] == KIND_COLLECT]

    def get_error_requests(self) -> List[dict]:
        """Только служебная телеметрия ошибок (t=error&_e=exc)."""
        return [r for r in self._requests if r["kind"] == KIND_ERROR_TELEMETRY]

    def get_all_requests(self) -> List[dict]:
        """Вообще все пойманные Google-запросы, включая error-телеметрию."""
        return self._requests.copy()
