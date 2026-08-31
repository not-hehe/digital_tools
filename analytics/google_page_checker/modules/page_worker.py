import asyncio
import logging
import re
import time
from typing import List, Optional

from .browser_manager import BrowserManager
from .network_listener import NetworkListener
from .classifier import (classify_site, is_ga_positive, is_universal_analytics,
                         UA_ID_RE)
from .failures import (classify_failure, failure_label, is_retryable,
                       worth_alternate_url, FAIL_INVALID_URL, FAIL_HTTP,
                       FAIL_OTHER)
from .url_variants import normalize, looks_like_url, alternate_urls
from .config import (GA_PATTERNS, HTML_GTM_STRONG_PATTERNS, HTML_GTM_WEAK_PATTERNS,
                     NAV_WAIT_UNTIL, LOAD_STATE_TIMEOUT,
                     NETWORKIDLE_TIMEOUT, MIN_WAIT_AFTER_LOAD,
                     TARGETED_WAIT_TIMEOUT, PROBE_TIMEOUT,
                     PAGE_TIMEOUT, MAX_RETRIES, RETRY_PAUSE,
                     ANALYZE_HTTP_ERRORS, HTTP_TRANSIENT_CODES,
                     STATUS_ERROR, STATUS_BROKEN,
                     BROKEN_CONTAINER_DELETED, BROKEN_LOADS_NO_HITS,
                     BROKEN_NO_RESPONSE, BROKEN_NOT_LOADED,
                     BROKEN_ALIVE_NOT_LOADED, BROKEN_UA_SUNSET,
                     BROKEN_REASON_LABELS)

logger = logging.getLogger(__name__)

# Паттерны компилируются один раз при импорте модуля, а не на каждую страницу.
_HTML_STRONG_RE = [re.compile(p) for p in HTML_GTM_STRONG_PATTERNS]
_HTML_WEAK_RE = [re.compile(p) for p in HTML_GTM_WEAK_PATTERNS]

_SNIPPET_RADIUS = 60          # символов контекста вокруг найденного маркера
_MAX_MATCHES_PER_PATTERN = 3  # защита отчёта от «простыней» повторов

# Сколько РАЗНЫХ вариантов адреса пробуем на один сайт, включая исходный.
# Ограничение страхует от размножения кандидатов на цепочках отказов.
_MAX_URL_VARIANTS = 3


def find_html_markers(html: str) -> List[dict]:
    """
    Ищет в HTML страницы следы GTM/GA.

    Возвращает список словарей:
        - 'marker': найденный фрагмент (например, 'GTM-ABC123');
        - 'snippet': кусок вёрстки вокруг маркера — чтобы в отчёте было
          видно, ГДЕ именно в коде страницы он находится;
        - 'strength': 'strong' — идентификатор собственной установки
          (GTM-/G-/UA-ID); 'weak' — упоминание без ID (голый gtag(,
          URL библиотеки), характерное для чужих скриптов-обвязок.

    Одинаковые значения маркеров не дублируются; по каждому паттерну
    берётся не более _MAX_MATCHES_PER_PATTERN РАЗНЫХ совпадений
    (повторы одного и того же ID лимит не расходуют).
    """
    if not html:
        return []
    markers = []
    seen = set()
    for regex_list, strength in ((_HTML_STRONG_RE, "strong"),
                                 (_HTML_WEAK_RE, "weak")):
        for regex in regex_list:
            taken = 0
            for match in regex.finditer(html):
                if taken >= _MAX_MATCHES_PER_PATTERN:
                    break
                value = match.group(0)
                if value in seen:
                    continue
                seen.add(value)
                taken += 1
                start = max(0, match.start() - _SNIPPET_RADIUS)
                end = min(len(html), match.end() + _SNIPPET_RADIUS)
                snippet = " ".join(html[start:end].split())
                markers.append({"marker": value, "snippet": snippet,
                                "strength": strength})
    return markers


def _has_container_request(requests: List[dict]) -> bool:
    """Был ли среди пойманных запросов заход на googletagmanager.com
    (загрузка контейнера GTM или библиотеки gtag)."""
    return any("googletagmanager.com" in r["url"] for r in requests)


