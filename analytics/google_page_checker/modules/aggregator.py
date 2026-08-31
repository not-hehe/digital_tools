import logging
from typing import Dict, List

from .config import STATUS_ERROR

logger = logging.getLogger(__name__)


def has_network_evidence(result: Dict) -> bool:
    """Сетевая улика СО СТРАНИЦЫ — критерий «Google обнаружено»
    мини-отчёта: запрос загрузки контейнера/тега (googletagmanager.com:
    gtm.js?id=..., gtag/js?id=...) или collect-хит аналитики."""
    if result.get("collect_requests"):
        return True
    return any("googletagmanager.com" in q.get("url", "")
               for q in (result.get("ga_requests") or []))


def has_strong_markers(result: Dict) -> bool:
    """Есть ли в вёрстке идентификатор собственной установки GA/GTM."""
    return any(m.get("strength") == "strong"
               for m in (result.get("html_markers") or []))


def _is_error(result: Dict) -> bool:
    """Прогон по сайту закончился ошибкой (страница не открылась)."""
    status = result.get("status")
    if status:
        return status == STATUS_ERROR
    return bool(result.get("error"))


def _rank(result: Dict) -> int:
    """Сила результата — чем больше, тем убедительнее улики:
        4 — collect-хиты (аналитика реально работает);
        3 — запрос контейнера со страницы (сетевая улика мини-отчёта);
        2 — в вёрстке найден ID установки (основание для BROKEN);
        1 — страница просто открылась, следов нет;
        0 — ошибка открытия.

    Ярус 2 добавлен после разбора боевого лога. Раньше BROKEN и NONE
    делили один ранг, а слияние заменяло результат только при строгом
    превосходстве — значит классификацию по вёрстке фиксировал ПЕРВЫЙ
    успешный прогон, и все последующие прогоны для таких сайтов просто
    сгорали. Если в первом прогоне не прочитался HTML, сайт навсегда
    оставался NONE, сколько бы прогонов ни делали.
    """
    if _is_error(result):
        return 0
    if result.get("collect_requests"):
        return 4
    if has_network_evidence(result):
        return 3
    if has_strong_markers(result):
        return 2
    return 1


def _tiebreak(result: Dict) -> int:
    """Разрешение равенства рангов: чистый ответ сервера лучше кода ошибки.

    Один и тот же сайт может в одном прогоне отдать 200, а в другом 503;
    улики при этом одинаковые. Для отчёта полезнее показать нормальный
    ответ, поэтому http_error проигрывает.
    """
    return 0 if result.get("http_error") else 1


def _score(result: Dict):
    return (_rank(result), _tiebreak(result))


def merge_runs(runs: List[List[Dict]]) -> List[Dict]:
    """Объединяет несколько прогонов в один список результатов.

    Правила:
        - каждый URL встречается в итоге ровно один раз (дубли отброшены);
        - из всех прогонов по URL берётся результат с максимальным рангом,
          при равенстве рангов — с более чистым ответом сервера, при
          полном равенстве — более ранний прогон (результат стабилен);
        - порядок сайтов — как в первом прогоне, новые URL из следующих
          прогонов добавляются в конец;
        - у итогового результата проставляется 'runs_seen' — в скольких
          прогонах сайт вообще встретился, и 'runs_failed' — в скольких
          из них не открылся. Это видно в расширенном отчёте и сразу
          показывает нестабильные сайты.
    """
    best: Dict[str, Dict] = {}
    order: List[str] = []
    seen_count: Dict[str, int] = {}
    failed_count: Dict[str, int] = {}

    for run in runs:
        for result in run:
            url = result.get("url")
            seen_count[url] = seen_count.get(url, 0) + 1
            if _is_error(result):
                failed_count[url] = failed_count.get(url, 0) + 1
            if url not in best:
                best[url] = result
                order.append(url)
            elif _score(result) > _score(best[url]):
                best[url] = result

    merged = []
    for url in order:
        result = best[url]
        result["runs_seen"] = seen_count.get(url, 0)
        result["runs_failed"] = failed_count.get(url, 0)
        merged.append(result)

    logger.info("Слияние прогонов: %d -> %d уникальных URL",
                sum(len(run) for run in runs), len(merged))
    return merged


def run_improved(previous: List[Dict], merged: List[Dict]) -> bool:
    """Дал ли очередной прогон хоть что-то новое.

    Сравниваются ранги по каждому URL. Используется только для логов:
    прогонов фиксированное число (RUN_COUNT), но в журнале полезно видеть,
    на каком прогоне картина перестала меняться.
    """
    if not previous:
        return True
    before = {r.get("url"): _score(r) for r in previous}
    for result in merged:
        url = result.get("url")
        if url not in before or _score(result) > before[url]:
            return True
    return False
