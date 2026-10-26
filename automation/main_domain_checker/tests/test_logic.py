#!/usr/bin/env python3
"""Тесты логики. Сеть не нужна, браузер не нужен, запускается за секунду.

    venv/bin/python tests/test_logic.py

Раннер сам находит функции с префиксом test_ в этом модуле. Ручного списка
нет намеренно: в соседнем проекте такой список ведётся руками, и тест,
забытый в нём, молча не выполняется, а раннер при этом рапортует об успехе.
"""

import io
import json
import logging
import re
import socket
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

from modules.alerting import alert, runlog as rl, state as st
from modules.config import CONFIRM_FAILS
from modules.probe import failures as f
from modules.probe.checker import blank_result, check_all, is_healthy, reason_of
from modules.sources import sheet as sh, targets as tg
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
              "failure_kind": kind})
    return r


def down_event(n, contour="unknown", reason="сервер не ответил"):
    return [{"event": "down", "label": "d{}.{}.example.com".format(i, contour),
             "url": "x", "since": None, "reason": reason, "contour": contour}
            for i in range(n)]


def closed_url(path=""):
    """Адрес закрытого локального порта: отказ соединения без выхода в сеть."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:{}/{}".format(port, path)


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


# ---------------------------------------------------------------------------
# Проверка домена
# ---------------------------------------------------------------------------

def test_check_all_survives_broken_target():
    """Одна битая цель не должна уносить весь прогон.

    Результатов обязано быть столько же, сколько целей: иначе сводка
    посчитает домены и не сойдётся с входным списком.
    """
    targets = [{"url": closed_url(), "label": "closed{}".format(i)}
               for i in range(5)]
    targets.append({"нет_ключа_url": "x"})
    results = check_all(targets, max_workers=2)
    assert len(results) == len(targets)
    assert all(r["ok"] is False for r in results)
    assert results[0]["failure_kind"] == f.FAIL_REFUSED
    assert results[-1]["failure_kind"] == f.FAIL_OTHER


def test_check_all_keeps_order():
    url = closed_url()
    targets = [{"url": url, "label": "closed{}".format(i)} for i in range(10)]
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
        s, events = st.apply_results(s, [result], confirm_fails=2)
        assert [e["event"] for e in events] == expected, why


def test_state_default_confirms_on_third_run():
    """На ВМ двойка давала шум: 39 из 57 падений поднимались следующим прогоном."""
    s = st.empty_state()
    seen = []
    for _ in range(3):
        s, events = st.apply_results(s, [bad_result("a.example.com", code=503)])
        seen.append([e["event"] for e in events])
    assert seen == [[], [], ["down"]]


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
    for _ in range(CONFIRM_FAILS):
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
    assert text.index("Тайм-аут - 3") < text.index("HTTP 404 - 1")


def test_alert_explains_reason():
    results = [bad_result("t.example.com", kind=f.FAIL_TIMEOUT),
               bad_result("s.example.com", kind=f.FAIL_TLS),
               bad_result("h.example.com", code=503),
               bad_result("x.example.com", code=418)]
    text = alert.build_alert(results)
    assert "  не открылся: сервер не ответил за 5 секунд" in text
    assert "TLS - 1\n  не открылся: защищённое соединение" in text
    assert "HTTP 503 - 1\n  открылся с ошибкой: сервер временно недоступен" in text
    assert "HTTP 418 - 1\n  открылся с ошибкой: сервер отказал в запросе" in text
    assert f.failure_label(f.FAIL_TIMEOUT) not in text
    # Причина, которой нет в словаре, выводится как есть и без пояснения.
    text = alert.build_event_alert(down_event(1, reason="что-то новое"),
                                   checked=100)
    assert "что-то новое - 1\n  d0.unknown.example.com" in text


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

def _report(text, webhook="https://mm.example.com/hooks/x", no_notify=False):
    """main._report_sending с подставной отправкой: (результат, что ушло)."""
    import main as entry
    sent = []
    saved = (entry.MATTERMOST_WEBHOOK_URL, entry.send)
    entry.MATTERMOST_WEBHOOK_URL = webhook
    entry.send = lambda text, url: sent.append(text) or True
    try:
        with redirect_stdout(io.StringIO()):
            result = entry._report_sending(text, no_notify)
    finally:
        entry.MATTERMOST_WEBHOOK_URL, entry.send = saved
    return result, sent


def test_notify_silent_without_problems():
    """Главное требование версии: при здоровых доменах канал молчит."""
    assert _report(alert.build_alert([ok_result("a.example.com")] * 3)) == (False, [])


def test_notify_sends_when_problem_exists():
    text = alert.build_alert([ok_result("a.example.com"),
                              bad_result("b.example.com", code=500)])
    assert _report(text) == (True, [text])


def test_notify_skips_without_webhook_or_with_flag():
    text = alert.build_alert([bad_result(code=500)])
    assert _report(text, webhook="") == (False, [])
    assert _report(text, no_notify=True) == (False, [])


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
    """Ручной прогон слал 263 строки списком."""
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


# ---------------------------------------------------------------------------
# Что из падений идёт в канал: группа или долгое одиночное
# ---------------------------------------------------------------------------

def _down_state(labels_since):
    """Состояние с подтверждёнными, но несообщёнными падениями."""
    return {"version": 1, "updated_at": None, "domains": {
        label: {"fails": 3, "down": True, "since": since, "reported": False,
                "url": "https://" + label, "contour": "unknown",
                "reason": "HTTP 503"}
        for label, since in labels_since.items()}}


def test_pick_alerts_needs_a_group_within_window():
    now = datetime(2026, 10, 5, 12, 0)
    two = _down_state({"a": "2026-10-05T11:50:00", "b": "2026-10-05T11:55:00"})
    s, alerts = st.pick_alerts(two, [], now=now)
    assert alerts == [], "двое в окне - молчим"
    assert not any(d["reported"] for d in s["domains"].values())

    three = _down_state({"a": "2026-10-05T11:50:00", "b": "2026-10-05T11:55:00",
                         "c": "2026-10-05T12:00:00"})
    s, alerts = st.pick_alerts(three, [], now=now)
    assert sorted(e["label"] for e in alerts) == ["a", "b", "c"]
    assert all(e["alert"] == "group" for e in alerts)
    assert all(d["reported"] for d in s["domains"].values())


def test_pick_alerts_group_ignores_old_fallen():
    """Третий, упавший час назад, группу не составляет: окно 15 минут."""
    now = datetime(2026, 10, 5, 12, 0)
    s = _down_state({"a": "2026-10-05T11:50:00", "b": "2026-10-05T11:55:00",
                     "old": "2026-10-05T11:00:00"})
    _, alerts = st.pick_alerts(s, [], now=now)
    assert alerts == []


def test_pick_alerts_lone_after_hours():
    now = datetime(2026, 10, 5, 12, 0)
    s = _down_state({"a": "2026-10-04T20:05:00", "b": "2026-10-04T19:55:00"})
    s, alerts = st.pick_alerts(s, [], now=now)
    assert [e["label"] for e in alerts] == ["b"], "16 часов есть только у b"
    assert alerts[0]["alert"] == "lone"
    assert s["domains"]["b"]["reported"] and not s["domains"]["a"]["reported"]
    # Сообщённый больше не ждёт: повторный вызов молчит.
    _, again = st.pick_alerts(s, [], now=now + timedelta(hours=1))
    assert [e["label"] for e in again] == ["a"]


def test_pick_alerts_reports_rise_only_if_fall_was_reported():
    up_silent = {"event": "up", "label": "q", "reported": False, "since": "x"}
    up_loud = {"event": "up", "label": "r", "reported": True, "since": "x"}
    _, alerts = st.pick_alerts(st.empty_state(), [up_silent, up_loud])
    assert [e["label"] for e in alerts] == ["r"]


def test_reported_flag_survives_runs_and_resets_on_rise():
    """Признак "сообщено" живёт в состоянии и исчезает вместе с падением."""
    s = st.empty_state()
    for _ in range(3):
        s, events = st.apply_results(s, [bad_result("a.example.com", code=503)])
    s, alerts = st.pick_alerts(s, events, now=datetime(2030, 1, 1))
    assert alerts and s["domains"]["a.example.com"]["reported"] is True
    s, _ = st.apply_results(s, [bad_result("a.example.com", code=503)])
    assert s["domains"]["a.example.com"]["reported"] is True, "переживает прогон"
    assert s["domains"]["a.example.com"]["reason"] == "HTTP 503"
    s, events = st.apply_results(s, [ok_result("a.example.com")])
    assert events[0]["reported"] is True
    assert "reported" not in s["domains"]["a.example.com"]


def test_event_alert_separates_lone_and_group():
    group = [dict(e, alert="group") for e in down_event(3)]
    lone = [{"event": "down", "label": "lone.example.com", "url": "x",
             "since": "2026-10-04T20:00:00", "reason": "HTTP 503",
             "contour": "unknown", "alert": "lone"}]
    text = alert.build_event_alert(group + lone, checked=100)
    assert "Упали: 3" in text and "начните с машины проверки" in text
    assert "Не отвечают дольше 16 часов: 1" in text
    assert ("lone.example.com - HTTP 503 (с 2026-10-04 20:00)\n"
            "    открылся с ошибкой: сервер временно недоступен") in text


# ---------------------------------------------------------------------------
# Лог прогонов и событий
# ---------------------------------------------------------------------------

def _log_lines(path, kind=None):
    """Строки журнала; kind - только записи этого вида (колонка 22-29)."""
    lines = [line for line in Path(path).read_text(encoding="utf-8").splitlines()
             if line.strip()]
    return lines if kind is None else [l for l in lines if l[21:29].strip() == kind]


def test_runlog_writes_run_and_events():
    tmp = Path(tempfile.mkdtemp()) / "runs.log"
    results = [ok_result("a.example.com"), bad_result("b.example.com", code=503)]
    s, events = st.apply_results(st.empty_state(), results, confirm_fails=1)
    assert rl.append([rl.run_line(results)] + rl.event_lines(events), path=tmp)
    lines = _log_lines(tmp)
    assert len(lines) == 2
    assert lines[0].endswith("прогон    проверено 2, не ответили 1: b.example.com (HTTP 503)")
    assert lines[1].endswith("упал      b.example.com - HTTP 503")
    s, events = st.apply_results(s, [ok_result("b.example.com")])
    rl.append(rl.event_lines(events), path=tmp)
    assert "поднялся  b.example.com - не отвечал с 20" in _log_lines(tmp)[2]
    # Строка на прогон пишется и когда событий нет: по ним считают ритм.
    assert rl.append([rl.run_line([ok_result("a.example.com")])], path=tmp)
    assert len(_log_lines(tmp, "прогон")) == 2
    assert _log_lines(tmp, "прогон")[1].endswith("проверено 1, не ответили 0")


def test_runlog_is_trimmed_with_slack():
    """Обрезка с запасом: иначе файл переписывался бы каждый прогон."""
    tmp = Path(tempfile.mkdtemp()) / "runs.log"
    for i in range(10):
        rl.append(["строка {}".format(i)], path=tmp, max_lines=10)
    assert len(_log_lines(tmp)) == 10
    rl.append(["строка 10"], path=tmp, max_lines=10)
    assert len(_log_lines(tmp)) == 11, "в пределах запаса файл не трогаем"
    rl.append(["строка 11"], path=tmp, max_lines=10)
    assert _log_lines(tmp) == ["строка {}".format(i) for i in range(2, 12)], \
        "за пределом запаса остаётся хвост"


def test_runlog_failure_does_not_raise():
    missing_dir = Path(tempfile.mkdtemp()) / "нет" / "runs.log"
    assert rl.append(["строка"], path=missing_dir) is False


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
        assert not _SHEET_ID_RE.search(source), "ссылка на таблицу в {}".format(
            path.relative_to(root))


_SHEET_ID_RE = re.compile(r"/spreadsheets/d/[A-Za-z0-9_-]{20,}")


# ---------------------------------------------------------------------------
# Google-таблица
# ---------------------------------------------------------------------------

SHEET = "https://docs.google.com/spreadsheets/d/TESTID/edit?hl=ru&gid=7#gid=7"


class _FakeHeaders:
    def __init__(self, ctype):
        self.ctype = ctype

    def get_content_type(self):
        return self.ctype


class _FakeResponse:
    def __init__(self, body, ctype="text/csv"):
        self.body, self.headers = body, _FakeHeaders(ctype)

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(body=b"", ctype="text/csv", error=None):
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        if error is not None:
            raise error
        return _FakeResponse(body, ctype)
    opener.calls = calls
    return opener


def _csv(*rows):
    return "\n".join(",".join(r) for r in rows).encode("utf-8")


def _sheet_reason(**kw):
    try:
        sh.read_sheet(SHEET, opener=_opener(**kw))
    except sh.SheetUnavailable as e:
        return str(e)
    raise AssertionError("ожидался SheetUnavailable")


def test_sheet_export_url_keeps_gid():
    assert sh.export_url(SHEET) == ("https://docs.google.com/spreadsheets/d/"
                                    "TESTID/export?format=csv&gid=7")
    assert sh.export_url("https://docs.google.com/spreadsheets/d/X1/edit"
                         ).endswith("/d/X1/export?format=csv&gid=0")
    try:
        sh.export_url("https://example.com/table")
    except sh.SheetUnavailable as e:
        assert "не похожа на Google-таблицу" in str(e)
    else:
        raise AssertionError("чужая ссылка должна давать SheetUnavailable")


def test_sheet_failures_are_plain_words():
    import socket
    import urllib.error
    not_found = urllib.error.HTTPError(SHEET, 404, "Not Found", {}, None)
    assert "не найдена (404)" in _sheet_reason(error=not_found)
    forbidden = urllib.error.HTTPError(SHEET, 403, "Forbidden", {}, None)
    assert "таблица закрыта" in _sheet_reason(error=forbidden)
    # Закрытая таблица отдаёт страницу входа, а не ошибку.
    assert "таблица закрыта" in _sheet_reason(body=b"<html>", ctype="text/html")
    assert "не ответил за 30 секунд" in _sheet_reason(error=socket.timeout())
    no_net = urllib.error.URLError("nodename nor servname provided")
    assert "нет связи с docs.google.com" in _sheet_reason(error=no_net)
    assert _sheet_reason(body=b"") == "таблица пуста"
    assert 'нет колонки "внешний"' in _sheet_reason(
        body=_csv(["ошибки", "внутренний"], ["a.example.com", "b.example.com"]))


def test_sheet_columns_by_header_prefix():
    body = _csv(["Внешний контур", "ОШИБКИ", "внутренний"],
                ["e1.example.com", "x1.example.com", "i1.example.com"],
                ["e2.example.com", "", "i2.example.com"],
                ["", "x2.example.com", ""])
    opener = _opener(body=body)
    cols = sh.read_sheet(SHEET, opener=opener)
    assert cols == {"external": ["e1.example.com", "e2.example.com"],
                    "errors": ["x1.example.com", "x2.example.com"],
                    "internal": ["i1.example.com", "i2.example.com"]}
    assert opener.calls == [sh.export_url(SHEET)]


def test_contour_switch():
    assert sh.contour_column(1) == "external"
    assert sh.contour_column("2") == "internal"
    for bad in ("3", "", "внешний", None):
        try:
            sh.contour_column(bad)
        except ValueError as e:
            assert "1 (внешний) или 2 (внутренний)" in str(e)
        else:
            raise AssertionError("переключатель {!r} должен давать ошибку".format(bad))


_SHEET_BODY = _csv(["ошибки", "внутренний", "внешний"],
                   ["dead.example.com", "in.example.com", "out.example.com"],
                   ["both.example.com", "", "both.example.com"],
                   ["нет", "", "уточняется"])


def test_load_from_sheet_contour_first_and_junk_skipped():
    loaded = tg.load_from_sheet(SHEET, contour=1, opener=_opener(body=_SHEET_BODY))
    got = {t["label"]: (t["contour"], t["known_bad"]) for t in loaded["targets"]}
    assert got == {"out.example.com": ("external", False),
                   "both.example.com": ("external", False),
                   "dead.example.com": ("external", True)}
    assert loaded["known_bad"] == 1
    assert loaded["counts"] == {"internal": 0, "external": 2}
    assert sorted(loaded["skipped"]) == ["нет", "уточняется"]

    inner = tg.load_from_sheet(SHEET, contour=2, opener=_opener(body=_SHEET_BODY))
    assert {t["label"] for t in inner["targets"] if not t["known_bad"]} == {
        "in.example.com"}


def test_load_from_sheet_empty_contour_is_sheet_failure():
    body = _csv(["ошибки", "внутренний", "внешний"],
                ["dead.example.com", "in.example.com", "нет"])
    try:
        tg.load_from_sheet(SHEET, contour=1, opener=_opener(body=body))
    except sh.SheetUnavailable as e:
        assert 'в колонке "внешний" нет ни одного адреса' in str(e)
    else:
        raise AssertionError("пустой контур должен давать SheetUnavailable")


def test_load_from_sheet_dedups_same_site_on_stdlib():
    """Схема, регистр и хвостовой слэш не делают из одного сайта три.

    Список читается на stdlib: тяжёлых библиотек в зависимостях быть не должно,
    их возврат через любой импорт сразу утяжелит установку на хосте.
    """
    body = _csv(["ошибки", "внутренний", "внешний"],
                ["", "", "a.example.com"],
                ["", "", "https://a.example.com/"],
                ["", "", "A.EXAMPLE.COM"],
                ["", "", "b.example.com"])
    r = tg.load_from_sheet(SHEET, contour=1, opener=_opener(body=body))
    assert [t["label"] for t in r["targets"]] == ["a.example.com", "b.example.com"]
    assert r["counts"] == {"internal": 0, "external": 2}
    assert "pandas" not in sys.modules and "openpyxl" not in sys.modules


def test_load_targets_limit_recounts_everything():
    """Выборка описывает то, что реально проверено: и контуры, и колонку ошибок."""
    saved = (tg.SHEET_URL, tg.read_sheet)
    try:
        tg.SHEET_URL = SHEET
        tg.read_sheet = lambda url, opener=None: sh.parse_columns(
            _SHEET_BODY.decode("utf-8"))
        full = tg.load_targets()
        assert (len(full["targets"]), full["known_bad"]) == (3, 1)
        cut = tg.load_targets(limit=2)
        assert [t["label"] for t in cut["targets"]] == ["out.example.com",
                                                        "both.example.com"]
        assert cut["counts"] == {"internal": 0, "external": 2}
        assert cut["known_bad"] == 0
    finally:
        tg.SHEET_URL, tg.read_sheet = saved


def test_load_from_sheet_without_url_is_sheet_failure():
    opener = _opener(body=_SHEET_BODY)
    try:
        tg.load_from_sheet("", contour=1, opener=opener)
    except sh.SheetUnavailable as e:
        assert "ссылка на таблицу не задана" in str(e)
    else:
        raise AssertionError("без ссылки должен быть SheetUnavailable")
    assert opener.calls == [], "без ссылки в сеть не ходим"


def _known_bad(label, ok):
    r = (ok_result if ok else bad_result)(label, contour="external",
                                          **({} if ok else {"kind": f.FAIL_TIMEOUT}))
    r["known_bad"] = True
    return r


def test_known_bad_silent_until_revived_then_normal_rules():
    state = st.empty_state()
    for _ in range(10):
        state, events = st.apply_results(state, [_known_bad("dead.example.com", False)])
        state, alerts = st.pick_alerts(state, events,
                                       now=datetime.now() + timedelta(days=3))
        assert events == [] and alerts == []
    assert state["domains"]["dead.example.com"]["reported"] is True

    state, events = st.apply_results(state, [_known_bad("dead.example.com", True)])
    state, alerts = st.pick_alerts(state, events)
    assert [a.get("revived") for a in alerts] == [True]
    text = alert.build_event_alert(alerts, checked=10)
    assert ('🟢 Ожили из списка ошибок: 1\n  dead.example.com\n'
            'Перенесите их в таблице из колонки "ошибки" в "внешний".') in text
    assert "Поднялись" not in text

    # Таблицу ещё не поправили, сайт по-прежнему в колонке ошибок - но живёт
    # уже по обычным правилам: подтверждение тремя прогонами, затем 16 часов.
    for i in range(CONFIRM_FAILS):
        state, events = st.apply_results(state, [_known_bad("dead.example.com", False)])
    assert [e["event"] for e in events] == ["down"]
    state, alerts = st.pick_alerts(state, events)
    assert alerts == []
    state, alerts = st.pick_alerts(state, [],
                                   now=datetime.now() + timedelta(hours=17))
    assert [a["alert"] for a in alerts] == ["lone"]
    state, events = st.apply_results(state, [_known_bad("dead.example.com", True)])
    state, alerts = st.pick_alerts(state, events)
    assert [(a["event"], a.get("revived")) for a in alerts] == [("up", None)]


def test_revived_block_is_not_hidden_by_guard():
    fell = [dict(e, alert="group") for e in down_event(60)]
    revived = [{"event": "up", "label": "dead.example.com", "revived": True,
                "reported": True, "contour": "internal"}]
    text = alert.build_event_alert(fell + revived, checked=100)
    assert "Похоже, дело в самом чекере" in text
    assert 'Ожили из списка ошибок: 1\n  dead.example.com' in text
    assert 'в "внутренний"' in text


def test_manual_summary_and_runlog_skip_known_bad_names():
    results = [bad_result("down.example.com", code=503),
               _known_bad("dead.example.com", False),
               _known_bad("back.example.com", True)]
    text = alert.build_alert(results)
    assert "проблемных 1 из 1" in text
    assert "dead.example.com" not in text
    assert "Ожили из списка ошибок: 1\n  back.example.com" in text
    line = rl.run_line(results)
    assert "не ответили 2 (из списка ошибок 1): down.example.com (HTTP 503)" in line
    assert "dead.example.com" not in line


def test_sheet_alert_once_then_recovery():
    import main as entry
    tmp = Path(tempfile.mkdtemp())
    sent = []
    saved = (st.STATE_FILE, entry.MATTERMOST_WEBHOOK_URL, entry.send,
             tg.SHEET_URL, tg.read_sheet, entry.run_full)
    try:
        st.STATE_FILE = tmp / "state.json"
        entry.MATTERMOST_WEBHOOK_URL = "http://127.0.0.1:9/test"
        entry.send = lambda text, url: sent.append(text) or True

        # Нет ссылки - тот же сбой таблицы: код 3 и одно сообщение.
        tg.SHEET_URL = ""
        assert entry.main([]) == 3
        assert len(sent) == 1 and "ссылка на таблицу не задана" in sent[0]
        assert (tmp / "sheet_alert.json").exists()

        def broken(url, opener=None):
            raise sh.SheetUnavailable("таблица закрыта")
        tg.SHEET_URL = SHEET
        tg.read_sheet = broken
        assert entry.main([]) == 3 and len(sent) == 1, "о сбое уже сообщено"
        # Выборку запускает человек: в канал не пишем.
        assert entry.main(["--limit", "3"]) == 3 and len(sent) == 1

        tg.read_sheet = lambda url, opener=None: sh.parse_columns(
            _SHEET_BODY.decode("utf-8"))
        entry.run_full = lambda loaded, no_notify: 0
        assert entry.main([]) == 0
        assert len(sent) == 2 and "снова читается" in sent[1]
        assert not (tmp / "sheet_alert.json").exists()
        assert entry.main([]) == 0 and len(sent) == 2

        tg.read_sheet = broken
        assert entry.main([]) == 3
        assert len(sent) == 3 and "Причина: таблица закрыта" in sent[2]
    finally:
        (st.STATE_FILE, entry.MATTERMOST_WEBHOOK_URL, entry.send,
         tg.SHEET_URL, tg.read_sheet, entry.run_full) = saved


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
