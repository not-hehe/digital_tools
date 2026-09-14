#!/usr/bin/env python3
"""Тесты логики. Сеть не нужна, браузер не нужен, запускается за секунду.

    venv/bin/python tests/test_logic.py

Раннер сам находит функции с префиксом test_ в этом модуле. Ручного списка
нет намеренно: в соседнем проекте такой список ведётся руками, и тест,
забытый в нём, молча не выполняется, а раннер при этом рапортует об успехе.
"""

import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from openpyxl import Workbook

from modules.alerting import alert, state as st
from modules.probe import failures as f
from modules.probe.checker import (blank_result, check_all, check_domain,
                                   is_healthy, reason_of)
from modules.sources import targets as tg
from modules.urls import label_of, looks_like_url, normalize

# ---------------------------------------------------------------------------
# Заготовки
# ---------------------------------------------------------------------------

def ok_result(label="a.example.com", code=200, contour="unknown"):
    r = blank_result("https://" + label, label)
    r.update({"ok": True, "status_code": code, "contour": contour})
    return r


def bad_result(label="b.example.com", code=None, kind=None, contour="unknown"):
    r = blank_result("https://" + label, label)
    r.update({"ok": False, "status_code": code, "contour": contour,
              "failure_kind": kind,
              "failure_label": f.failure_label(kind) if kind else None})
    return r


def down_event(n, contour="unknown", reason="сервер не ответил"):
    return [{"event": "down", "label": "d{}.{}.example.com".format(i, contour),
             "url": "x", "since": None, "reason": reason, "contour": contour}
            for i in range(n)]


def excel_with(rows, headers=("url", "contour")):
    wb = Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    path = Path(tempfile.mkdtemp()) / "list.xlsx"
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Признак живого домена
# ---------------------------------------------------------------------------

def test_healthy_boundaries():
    """Граница 200..399 включительно; отсутствие ответа живым не считается."""
    for code in (200, 204, 301, 302, 399):
        assert is_healthy(code), code
    for code in (100, 199, 400, 404, 500, 503):
        assert not is_healthy(code), code
    assert not is_healthy(None)


def test_reason_of():
    assert reason_of(bad_result(code=404)) == "HTTP 404"
    assert reason_of(bad_result(kind=f.FAIL_TIMEOUT)) == f.failure_label(f.FAIL_TIMEOUT)
    assert reason_of(blank_result("https://x.example.com")) == "причина не определена"


# ---------------------------------------------------------------------------
# Таксономия отказов
# ---------------------------------------------------------------------------

def test_failure_classification():
    e = requests.exceptions
    cases = {
        e.ConnectTimeout("timed out"): f.FAIL_TIMEOUT,
        e.ReadTimeout("timed out"): f.FAIL_TIMEOUT,
        e.SSLError("bad handshake"): f.FAIL_TLS,
        e.ProxyError("tunnel failed"): f.FAIL_PROXY,
        e.TooManyRedirects("loop"): f.FAIL_REDIRECT_LOOP,
        e.MissingSchema("no scheme"): f.FAIL_INVALID_URL,
        e.InvalidURL("bad url"): f.FAIL_INVALID_URL,
        e.ConnectionError("Connection refused"): f.FAIL_REFUSED,
        e.ConnectionError("Name or service not known"): f.FAIL_DNS,
        e.ConnectionError("nodename nor servname provided"): f.FAIL_DNS,
        ValueError("что-то своё"): f.FAIL_OTHER,
    }
    for exc, expected in cases.items():
        got = f.classify_exception(exc)
        assert got == expected, "{}: ожидалось {}, вышло {}".format(
            type(exc).__name__, expected, got)


def test_connect_timeout_is_not_refused():
    """ConnectTimeout наследуется и от ConnectionError, и от Timeout.

    Если проверять ConnectionError первым, все таймауты схлопнутся
    в «соединение отвергнуто», и в отчёте пропадёт целый класс отказов.
    """
    exc = requests.exceptions.ConnectTimeout("timed out")
    assert isinstance(exc, requests.exceptions.ConnectionError)
    assert f.classify_exception(exc) == f.FAIL_TIMEOUT


