import logging
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, TextIO

from .aggregator import has_network_evidence, has_strong_markers
from .failures import (FAILURE_ORDER, FAIL_OTHER, failure_label, failure_hint)
from .classifier import is_universal_analytics
from .config import (STATUS_WORKING, STATUS_BROKEN, STATUS_SUSPICIOUS,
                     STATUS_NONE, STATUS_ERROR, BROKEN_REASON_LABELS,
                     BROKEN_UA_SUNSET)

logger = logging.getLogger(__name__)

_URL_LIMIT = 200        # длинные collect-URL обрезаем для читаемости отчёта
_MAX_ITEMS_SHOWN = 3    # сколько запросов одного типа показывать на сайт


def _shorten(text: str, limit: int = _URL_LIMIT) -> str:
    """Обрезает слишком длинную строку, помечая обрез многоточием."""
    return text if len(text) <= limit else text[:limit] + "…"


def _status_of(result: Dict) -> str:
    status = result.get("status")
    if status:
        return status
    if result.get("error"):
        return STATUS_ERROR
    return STATUS_WORKING if result.get("has_ga") else STATUS_NONE


def group_failures(results: List[Dict]) -> "OrderedDict[str, List[Dict]]":
    """Раскладывает недоступные сайты по виду отказа.

    Порядок групп — из FAILURE_ORDER: сверху то, что чинится правкой
    входных данных или настроек, снизу то, что упирается в контур сети.
    """
    buckets: Dict[str, List[Dict]] = {}
    for r in results:
        if _status_of(r) != STATUS_ERROR:
            continue
        buckets.setdefault(r.get("failure_kind") or FAIL_OTHER, []).append(r)
    ordered = OrderedDict()
    for kind in FAILURE_ORDER:
        if buckets.get(kind):
            ordered[kind] = buckets.pop(kind)
    for kind, items in buckets.items():   # виды, которых нет в FAILURE_ORDER
        ordered[kind] = items
    return ordered


def count_ua_sunset(results: List[Dict]) -> int:
    """Сколько сайтов несут тег отключённой Universal Analytics.

    Отдельный счётчик нужен, потому что это принципиально другая ситуация,
    чем «тег есть, но выключен конфигом»: чинить нечего, систему выключил
    Google, код надо просто вычистить.
    """
    return sum(1 for r in results
               if r.get("broken_reason") == BROKEN_UA_SUNSET
               or (_status_of(r) == STATUS_BROKEN
                   and is_universal_analytics(r.get("container_id"))))


def group_by_container(results: List[Dict]) -> "OrderedDict[str, List[Dict]]":
    """Группирует сайты по идентификатору установки GA/GTM из вёрстки.

    В боевом прогоне 24 сайта делили один GTM-W9D3BC, а в отчёте лежали
    двадцатью четырьмя независимыми записями. Один контейнер на N сайтов —
    это один шаблон платформы и одно решение, а не N разборов.
    """
    buckets: Dict[str, List[Dict]] = {}
    for r in results:
        container_id = r.get("container_id")
        if container_id:
            buckets.setdefault(container_id, []).append(r)
    return OrderedDict(sorted(buckets.items(),
                              key=lambda kv: (-len(kv[1]), kv[0])))