# Опознание идентификатора установки в вёрстке. GTM-контейнер приоритетнее:
# на практике ставят именно его, а UA-тег часто лежит рядом как наследие.
_CONTAINER_ID_RES = [
    re.compile(r"GTM-[A-Z0-9]{4,}"),
    re.compile(r"G-[A-Z0-9]{6,}"),
    UA_ID_RE,
]

# Цели зонда. UA здесь сознательно НЕТ: googletagmanager.com отдаёт
# gtag.js на любой синтаксически валидный идентификатор, поэтому ответ
# 200 на UA-ID не доказывает ровным счётом ничего, а сам Universal
# Analytics отключён с 2023 года.
_PROBE_ID_RES = [
    (re.compile(r"GTM-[A-Z0-9]{4,}"),
     "https://www.googletagmanager.com/gtm.js?id={}"),
    (re.compile(r"G-[A-Z0-9]{6,}"),
     "https://www.googletagmanager.com/gtag/js?id={}"),
]


def extract_container_id(markers: List[dict]) -> Optional[str]:
    """Достаёт идентификатор установки из сильных маркеров вёрстки.

    Маркеры приходят в сыром виде ('id=G-XXXX', "'G-XXXX'"), поэтому ID
    вытаскивается регуляркой. Нужен отчёту: группировка сайтов по одному
    контейнеру показывает, что за сотней «поломанных» сайтов стоит один
    общий шаблон платформы, а не сто независимых установок.

    Если в вёрстке есть и GTM-контейнер, и UA-тег, возвращается GTM:
    живая установка важнее наследия.
    """
    text = " ".join(m["marker"] for m in markers
                    if m.get("strength") == "strong")
    for regex in _CONTAINER_ID_RES:
        match = regex.search(text)
        if match:
            return match.group(0)
    return None


def _extract_probe_target(strong_markers: List[dict]):
    """По сильным маркерам вёрстки строит цель зонда.

    Возвращает (container_id, probe_url) или None, если зондировать нечего
    (в том числе когда в вёрстке только UA-тег — см. _PROBE_ID_RES).
    """
    text = " ".join(m["marker"] for m in strong_markers)
    for regex, url_template in _PROBE_ID_RES:
        match = regex.search(text)
        if match:
            container_id = match.group(0)
            return container_id, url_template.format(container_id)
    return None


async def _probe_container(page, strong_markers: List[dict], url: str) -> dict:
    """Зонд: чекер САМ запрашивает контейнер, найденный в вёрстке.

    Вызывается только когда страница за контейнером не сходила. Запрос
    идёт через browser context request API (те же сетевые настройки, что
    у страницы), но от имени чекера — в network-статистику страницы он
    НЕ попадает. Ошибки зонда прогон не роняют.

    Возвращает словарь:
        - 'container_id': какой ID проверяли;
        - 'url': куда ходили;
        - 'status': HTTP-статус (404/410 = контейнер удалён, 2xx = жив)
          или None, если ответа не было;
        - 'error': текст ошибки, если запрос не удался.
    """
    target = _extract_probe_target(strong_markers)
    if target is None:
        return None
    container_id, probe_url = target
    probe = {"container_id": container_id, "url": probe_url,
             "status": None, "error": None}
    try:
        response = await page.context.request.get(probe_url,
                                                  timeout=PROBE_TIMEOUT)
        probe["status"] = response.status
        logger.info("%s: зонд %s -> HTTP %s", url, probe_url, response.status)
    except Exception as e:
        probe["error"] = str(e)
        logger.warning("%s: зонд %s не удался: %s", url, probe_url, e)
    return probe