def test_every_failure_kind_has_label():
    for kind in (f.FAIL_DNS, f.FAIL_TLS, f.FAIL_TIMEOUT, f.FAIL_REFUSED,
                 f.FAIL_REDIRECT_LOOP, f.FAIL_PROXY, f.FAIL_INVALID_URL,
                 f.FAIL_OTHER):
        assert f.failure_label(kind)
        assert f.failure_label(kind) != kind


# ---------------------------------------------------------------------------
# Адреса
# ---------------------------------------------------------------------------

def test_normalize_and_looks_like_url():
    assert normalize("example.com") == "https://example.com"
    assert normalize("http://example.com") == "http://example.com"
    assert normalize("  example.com  ") == "https://example.com"
    assert label_of("https://example.com/path") == "example.com/path"

    for good in ("example.com", "https://a.b.example.com/p?x=1", "localhost:8080",
                 "127.0.0.1", "xn--80aswg.xn--p1ai"):
        assert looks_like_url(good), good
    for junk in ("", "   ", "нет", "уточняется", "?", "va-audio"):
        assert not looks_like_url(junk), junk


def test_contour_aliases():
    for value in ("internal", "INT", "внутренний", " ВН "):
        assert tg.normalize_contour(value) == tg.CONTOUR_INTERNAL, value
    for value in ("external", "ext", "ВНЕШНИЙ"):
        assert tg.normalize_contour(value) == tg.CONTOUR_EXTERNAL, value
    # Нераспознанное не угадываем: приписать домен не тому контуру хуже,
    # чем признать, что метки нет.
    for value in ("", None, "непонятно", "42"):
        assert tg.normalize_contour(value) == tg.CONTOUR_UNKNOWN, value


# ---------------------------------------------------------------------------
# Загрузка списка
# ---------------------------------------------------------------------------

def test_loader_counts_and_dedup():
    path = excel_with([
        ("a.example.com", "internal"),
        ("https://a.example.com/", "internal"),   # тот же сайт: схема и слэш
        ("A.EXAMPLE.COM", "internal"),            # тот же сайт: регистр
        ("b.example.com", "external"),
        ("c.example.com", ""),
        ("нет", "internal"),                      # мусор
    ])
    r = tg.load_from_excel(path=path)
    assert [t["label"] for t in r["targets"]] == [
        "a.example.com", "b.example.com", "c.example.com"]
    assert r["skipped"] == ["нет"]
    assert r["counts"] == {"internal": 1, "external": 1, "unknown": 1}
    assert sum(r["counts"].values()) == len(r["targets"])


def test_loader_without_contour_column():
    """Старый файл без колонки должен грузиться как раньше."""
    path = excel_with([("a.example.com",), ("b.example.com",)], headers=("url",))
    r = tg.load_from_excel(path=path)
    assert len(r["targets"]) == 2
    assert r["counts"]["unknown"] == 2
    assert all(t["contour"] == tg.CONTOUR_UNKNOWN for t in r["targets"])


def test_loader_refuses_empty_list():
    """Ноль целей это отказ, а не тихий прогон.

    Иначе проверять нечего, событий нет, канал молчит - и при этом состояние
    обнуляется целиком, унося память обо всех упавших доменах.
    """
    path = excel_with([], headers=("url", "contour"))
    try:
        tg.load_targets(path=path)
    except tg.EmptyTargetList:
        return
    raise AssertionError("ожидался EmptyTargetList")


def test_loader_reads_without_pandas():
    """Читалка работает на голом openpyxl: pandas в зависимостях быть не должно.

    Проверка не косметическая: pandas был самой тяжёлой зависимостью, и его
    возврат через любой импорт сразу утяжелит установку на хосте.
    """
    path = excel_with([("a.example.com", "internal")])
    assert len(tg.load_from_excel(path=path)["targets"]) == 1
    assert "pandas" not in sys.modules


# ---------------------------------------------------------------------------
# Проверка домена
# ---------------------------------------------------------------------------

def test_check_domain_rejects_junk_without_network():
    r = check_domain("нет")
    assert r["failure_kind"] == f.FAIL_INVALID_URL
    assert r["ok"] is False
    assert r["status_code"] is None


