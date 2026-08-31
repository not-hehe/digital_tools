#!/usr/bin/env python3
import asyncio
import logging
import sys
from collections import Counter

from modules.excel_reader import read_urls_from_excel
from modules.browser_manager import BrowserManager
from modules.page_worker import PageWorker, build_error_result
from modules.report_writer import ReportWriter
from modules.notifier import send_to_mattermost
from modules.aggregator import merge_runs, has_network_evidence, run_improved
from modules.failures import failure_label
from modules.url_variants import normalize
from modules.config import (INPUT_FILE, OUTPUT_FILE, SUMMARY_FILE,
                            LOG_FILE, LOG_LEVEL, LOG_FORMAT,
                            EXCEL_COLUMN_NAME, EXCEL_SHEET_NAME,
                            EXCEL_EMPTY_ROW_BREAK, MAX_CONCURRENT_PAGES,
                            BROWSER_HEADLESS, BROWSER_SLOW_MO, BLOCK_RESOURCES,
                            ALLOW_LEGACY_TLS,
                            MATTERMOST_WEBHOOK_URL, MATTERMOST_TIMEOUT, RUN_COUNT,
                            STATUS_WORKING, STATUS_BROKEN, STATUS_SUSPICIOUS,
                            STATUS_NONE, STATUS_ERROR, GA_POSITIVE_STATUSES)


def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(LOG_FILE, encoding="utf-8")]
    )
    for lib in ("playwright", "asyncio"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def process_urls(urls, bm):
    """Проверяет весь список за один прогон.

    Одновременность ограничивает семафор внутри BrowserManager.page_context
    Порядок результатов совпадает с порядком входного файла: отчёт
    читается рядом с исходным Excel, и перестановка строк мешала сверке.
    """
    worker = PageWorker(bm)
    logger = logging.getLogger(__name__)

    async def run_one(url):
        logger.info("...Проверка: %s", url)
        try:
            data = await worker.check_pageview_only(url)
            logger.info("✅ %s: статус=%s, GA=%s", url, data.get("status"),
                        data.get("has_ga"))
            return data
        except Exception as e:
            logger.error("❌ Ошибка %s: %s", url, e)
            # Адрес обязательно нормализуем: check_pageview_only всегда
            # возвращает нормализованный URL, и если аварийная ветка
            # запишет сырой («lk.example.com» вместо «https://lk.example.com»),
            # слияние прогонов посчитает это разными сайтами и раздует
            # отчёт дублями.
            return build_error_result(normalize(url), str(e))

    tasks = [asyncio.create_task(run_one(u)) for u in urls]
    return list(await asyncio.gather(*tasks))


def _log_failure_breakdown(logger, results):
    """Пишет в журнал разбивку недоступных сайтов по виду отказа.

    Одна куча ERROR ничего не говорит о том, что чинить: DNS-записи
    другого контура, устаревший TLS и мусор во входном файле требуют
    совершенно разных действий.
    """
    failures = [r for r in results if r.get("status") == STATUS_ERROR]
    if not failures:
        return
    counts = Counter(r.get("failure_kind") or "other" for r in failures)
    logger.info("Недоступные сайты (%d) по видам отказа:", len(failures))
    for kind, count in counts.most_common():
        logger.info("  %-14s %3d — %s", kind, count, failure_label(kind))


async def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Запуск проверки Google")
    logger.info("=" * 60)

    try:
        urls = list(read_urls_from_excel(INPUT_FILE, column_name=EXCEL_COLUMN_NAME,
                                         sheet_name=EXCEL_SHEET_NAME,
                                         empty_row_break=EXCEL_EMPTY_ROW_BREAK))
        logger.info("Загружено URL: %d", len(urls))
        if not urls:
            logger.error("Список URL пуст.")
            return
    except Exception as e:
        logger.error("Ошибка чтения Excel: %s", e)
        return

    bm = BrowserManager(headless=BROWSER_HEADLESS, slow_mo=BROWSER_SLOW_MO,
                        block_resources=BLOCK_RESOURCES,
                        max_concurrent_pages=MAX_CONCURRENT_PAGES,
                        allow_legacy_tls=ALLOW_LEGACY_TLS)
    all_runs = []
    merged = []
    try:
        async with bm:
            for run_no in range(1, RUN_COUNT + 1):
                logger.info("--- Прогон %d из %d ---", run_no, RUN_COUNT)
                run_results = await process_urls(urls, bm)
                found = sum(1 for r in run_results if has_network_evidence(r))
                opened = sum(1 for r in run_results
                             if r.get("status") != STATUS_ERROR)
                logger.info("Прогон %d: открыто %d из %d, "
                            "Google обнаружено (network) у %d",
                            run_no, opened, len(run_results), found)
                all_runs.append(run_results)
                previous, merged = merged, merge_runs(all_runs)
                if run_no > 1 and not run_improved(previous, merged):
                    logger.info("Прогон %d новых улик не добавил "
                                "(картина стабильна)", run_no)
                if not bm.is_alive():
                    # Chromium умер: оставшиеся прогоны не проверят ничего,
                    # но допишут в отчёт сотни выдуманных ERROR. Выходим
                    # и отчитываемся по тому, что успели собрать.
                    logger.error("Браузер недоступен после прогона %d — "
                                 "останавливаемся, отчёт строим по "
                                 "собранным данным", run_no)
                    break
    except Exception as e:
        logger.error("Критическая ошибка браузера: %s", e)

    # Слияние прогонов: по каждому URL — лучший результат, без дублей.
    # Ошибкой сайт числится, только если не открылся ни в одном прогоне.
    all_results = merge_runs(all_runs) if all_runs else []

    if all_results:
        writer = ReportWriter(OUTPUT_FILE)
        writer.write_results_simple(all_results, run_count=len(all_runs))
        logger.info("Расширенный отчёт сохранён: %s", OUTPUT_FILE)
        summary_text = writer.write_summary(all_results, SUMMARY_FILE)

        counts = Counter(r.get("status") for r in all_results)
        ga_found = sum(1 for r in all_results
                       if r.get("status") in GA_POSITIVE_STATUSES)
        http_errors = sum(1 for r in all_results if r.get("http_error"))
        logger.info(
            "Готово. URL: %d | GA есть: %d (WORKING: %d, BROKEN: %d) | "
            "SUSPICIOUS: %d | NONE: %d | ERROR: %d | "
            "разобрано с кодом ошибки: %d",
            len(all_results), ga_found,
            counts.get(STATUS_WORKING, 0), counts.get(STATUS_BROKEN, 0),
            counts.get(STATUS_SUSPICIOUS, 0), counts.get(STATUS_NONE, 0),
            counts.get(STATUS_ERROR, 0), http_errors,
        )
        _log_failure_breakdown(logger, all_results)

        # Отправка в Mattermost (если настроен вебхук); ``` сохраняет
        # моноширинное выравнивание счётчиков в сообщении
        if MATTERMOST_WEBHOOK_URL:
            send_to_mattermost(MATTERMOST_WEBHOOK_URL,
                               "```\n" + summary_text + "```",
                               timeout=MATTERMOST_TIMEOUT)
        else:
            logger.info("Вебхук Mattermost не задан (GPC_MATTERMOST_WEBHOOK) "
                        "— отчёт не отправляем")

        # Краткий отчёт — последним блоком в консоли, удобно копировать в чат
        print("\n" + summary_text)
    else:
        logger.warning("Нет результатов.")


if __name__ == "__main__":
    asyncio.run(main())