def detect_broken_reason(ga_requests: List[dict], probe: dict = None,
                         container_id: str = None) -> str:
    """
    Уточняет причину статуса BROKEN по запросам загрузки контейнера
    (домен googletagmanager.com: gtm.js, gtag/js).

    Приоритет — по силе доказательства. Сначала запросы самой страницы:
        404/410      -> контейнер удалён из GTM (подтверждённая смерть);
        2xx          -> контейнер загрузился, но хитов нет (внутри нет
                        GA-тегов или отправку блокирует согласие);
        None         -> запрос отправлен, ответ не пришёл.
    Затем — отключённая Universal Analytics: если единственный найденный
    идентификатор относится к UA, дальше разбирать нечего. Google
    остановил сбор данных в UA 01.07.2023 и удалил ресурсы 01.07.2024,
    так что тег мёртв независимо от того, что отвечает сеть.
    Наконец, если страница за контейнером не ходила, смотрим зонд:
        зонд 404/410 -> контейнер удалён;
        зонд 2xx     -> контейнер жив, но страница его не запрашивает
                        (похоже, выключен конфигом страницы);
        зонда нет / не ответил -> загрузка не зафиксирована.
    """
    loaders = [r for r in ga_requests if "googletagmanager.com" in r["url"]]
    statuses = [r.get("status") for r in loaders]
    if any(s in (404, 410) for s in statuses):
        return BROKEN_CONTAINER_DELETED
    if any(s is not None and 200 <= s < 300 for s in statuses):
        return BROKEN_LOADS_NO_HITS
    if loaders:
        return BROKEN_NO_RESPONSE
    if is_universal_analytics(container_id):
        return BROKEN_UA_SUNSET
    probe_status = probe.get("status") if probe else None
    if probe_status in (404, 410):
        return BROKEN_CONTAINER_DELETED
    if probe_status is not None and 200 <= probe_status < 300:
        return BROKEN_ALIVE_NOT_LOADED
    return BROKEN_NOT_LOADED


def _blank_result(url: str) -> dict:
    """Каркас результата: один и тот же набор ключей для всех исходов."""
    return {
        "url": url,
        "opened_url": None,      # вариант адреса, которым сайт удалось открыть
        "final_url": None,       # адрес после всех редиректов
        "status": None,
        "has_ga": False,
        "broken_reason": None,
        "http_status": None,
        "http_error": False,     # сервер ответил кодом >= 400
        "container_id": None,
        "ga_requests": [],
        "collect_requests": [],
        "error_requests": [],
        "html_markers": [],
        "probe": None,
        "attempts": 0,
        "tried_urls": [],
        "failure_kind": None,
        "failure_label": None,
        "error": None,
    }


def build_error_result(url: str, error: str, failure_kind: str = None,
                       tried_urls: List[str] = None,
                       attempts: int = 0) -> dict:
    """Результат «сайт открыть не удалось» с разобранной причиной."""
    result = _blank_result(url)
    kind = failure_kind or classify_failure(error)
    result.update({
        "status": STATUS_ERROR,
        "error": error,
        "failure_kind": kind,
        "failure_label": failure_label(kind),
        "tried_urls": list(tried_urls or [url]),
        "attempts": attempts,
    })
    return result