def test_check_all_survives_broken_target():
    """Одна битая цель не должна уносить весь прогон.

    Результатов обязано быть столько же, сколько целей: иначе сводка
    посчитает домены и не сойдётся с входным списком.
    """
    targets = [{"url": "нет", "label": "нет"} for _ in range(5)]
    targets.append({"нет_ключа_url": "x"})
    results = check_all(targets, max_workers=2)
    assert len(results) == len(targets)
    assert all(r["ok"] is False for r in results)


def test_check_all_keeps_order():
    targets = [{"url": "junk{}".format(i), "label": "junk{}".format(i)}
               for i in range(10)]
    results = check_all(targets, max_workers=5)
    assert [r["label"] for r in results] == [t["label"] for t in targets]


# ---------------------------------------------------------------------------
# Состояние между запусками
# ---------------------------------------------------------------------------

def test_state_confirms_failure_over_two_runs():
    """Пять сценариев из постановки задачи, по шагам."""
    s = st.empty_state()
    steps = [
        (False, [], "первый отказ: событий нет"),
        (False, ["down"], "второй отказ подряд: домен признан упавшим"),
        (False, [], "третий отказ: повторных событий нет"),
        (True, ["up"], "успех после отказов: домен поднялся"),
        (False, [], "одиночный отказ: тревоги нет"),
        (True, [], "успех следом: событий нет"),
    ]
    for ok, expected, why in steps:
        result = ok_result() if ok else bad_result("a.example.com", code=503)
        s, events = st.apply_results(s, [result])
        assert [e["event"] for e in events] == expected, why


def test_state_roundtrip_and_degradation():
    tmp = Path(tempfile.mkdtemp())
    s, _ = st.apply_results(st.empty_state(), [bad_result(code=404)])
    assert st.save_state(s, tmp / "state.json")
    assert st.load_state(tmp / "state.json")["domains"]

    # Ни отсутствие файла, ни мусор в нём не должны ронять прогон.
    assert st.load_state(tmp / "нет.json")["domains"] == {}
    broken = tmp / "broken.json"
    broken.write_text("{это не json", encoding="utf-8")
    assert st.load_state(broken)["domains"] == {}


def test_state_forgets_domains_out_of_list():
    """Иначе файл растёт вечно и хранит то, что давно не проверяется."""
    s, _ = st.apply_results(st.empty_state(), [bad_result("old.example.com", code=500)])
    s, _ = st.apply_results(s, [bad_result("new.example.com", code=500)])
    assert "old.example.com" not in s["domains"]
    assert "new.example.com" in s["domains"]


def test_state_carries_contour_into_events():
    s = st.empty_state()
    for _ in range(2):
        s, events = st.apply_results(
            s, [bad_result("a.example.com", code=500, contour="internal")])
    assert events[0]["contour"] == "internal"


def test_state_survives_json_roundtrip():
    """Состояние пишется в JSON, значит должно состоять из простых типов."""
    s, _ = st.apply_results(st.empty_state(), [bad_result(code=404)])
    assert json.loads(json.dumps(s)) == s


# ---------------------------------------------------------------------------
# Сводка обо всех проблемах
# ---------------------------------------------------------------------------

def test_build_alert_silent_when_healthy():
    assert alert.build_alert([ok_result("a.example.com"),
                              ok_result("b.example.com")]) is None


def test_build_alert_lists_only_problems():
    text = alert.build_alert([ok_result("healthy.example.com"),
                              bad_result("broken.example.com", code=404)])
    assert "broken.example.com" in text
    assert "healthy.example.com" not in text
    assert "HTTP 404" in text


def test_build_alert_groups_largest_first():
    results = ([bad_result("t{}.example.com".format(i), kind=f.FAIL_TIMEOUT)
                for i in range(3)]
               + [bad_result("h.example.com", code=404)])
    text = alert.build_alert(results)
    assert text.index(f.failure_label(f.FAIL_TIMEOUT)) < text.index("HTTP 404")


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def _counting_sender(calls):
    return lambda *a, **kw: calls.append(a) or True