class ReportWriter:

    def __init__(self, output_file: Path):
        self.output_file = output_file
        self.output_file.parent.mkdir(parents=True, exist_ok=True)

    def write_results_simple(self, all_results: List[Dict],
                             run_count: int = None) -> None:

        """
        Записывает расширенный отчёт в текстовый файл.

        Аргументы:
            all_results (List[Dict]): результаты проверки от PageWorker
                (после слияния прогонов — по одному на URL).
            run_count (int|None): сколько циклов проверки объединено
                в этих результатах; если задано — выводится в шапке.

        Структура отчёта:
            - шапка со счётчиками;
            - сводка по контейнерам (один GTM на несколько сайтов);
            - секции по статусам GA;
            - недоступные сайты, разложенные по ВИДУ отказа, а не одной
              кучей: DNS другого контура, устаревший TLS и мусор во
              входном файле требуют разных действий.
        """

        groups = self._group_results(all_results)

        # NONE делим на «есть слабые упоминания GA» и «совсем чисто»
        none_weak, none_clean = [], []
        for r in groups[STATUS_NONE]:
            has_weak = any(m.get("strength") == "weak"
                           for m in (r.get("html_markers") or []))
            (none_weak if has_weak else none_clean).append(r)

        n_working = len(groups[STATUS_WORKING])
        n_broken = len(groups[STATUS_BROKEN])
        n_susp = len(groups[STATUS_SUSPICIOUS])
        n_none = len(groups[STATUS_NONE])
        n_err = len(groups[STATUS_ERROR])
        opened = len(all_results) - n_err
        n_http_error = sum(1 for r in all_results if r.get("http_error"))
        n_alternate = sum(1 for r in all_results
                          if r.get("opened_url")
                          and r.get("opened_url") != r.get("url"))
        n_ua = count_ua_sunset(all_results)

        with open(self.output_file, "w", encoding="utf-8") as f:
            f.write("=== ОТЧЁТ ПО НАЛИЧИЮ GOOGLE ANALYTICS ===\n")
            if run_count:
                f.write(f"Циклов проверки              : {run_count}\n")
            f.write(f"Всего URL в списке           : {len(all_results)}\n")
            f.write(f"Успешно открыто (всего)      : {opened}\n")
            if n_http_error:
                f.write(f"  ├─ сервер ответил кодом >=400, содержимое "
                        f"разобрано : {n_http_error}\n")
            if n_alternate:
                f.write(f"  └─ открыто альтернативным адресом "
                        f"(http/www)        : {n_alternate}\n")
            f.write(f"Google Analytics есть        : {n_working + n_broken}\n")
            f.write(f"  ├─ WORKING (работает)      : {n_working}\n")
            f.write(f"  └─ BROKEN (тег без хитов)  : {n_broken}\n")
            if n_ua:
                f.write(f"     └─ из них мёртвый тег Universal Analytics"
                        f" : {n_ua}\n")
            f.write(f"SUSPICIOUS (ручная проверка) : {n_susp}\n")
            f.write(f"NONE (аналитики нет)         : {n_none}\n")
            f.write(f"  └─ со слабыми упоминаниями : {len(none_weak)}\n")
            f.write(f"Ошибок (не удалось открыть)  : {n_err}\n")
            f.write("=" * 60 + "\n\n")

            self._write_container_summary(f, all_results)

            # BROKEN делим надвое: мёртвый UA — это «вычистить код»,
            # остальное — «разобраться, почему не срабатывает».
            broken_ua, broken_rest = [], []
            for r in groups[STATUS_BROKEN]:
                (broken_ua if r.get("broken_reason") == BROKEN_UA_SUNSET
                 else broken_rest).append(r)

            self._write_group(f, "WORKING — аналитика работает",
                              groups[STATUS_WORKING], self._format_working)
            self._write_group(
                f, "BROKEN — мёртвый тег Universal Analytics "
                   "(Google отключил систему, чинить нечего — вычистить код)",
                broken_ua, self._format_broken)
            self._write_group(f, "BROKEN — GA в вёрстке есть, рабочих хитов нет",
                              broken_rest, self._format_broken)
            self._write_group(f, "SUSPICIOUS — только error-телеметрия, проверить вручную",
                              groups[STATUS_SUSPICIOUS], self._format_suspicious)
            self._write_group(f, "NONE — GA не установлен, но есть слабые упоминания в вёрстке",
                              none_weak, self._format_none_weak)
            self._write_group(f, "NONE — признаков Google Analytics нет",
                              none_clean, self._format_none)

            self._write_failures(f, groups[STATUS_ERROR])

            f.write("=" * 60 + "\nКОНЕЦ ОТЧЁТА\n")
        logger.info("Отчёт сохранён в %s", self.output_file)

    # ------------------------------------------------------------------
    # Сводка по контейнерам
    # ------------------------------------------------------------------

    @staticmethod
    def _write_container_summary(f: TextIO, all_results: List[Dict]) -> None:
        """Пишет секцию «один контейнер — сколько сайтов».

        Выводятся только контейнеры, встреченные больше чем на одном
        сайте: именно они означают общий шаблон, а не отдельную установку.
        """
        containers = group_by_container(all_results)
        shared = OrderedDict((cid, items) for cid, items in containers.items()
                             if len(items) > 1)
        if not shared:
            return
        f.write(f"--- Общие контейнеры: один ID на несколько сайтов "
                f"({len(shared)}) ---\n\n")
        f.write("  Такие сайты собраны на общем шаблоне: решение по "
                "контейнеру\n  закрывает сразу всю группу.\n\n")
        for container_id, items in shared.items():
            statuses = Counter(_status_of(r) for r in items)
            breakdown = ", ".join(f"{status}: {count}"
                                  for status, count in statuses.most_common())
            mark = (" — Universal Analytics, система отключена Google"
                    if is_universal_analytics(container_id) else "")
            f.write(f"  {container_id} — сайтов: {len(items)} "
                    f"({breakdown}){mark}\n")
            for r in items:
                f.write(f"    - {r['url']}\n")
            f.write("\n")
        f.write("\n")

    # ------------------------------------------------------------------
    # Недоступные сайты по видам отказа
    # ------------------------------------------------------------------

    @staticmethod
    def _write_failures(f: TextIO, failures: List[Dict]) -> None:
        if not failures:
            return
        grouped = group_failures(failures)
        f.write(f"--- ERROR — не удалось открыть ({len(failures)}) ---\n\n")
        f.write("  Разложено по виду отказа: у каждой группы своя причина\n"
                "  и своё действие.\n\n")
        for kind, items in grouped.items():
            f.write(f"  [{kind}] {failure_label(kind)} — {len(items)}\n")
            f.write(f"  что делать: {failure_hint(kind)}\n")
            for r in items:
                f.write(f"    - {r['url']}\n")
                error_line = str(r.get("error") or "").splitlines()[:1]
                if error_line:
                    f.write(f"      {_shorten(error_line[0], 160)}\n")
                tried = [u for u in (r.get("tried_urls") or [])
                         if u != r.get("url")]
                if tried:
                    f.write(f"      пробовали также: {', '.join(tried)}\n")
            f.write("\n")
        f.write("\n")

    # ------------------------------------------------------------------
    # Краткий отчёт («для чата»)
    # ------------------------------------------------------------------

    def build_summary(self, all_results: List[Dict]) -> str:
        groups = self._group_results(all_results)
        n_err = len(groups[STATUS_ERROR])
        opened = [r for r in all_results if _status_of(r) != STATUS_ERROR]

        ga_found, ga_not_found = [], []
        for r in opened:
            if has_network_evidence(r):
                ga_found.append(r)
            else:
                ga_not_found.append(r)
        # Сайты без сетевой активности, но с ID установки в вёрстке.
        # Отдельная строка нужна, чтобы «обнаружено: 7» в этом отчёте и
        # «GA есть: 127» в расширенном не выглядели противоречием: это
        # разные вопросы — «счётчик работает» и «тег лежит в коде».
        markup_only = [r for r in ga_not_found if has_strong_markers(r)]
        ua_only = [r for r in markup_only
                   if is_universal_analytics(r.get("container_id"))]

        lines = []
        lines.append("=== ОТЧЁТ ПО НАЛИЧИЮ GOOGLE ANALYTICS ===")
        lines.append(f"Всего URL в списке          : {len(all_results)}")
        lines.append(f"Успешно открыто (всего)     : {len(opened)}")
        lines.append(f"  └─ Google обнаружено      : {len(ga_found)}")
        lines.append(f"  └─ Google не обнаружено   : {len(ga_not_found)}")
        # if markup_only:
        #     lines.append(f"     из них тег GA лежит в вёрстке, "
        #                  f"но не срабатывает : {len(markup_only)}")
        if ua_only:
            lines.append(f"     из них мёртвый тег Universal Analytics "
                         f"(отключён Google) : {len(ua_only)}")
        lines.append(f"Ошибок (не удалось открыть) : {n_err}")
        lines.append("-" * 60)

        if ga_found:
            lines.append("--- Google обнаружен ---")
            for r in ga_found:
                lines.append(r["url"])
                lines.append("  Статус : Google Found")

        if groups[STATUS_ERROR]:
            lines.append("--- Ошибка при открытии ---")
            for kind, items in group_failures(groups[STATUS_ERROR]).items():
                lines.append(f"  {failure_label(kind)} — {len(items)}")
                for r in items:
                    lines.append(f"    {r['url']}")

        lines.append("-" * 60)
        lines.append("КОНЕЦ ОТЧЁТА")
        return "\n".join(lines) + "\n"

    def write_summary(self, all_results: List[Dict], summary_file: Path) -> str:
        """Строит краткий отчёт, сохраняет его в файл и возвращает текст."""
        text = self.build_summary(all_results)
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write(text)
        logger.info("Краткий отчёт сохранён в %s", summary_file)
        return text

    # ------------------------------------------------------------------
    # Вспомогательные методы записи
    # ------------------------------------------------------------------

    @staticmethod
    def _group_results(all_results: List[Dict]) -> Dict[str, List[Dict]]:
        """Раскладывает результаты по статусам классификации."""
        groups = {status: [] for status in (STATUS_WORKING, STATUS_BROKEN,
                                            STATUS_SUSPICIOUS, STATUS_NONE,
                                            STATUS_ERROR)}
        for r in all_results:
            groups.setdefault(_status_of(r), []).append(r)
        return groups

    @staticmethod
    def _write_context(f: TextIO, r: Dict) -> None:
        """Пишет обстоятельства открытия: код ответа и подменённый адрес."""
        http_status = r.get("http_status")
        if r.get("http_error"):
            f.write(f"  Ответ  : HTTP {http_status} — страница отдана "
                    f"с кодом ошибки, содержимое разобрано\n")
        opened_url = r.get("opened_url")
        if opened_url and opened_url != r.get("url"):
            f.write(f"  Открыт : {opened_url} (исходный адрес не отвечал)\n")
        if r.get("runs_failed"):
            f.write(f"  Стабильность : не открылся в "
                    f"{r['runs_failed']} из {r.get('runs_seen')} прогонов\n")

    def _write_group(self, f: TextIO, title: str, items: List[Dict],
                     formatter) -> None:
        """Пишет секцию отчёта: заголовок и все сайты через formatter.

        Сайт, сам отправивший сетевой запрос (критерий мини-отчёта),
        подсвечивается строкой «Google : Found ...» в любой секции.
        """
        if not items:
            return
        f.write(f"--- {title} ({len(items)}) ---\n\n")
        for r in items:
            f.write(f"{r['url']}\n")
            if has_network_evidence(r):
                f.write("  Google : Found (сетевой запрос со страницы — "
                        "в мини-отчёте)\n")
            formatter(f, r)
            self._write_context(f, r)
            f.write("\n")
        f.write("\n")

    @staticmethod
    def _write_requests(f: TextIO, title: str, requests: List[Dict]) -> None:
        """Пишет список запросов (не более _MAX_ITEMS_SHOWN) с HTTP-статусами."""
        if not requests:
            return
        f.write(f"  {title} ({len(requests)}):\n")
        for req in requests[:_MAX_ITEMS_SHOWN]:
            http = req.get("status")
            suffix = f"  [HTTP {http}]" if http is not None else ""
            f.write(f"    - {_shorten(req['url'])}{suffix}\n")
        hidden = len(requests) - _MAX_ITEMS_SHOWN
        if hidden > 0:
            f.write(f"    … и ещё {hidden}\n")

    @staticmethod
    def _write_markers(f: TextIO, markers: List[Dict]) -> None:
        """Пишет найденные в вёрстке маркеры и контекст вокруг них."""
        if not markers:
            return
        f.write(f"  Маркеры в вёрстке ({len(markers)}):\n")
        for m in markers:
            f.write(f"    - {m['marker']}\n")
            if m.get("snippet"):
                f.write(f"      контекст: …{m['snippet']}…\n")

    def _format_working(self, f: TextIO, r: Dict) -> None:
        f.write("  Статус : WORKING (Google Analytics работает)\n")
        self._write_requests(f, "Collect-хиты", r.get("collect_requests") or [])
        self._write_markers(f, r.get("html_markers") or [])

    def _format_broken(self, f: TextIO, r: Dict) -> None:
        reason = BROKEN_REASON_LABELS.get(r.get("broken_reason"))
        if reason:
            f.write(f"  Статус : BROKEN — {reason}\n")
        else:
            f.write("  Статус : BROKEN (тег в вёрстке есть, рабочих хитов нет)\n")
        if r.get("container_id"):
            f.write(f"  Контейнер : {r['container_id']}\n")
        probe = r.get("probe")
        if probe:
            status = probe.get("status")
            outcome = (f"HTTP {status}" if status is not None
                       else f"ошибка: {probe.get('error')}")
            f.write(f"  Зонд   : {_shorten(probe['url'])} → {outcome}\n")
        self._write_markers(f, r.get("html_markers") or [])
        self._write_requests(f, "Error-телеметрия", r.get("error_requests") or [])
        self._write_requests(f, "Прочие Google-запросы", r.get("ga_requests") or [])

    def _format_suspicious(self, f: TextIO, r: Dict) -> None:
        f.write("  Статус : SUSPICIOUS (следов в вёрстке нет — проверить вручную)\n")
        self._write_requests(f, "Error-телеметрия", r.get("error_requests") or [])

    def _format_none(self, f: TextIO, r: Dict) -> None:
        f.write("  Статус : NONE (признаков Google Analytics нет)\n")

    def _format_none_weak(self, f: TextIO, r: Dict) -> None:
        f.write("  Статус : NONE (слабые упоминания GA без ID — "
                "установкой не считается)\n")
        weak = [m for m in (r.get("html_markers") or [])
                if m.get("strength") == "weak"]
        self._write_markers(f, weak)
