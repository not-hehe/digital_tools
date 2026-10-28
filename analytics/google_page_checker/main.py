#!/usr/bin/env python3
import asyncio
import logging
import sys
from collections import Counter

from modules.config import (OUTPUT_FILE, SUMMARY_FILE,
                            LOG_FILE, LOG_LEVEL, LOG_FORMAT,
                            MAX_CONCURRENT_PAGES,
                            ALLOW_LEGACY_TLS,
                            SHEET_URL, CONTOUR,
                            MATTERMOST_WEBHOOK_URL, MATTERMOST_TIMEOUT, RUN_COUNT,
                            STATUS_WORKING, STATUS_BROKEN, STATUS_SUSPICIOUS,
                            STATUS_NONE, STATUS_ERROR, STATE_FILE,
                            LOST_CONFIRM_RUNS, GUARD_MIN_ITEMS,
                            MASS_FAILURE_RATIO, HEARTBEAT_EVERY_HOURS)
from modules.alerting import (apply_results, build_message, build_sheet_back,
                               build_state_failure, clear_sheet_failure, load_state,
                               mark_sheet_failure, save_state)
from modules.probe import BrowserManager, PageWorker, build_error_result
from modules.reporting import (ReportWriter, has_network_evidence, merge_runs,
                               send_to_mattermost)
from modules.sources import (COLUMN_NAMES, SheetUnavailable, build_targets,
                             contour_column, read_sheet)
from modules.urls import normalize


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
    Порядок результатов совпадает с порядком списка из таблицы: отчёт
    читается рядом с исходной таблицей, и перестановка строк мешала сверке.
    """
    worker = PageWorker(bm)
    logger = logging.getLogger(__name__)

    async def run_one(url):
        logger.info("...Проверка: %s", url)
        try:
            data = await worker.check_pageview_only(url)
            logger.info("✅ %s: статус=%s", url, data.get("status"))
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


async def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("Запуск проверки Google")
    logger.info("=" * 60)

    try:
        if not SHEET_URL:
            raise SheetUnavailable("ссылка на таблицу не задана "
                                   "(secrets/sheet_url.txt или GPC_SHEET_URL)")
        column = contour_column(CONTOUR)
        columns = read_sheet(SHEET_URL)
        urls, error_urls, skipped = build_targets(columns, CONTOUR)
        if len(urls) == len(error_urls):
            raise SheetUnavailable('в колонке "{}" нет ни одного '
                                   'адреса'.format(COLUMN_NAMES[column]))
    except ValueError as e:
        logger.error("%s", e)
        return 1
    except SheetUnavailable as e:
        logger.error("Не удалось прочитать Google-таблицу: %s", e)
        # Как у domain_checker: о сбое таблицы одно сообщение, дальше тишина до починки.
        if not MATTERMOST_WEBHOOK_URL:
            return 1
        state, _ = load_state(STATE_FILE)
        if not mark_sheet_failure(state, str(e)):
            logger.info("О сбое таблицы уже сообщали, повторно не шлём")
            return 1
        send_to_mattermost(
            MATTERMOST_WEBHOOK_URL,
            "```\n⚠️ GA-чекер: не удалось прочитать Google-таблицу "
            "со списком\n\nПричина: " + str(e) + "\n```",
            timeout=MATTERMOST_TIMEOUT)
        save_state(state, STATE_FILE)
        return 1
    logger.info("Из таблицы: контур %s, адресов %d (из «ошибок» %d)",
                CONTOUR, len(urls), len(error_urls))
    logger.info("Отсеяно мусорных строк: %d", skipped)
    logger.info("Загружено URL: %d", len(urls))
    state, first_run = load_state(STATE_FILE)
    since = clear_sheet_failure(state)
    if since:
        if MATTERMOST_WEBHOOK_URL:
            send_to_mattermost(MATTERMOST_WEBHOOK_URL, build_sheet_back(since),
                               timeout=MATTERMOST_TIMEOUT)
        save_state(state, STATE_FILE)

    bm = BrowserManager(max_concurrent_pages=MAX_CONCURRENT_PAGES,
                        allow_legacy_tls=ALLOW_LEGACY_TLS)
    all_runs = []
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
    for r in all_results:
        if r.get("url") in error_urls:
            r["from_errors"] = True

    if all_results:
        writer = ReportWriter(OUTPUT_FILE)
        writer.write_results_simple(all_results, run_count=len(all_runs))
        logger.info("Расширенный отчёт сохранён: %s", OUTPUT_FILE)
        summary_text = writer.write_summary(all_results, SUMMARY_FILE)

        counts = Counter(r.get("status") for r in all_results)
        http_errors = sum(1 for r in all_results if r.get("http_error"))
        logger.info(
            "Готово. URL: %d | WORKING: %d | BROKEN: %d | "
            "SUSPICIOUS: %d | NONE: %d | ERROR: %d | "
            "разобрано с кодом ошибки: %d",
            len(all_results),
            counts.get(STATUS_WORKING, 0), counts.get(STATUS_BROKEN, 0),
            counts.get(STATUS_SUSPICIOUS, 0), counts.get(STATUS_NONE, 0),
            counts.get(STATUS_ERROR, 0), http_errors,
        )

        # В канал - только смена состояния, как у domain_checker:
        # краткий отчёт целиком остаётся в data/ и в консоли. Память пишется
        # до отправки, чтобы недоступный канал не терял события.
        state, events = apply_results(state, all_results, first_run, LOST_CONFIRM_RUNS)
        text = build_message(state, events, all_results, first_run, GUARD_MIN_ITEMS,
                             MASS_FAILURE_RATIO, HEARTBEAT_EVERY_HOURS)
        saved = save_state(state, STATE_FILE)
        if not saved:
            text = build_state_failure(all_results, STATE_FILE)
        logger.info("Событий: появилась аналитика %d, пропала %d",
                    sum(1 for e in events if e["event"] == "appeared"),
                    sum(1 for e in events if e["event"] == "lost"))
        if text and MATTERMOST_WEBHOOK_URL:
            send_to_mattermost(MATTERMOST_WEBHOOK_URL, text, timeout=MATTERMOST_TIMEOUT)
        elif text:
            logger.info("Вебхук Mattermost не задан (GPC_MATTERMOST_WEBHOOK) "
                        "— сообщение не отправляем")
        else:
            logger.info("Изменений нет — в канал ничего не уходит")

        # Краткий отчёт — последним блоком в консоли, удобно копировать в чат
        print("\n" + summary_text)
        if not saved:
            return 1
    else:
        logger.warning("Нет результатов.")
        # Без результатов память не трогаем: иначе следующий прогон принял бы
        # пустой список за пропажу Google у всех.
        if MATTERMOST_WEBHOOK_URL:
            send_to_mattermost(MATTERMOST_WEBHOOK_URL,
                               "```\n⚠️ GA-чекер: прогон не дал результатов "
                               "(браузер не запустился или упал), отчёты "
                               "не обновлены. Причина в logs/app.log\n```",
                               timeout=MATTERMOST_TIMEOUT)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