def test_notify_silent_without_problems():
    """Главное требование версии: при здоровых доменах канал молчит."""
    calls = []
    sent = alert.send(alert.build_alert([ok_result("a.example.com")] * 3),
                      "https://mm.example.com/hooks/x",
                      sender=_counting_sender(calls))
    assert calls == []
    assert sent is False


def test_notify_sends_when_problem_exists():
    calls = []
    text = alert.build_alert([ok_result("a.example.com"),
                              bad_result("b.example.com", code=500)])
    sent = alert.send(text, "https://mm.example.com/hooks/x",
                      sender=_counting_sender(calls))
    assert len(calls) == 1
    assert sent is True


def test_notify_skips_without_webhook():
    calls = []
    sent = alert.send(alert.build_alert([bad_result(code=500)]), "",
                      sender=_counting_sender(calls))
    assert calls == []
    assert sent is False


# ---------------------------------------------------------------------------
# Сообщение о смене состояния и предохранители
# ---------------------------------------------------------------------------

def test_event_alert_silent_without_events():
    assert alert.build_event_alert([], checked=10) is None


def test_event_alert_lists_fallen_and_risen():
    events = down_event(2) + [{"event": "up", "label": "up.example.com",
                               "url": "x", "since": "2026-08-31T10:00:00",
                               "reason": None, "contour": "unknown"}]
    text = alert.build_event_alert(events, checked=100)
    assert "Упали: 2" in text
    assert "Поднялись: 1" in text
    assert "не отвечал с 2026-08-31 10:00" in text


def test_mass_failure_guard_replaces_the_list():
    """Половина списка разом это отказ наблюдателя, а не столько же аварий."""
    text = alert.build_event_alert(down_event(60), checked=100)
    assert "дело в самом чекере" in text
    assert "d0.unknown.example.com" not in text


def test_mass_failure_guard_stays_out_of_the_way():
    text = alert.build_event_alert(down_event(3), checked=100)
    assert "дело в самом чекере" not in text
    assert "d0.unknown.example.com" in text


def test_contour_guard_catches_route_failure():
    """Отказ маршрута кладёт четверть списка, общий порог его пропустит."""
    counts = {"internal": 88, "external": 278, "unknown": 0}
    text = alert.build_event_alert(down_event(88, "internal"), checked=366,
                                   contour_counts=counts)
    assert "внутренн" in text
    assert "278" in text, "должно быть сказано, что другой контур цел"
    assert "d0.internal.example.com" not in text


def test_manual_summary_goes_through_guards():
    """Ручной прогон слал 263 строки списком - тот же случай, что и T-11."""
    results = ([bad_result("b{}.example.com".format(i), code=500)
                for i in range(60)]
               + [ok_result("h{}.example.com".format(i)) for i in range(40)])
    text = alert.build_alert(results)
    assert "дело в самом чекере" in text
    assert "b0.example.com" not in text


def test_guards_leave_short_lists_alone():
    """Перечень из трёх адресов полезнее строки «упало 3 из 4»."""
    results = ([bad_result("b{}.example.com".format(i), code=500)
                for i in range(3)] + [ok_result("h.example.com")])
    text = alert.build_alert(results)
    assert "дело в самом чекере" not in text
    assert "b0.example.com" in text


def test_contour_guard_catches_route_recovery():
    """Вернувшийся маршрут даёт тот же поток строк, что и отказавший."""
    counts = {"internal": 88, "external": 278, "unknown": 0}
    events = [{"event": "up", "label": "u{}.internal.example.com".format(i),
               "url": "x", "since": "2026-08-31T10:00:00", "reason": None,
               "contour": "internal"} for i in range(88)]
    text = alert.build_event_alert(events, checked=366, contour_counts=counts)
    assert "восстановилась связь" in text
    assert "внутренн" in text
    assert "u0.internal.example.com" not in text


def test_contour_guard_needs_enough_domains():
    """При частичной разметке одно падение не должно давать тревогу о контуре.

    Тревога о маршруте вдобавок не называет адресов, так что падение
    единственного размеченного домена осталось бы вообще без имени.
    """
    counts = {"internal": 1, "external": 365, "unknown": 0}
    text = alert.build_event_alert(down_event(1, "internal"), checked=366,
                                   contour_counts=counts)
    assert "пропала связь" not in text
    assert "d0.internal.example.com" in text