class PageWorker:
    def __init__(self, browser_manager: BrowserManager):
        self.bm = browser_manager

    # ------------------------------------------------------------------
    # Публичный вход
    # ------------------------------------------------------------------

    async def check_pageview_only(self, url: str) -> dict:
        """
        Загружает страницу, собирает сетевые и HTML-признаки Google Analytics
        и классифицирует сайт.

        Порядок работы:
            1. Отсев мусора
            2. Навигация по domcontentloaded, затем мягкое дожидание load.
            3. Ответ сервера кодом >= 400 больше не считается провалом. Результат помечается http_error.
            4. Отказ навигации разбирается по виду (modules/failures.py)
            5. Дальше networkidle + гарантированный минимум,
               поиск маркеров в вёрстке, прицельное дожидание запроса
               к контейнеру, зонд, классификация.
        """
        raw_url = normalize(url)

        if not looks_like_url(raw_url):
            logger.warning("%s: не похоже на адрес сайта — в браузер "
                           "не отправляем", url)
            return build_error_result(
                raw_url, f"Строка не является адресом сайта: {url!r}",
                failure_kind=FAIL_INVALID_URL, tried_urls=[raw_url])

        async with self.bm.page_context() as page:
            queue = [raw_url]
            tried: List[str] = []
            last_error = None
            last_kind = FAIL_OTHER
            total_attempts = 0

            while queue and len(tried) < _MAX_URL_VARIANTS:
                candidate = queue.pop(0)
                if candidate in tried:
                    continue
                if tried:
                    # Перед КАЖДЫМ следующим вариантом гасим страницу:
                    # после неудачной навигации Chromium ещё коммитит свою
                    # chrome-error-страницу и прерывает ею наш goto. Сброс
                    # именно здесь, а не в ветке постановки альтернатив:
                    # к следующему кандидату можно прийти и после отказа,
                    # который сам альтернатив не порождает.
                    await self._reset_page(page)
                tried.append(candidate)

                result, error, kind, attempts = await self._try_url(page,
                                                                    candidate)
                total_attempts += attempts
                if result is not None:
                    result["url"] = raw_url
                    result["opened_url"] = candidate
                    result["tried_urls"] = list(tried)
                    result["attempts"] = total_attempts
                    if candidate != raw_url:
                        logger.info("%s: открылся альтернативным адресом %s",
                                    raw_url, candidate)
                    return result

                last_error, last_kind = error, kind
                if worth_alternate_url(kind):
                    for alternate in alternate_urls(candidate, kind):
                        if alternate not in tried and alternate not in queue:
                            queue.append(alternate)
                            logger.info("%s: %s — пробуем вариант %s",
                                        candidate, failure_label(kind),
                                        alternate)

            logger.error("Не удалось загрузить %s (%s): %s",
                         raw_url, failure_label(last_kind), last_error)
            return build_error_result(raw_url, last_error, last_kind,
                                      tried_urls=tried,
                                      attempts=total_attempts)

    @staticmethod
    async def _reset_page(page) -> None:
        """Уводит страницу на about:blank между вариантами адреса.

        После неудачной навигации Chromium ещё коммитит собственную
        chrome-error-страницу. Если уйти на альтернативный адрес прямо
        сейчас, goto упадёт с "interrupted by another navigation" —
        попытка потрачена впустую. Сначала даём странице устояться,
        затем уводим на about:blank; первый заход может быть прерван
        тем самым коммитом, поэтому пробуем дважды.
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        for attempt in (1, 2):
            try:
                await page.goto("about:blank", timeout=5000)
                return
            except Exception:
                if attempt == 1:
                    await asyncio.sleep(0.3)

    # ------------------------------------------------------------------
    # Одна попытка одного варианта адреса
    # ------------------------------------------------------------------

    async def _try_url(self, page, url: str):
        """Пробует открыть конкретный адрес с повторами.

        Возвращает кортеж (result, error, failure_kind, attempts):
        result заполнен, если страница открылась (в том числе с кодом
        ошибки при ANALYZE_HTTP_ERRORS); иначе result=None, а error и
        failure_kind описывают отказ.
        """
        last_error = None
        last_kind = FAIL_OTHER
        attempt = 0

        while attempt < MAX_RETRIES:
            attempt += 1
            listener = None
            try:
                listener = NetworkListener(page, GA_PATTERNS)
                listener.start()
                logger.info("Переходим на %s (попытка %d)", url, attempt)
                response = await page.goto(url, wait_until=NAV_WAIT_UNTIL,
                                           timeout=PAGE_TIMEOUT)
                main_status = response.status if response else None

                if main_status is not None and main_status >= 400:
                    transient = main_status in HTTP_TRANSIENT_CODES
                    if transient and attempt < MAX_RETRIES:
                        # Бэкенд временно нездоров — добиваем повтором.
                        logger.warning("%s: HTTP %s (попытка %d) — повторяем",
                                       url, main_status, attempt)
                        listener.stop()
                        await asyncio.sleep(RETRY_PAUSE)
                        continue
                    if not ANALYZE_HTTP_ERRORS:
                        listener.stop()
                        return (None,
                                f"Главная страница вернула HTTP {main_status}",
                                FAIL_HTTP, attempt)
                    # Сервер ответил — страница есть, разбираем её как обычно
                    # и помечаем результат кодом ответа.
                    logger.info("%s: HTTP %s — страница есть, разбираем "
                                "содержимое", url, main_status)

                result = await self._analyze_page(page, listener, url,
                                                  main_status)
                return result, None, None, attempt

            except Exception as e:
                last_error = str(e)
                last_kind = classify_failure(last_error)
                logger.warning("Ошибка при попытке %d для %s: %s (%s)",
                               attempt, url, last_error,
                               failure_label(last_kind))
                if listener:
                    try:
                        listener.stop()
                    except Exception:
                        pass
                if not is_retryable(last_kind):
                    # Отказ детерминирован: DNS-запись, мусорный адрес или
                    # требование авторизации за паузу не изменятся.
                    break
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_PAUSE)

        return None, last_error, last_kind, attempt

    # ------------------------------------------------------------------
    # Разбор открытой страницы
    # ------------------------------------------------------------------

    async def _analyze_page(self, page, listener, url: str,
                            main_status) -> dict:
        """Собирает признаки с уже открытой страницы и классифицирует сайт."""
        # 1. Мягко дожидаемся события load. Навигация уже состоялась по
        #    domcontentloaded, поэтому не дождаться load — не потеря
        #    страницы, а всего лишь повод не ждать дольше.
        try:
            await page.wait_for_load_state("load", timeout=LOAD_STATE_TIMEOUT)
        except Exception:
            logger.info("%s: событие load не наступило за %d мс — "
                        "разбираем то, что есть", url, LOAD_STATE_TIMEOUT)
        loaded_at = time.monotonic()

        # 2. networkidle: ждём затишья сети, чтобы аналитика и
        #    инжектируемые скрипты успели отработать. «Шумный» сайт
        #    затишья не даст — идём дальше по таймауту.
        try:
            await page.wait_for_load_state("networkidle",
                                           timeout=NETWORKIDLE_TIMEOUT)
        except Exception:
            logger.info("%s: networkidle не наступил за %d мс — продолжаем",
                        url, NETWORKIDLE_TIMEOUT)

        # 3. Гарантированный минимум после load: платформа инжектит внешние
        #    скрипты через ~3 с ПОСЛЕ load при тихой сети — networkidle
        #    мог наступить раньше инжекции.
        elapsed = time.monotonic() - loaded_at
        if elapsed < MIN_WAIT_AFTER_LOAD:
            await asyncio.sleep(MIN_WAIT_AFTER_LOAD - elapsed)

        # HTML читаем ДО остановки listener: вёрстка может сказать,
        # что стоит подождать ещё (см. ниже).
        html_markers = []
        try:
            html = await page.content()
            html_markers = find_html_markers(html)
        except Exception as e:
            logger.warning("Не удалось прочитать HTML %s: %s", url, e)

        strong_markers = [m for m in html_markers if m["strength"] == "strong"]

        # --- Прицельное дожидание запроса к контейнеру ---
        if strong_markers and not _has_container_request(
                listener.get_ga_requests()):
            try:
                await page.wait_for_event(
                    "response",
                    predicate=lambda r: "googletagmanager.com" in r.url,
                    timeout=TARGETED_WAIT_TIMEOUT)
                logger.info("%s: дождались запроса к контейнеру", url)
            except Exception:
                logger.info("%s: контейнер так и не запрошен за %d мс "
                            "дожидания", url, TARGETED_WAIT_TIMEOUT)

        # Сетевые данные снимаем ПОСЛЕ дожидания — «опоздавший» запрос
        # уже записан listener'ом.
        ga_requests = listener.get_ga_requests()
        collect_requests = listener.get_collect_requests()
        error_requests = listener.get_error_requests()
        listener.stop()

        container_id = extract_container_id(html_markers)

        # --- Зонд состояния контейнера ---
        # Для UA-тегов _extract_probe_target вернёт None, и запрос
        # не уйдёт: его ответ всё равно ничего не значил бы.
        probe = None
        if (strong_markers and not collect_requests
                and not _has_container_request(ga_requests)):
            probe = await _probe_container(page, strong_markers, url)

        status = classify_site(
            has_collect=bool(collect_requests),
            has_strong_html_markers=bool(strong_markers),
            has_error_telemetry=bool(error_requests),
        )
        broken_reason = (detect_broken_reason(ga_requests, probe, container_id)
                         if status == STATUS_BROKEN else None)
        if broken_reason:
            logger.info("%s: причина BROKEN — %s", url,
                        BROKEN_REASON_LABELS.get(broken_reason, broken_reason))

        http_error = main_status is not None and main_status >= 400
        logger.info(
            "%s -> %s (HTTP %s, collect: %d, маркеры: %d сильных / %d слабых, "
            "error-телеметрия: %d)",
            url, status, main_status, len(collect_requests),
            len(strong_markers), len(html_markers) - len(strong_markers),
            len(error_requests),
        )

        try:
            final_url = page.url
        except Exception:
            final_url = url

        result = _blank_result(url)
        result.update({
            "status": status,
            "has_ga": is_ga_positive(status),
            "broken_reason": broken_reason,
            "http_status": main_status,
            "http_error": http_error,
            "container_id": container_id,
            "final_url": final_url,
            "ga_requests": ga_requests,
            "collect_requests": collect_requests,
            "error_requests": error_requests,
            "html_markers": html_markers,
            "probe": probe,
        })
        return result