def test_contour_guard_ignores_mixed_failures():
    """Упали домены обоих контуров - маршрут ни при чём, разбираем обычно."""
    counts = {"internal": 88, "external": 278, "unknown": 0}
    events = down_event(50, "internal") + down_event(50, "external")
    text = alert.build_event_alert(events, checked=366, contour_counts=counts)
    assert "пропала связь" not in text
    assert "d0.internal.example.com" in text


# ---------------------------------------------------------------------------
# Признак жизни
# ---------------------------------------------------------------------------

def test_heartbeat_due_without_stamp():
    """Отметки нет - слать сразу: это первый прогон после выката."""
    assert st.due_for_heartbeat(st.empty_state()) is True


def test_heartbeat_not_due_right_after():
    """Иначе признак жизни уходил бы каждые пять минут.

    Давнюю отметку задаём строкой, а не малым every_hours: _stamp усекает
    время до секунд, и порог в доли секунды давал бы то падение, то успех
    в зависимости от момента запуска.
    """
    state = st.mark_heartbeat(st.empty_state())
    assert st.due_for_heartbeat(state) is False
    assert st.due_for_heartbeat({"last_heartbeat": "2020-01-01T00:00:00"}) is True


def test_heartbeat_survives_apply_results():
    """Отметка обязана переносить прогон.

    apply_results собирает состояние заново, и без явного переноса отметка
    терялась бы каждый раз, а heartbeat шёл бы в канал непрерывно.
    """
    state = st.mark_heartbeat(st.empty_state())
    stamp = state["last_heartbeat"]
    new_state, _ = st.apply_results(state, [ok_result("a.example.com")])
    assert new_state.get("last_heartbeat") == stamp


def test_heartbeat_with_broken_stamp_is_due():
    """Битая отметка - лучше лишнее сообщение, чем молчание."""
    assert st.due_for_heartbeat({"last_heartbeat": "не дата"}) is True


def test_heartbeat_text_names_numbers():
    text = alert.build_heartbeat(366, 240)
    assert "Проверено 366 доменов" in text
    assert "не отвечают 240" in text


# ---------------------------------------------------------------------------
# Секреты
# ---------------------------------------------------------------------------

def test_no_webhook_address_in_code():
    """Адрес вебхука даёт право писать в канал: в коде его быть не должно.

    Обходится всё, что уезжает в публичную копию: пакеты modules/ на любую
    глубину, tools/, корневые скрипты, run.sh и .env.example. Перечень
    каталогов не задан руками нарочно - после раскладки по слоям шаблон
    modules/*.py молча перестал видеть три подпакета из четырёх файлов.
    Тесты не сканируются: подставные адреса в них лежат намеренно.
    """
    root = Path(__file__).parent.parent
    skip = {"venv", "tests", "__pycache__", "docs", "data"}
    paths = [p for p in root.rglob("*.py")
             if not (set(p.relative_to(root).parts[:-1]) & skip)]
    paths += [root / "run.sh", root / ".env.example"]
    assert len(paths) > 10, "обход нашёл слишком мало файлов: {}".format(paths)
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "/hooks/" not in source, "адрес вебхука в {}".format(
            path.relative_to(root))


# ---------------------------------------------------------------------------
# Раннер
# ---------------------------------------------------------------------------

def run_all() -> int:
    # Часть тестов нарочно ведёт код по путям отказа, и он честно пишет
    # об этом в лог. В выводе тестов такие строки выглядят как поломка,
    # хотя проверяют они как раз правильное поведение.
    logging.disable(logging.CRITICAL)
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("FAIL {}: {}".format(name, e))
        except Exception as e:
            failed += 1
            print("ERROR {}: {}: {}".format(name, type(e).__name__, e))
        else:
            print("OK   {}".format(name))
    print()
    if failed:
        print("Провалено {} из {}.".format(failed, len(tests)))
    else:
        print("Все {} тестов пройдены.".format(len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
