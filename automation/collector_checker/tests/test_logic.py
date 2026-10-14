#!/usr/bin/env python3
"""Тесты логики collector_checker без сети: сессия, sleep и отправитель подменяются.

    venv/bin/python -B -W ignore tests/test_logic.py
"""

import contextlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

import main as main_module
from modules import sources
from modules.alerting import alert, notifier, reminder, runlog, state
from modules.probe import checker as probe
from modules.probe import failures
from modules.config import (ATTEMPTS, GROUP_MIN_DOWN, RETRY_PAUSE, TELEGRAM_ENDPOINT_PAUSE,
                            TELEGRAM_HOST, TELEGRAM_IPS)

ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def raises(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    raise AssertionError("ожидали {}".format(exc_type.__name__))


def test_sources_empty_text():
    raises(sources.EmptyCollectorList, sources.parse_collectors, "")


def test_sources_only_comments():
    raises(sources.EmptyCollectorList, sources.parse_collectors, "# a\n\n   # b\n")


def test_sources_duplicate_name_reports_line():
    e = raises(sources.BadCollectorLine, sources.parse_collectors,
               "# шапка\nv1 https://a/1\n\nv1 https://a/2\n")
    assert "строка 4" in str(e), str(e)


def test_sources_one_field():
    e = raises(sources.BadCollectorLine, sources.parse_collectors, "v1 https://a/1\nv2\n")
    assert "строка 2" in str(e), str(e)


def test_sources_third_field_must_be_codes():
    e = raises(sources.BadCollectorLine, sources.parse_collectors, "# c\nv1 https://a/1 лишнее\n")
    assert "строка 2" in str(e), str(e)
    raises(sources.BadCollectorLine, sources.parse_collectors, "v1 https://a/1 200,abc\n")
    raises(sources.BadCollectorLine, sources.parse_collectors, "v1 https://a/1 999\n")
    raises(sources.BadCollectorLine, sources.parse_collectors, "v1 https://a/1 200 404\n")


def test_sources_third_field_sets_healthy_codes():
    got = sources.parse_collectors("v1 https://a/1\nv16 https://a/v16/i 404,200\n")
    assert "healthy" not in got[0], "без колонки правило общее"
    assert got[1] == {"name": "v16", "url": "https://a/v16/i", "healthy": (200, 404)}, got[1]


def test_sources_url_without_scheme():
    e = raises(sources.BadCollectorLine, sources.parse_collectors, "\n\nv1 ftp://a/1\n")
    assert "строка 3" in str(e), str(e)


def test_sources_http_and_https_accepted_in_order():
    got = sources.parse_collectors("v9 http://h/9\nv2 https://h/2\nv5 https://h/5\n")
    assert got == [{"name": "v9", "url": "http://h/9"},
                   {"name": "v2", "url": "https://h/2"},
                   {"name": "v5", "url": "https://h/5"}], got


# ---------------------------------------------------------------------------
# failures
# ---------------------------------------------------------------------------

def test_failures_kinds():
    ex = requests.exceptions
    cases = [
        (ex.SSLError("bad cert"), "tls"),
        (ex.ConnectTimeout("t"), "timeout"),
        (ex.ReadTimeout("t"), "timeout"),
        (ex.ConnectionError("[Errno 8] nodename nor servname provided"), "dns"),
        (ex.ConnectionError("Connection refused"), "connection"),
        (ValueError("x"), "other"),
        (ex.InvalidURL("x"), "other"),
    ]
    for exc, expected in cases:
        got = failures.classify_exception(exc)
        assert got == expected, "{}: {} != {}".format(type(exc).__name__, got, expected)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

class ProbeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


class ProbeSession:
    """script: по элементу на вызов get; исключение бросается, число становится кодом."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.responses = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        response = ProbeResponse(item)
        self.responses.append(response)
        return response


COLLECTOR = {"name": "v7", "url": "https://h/v7"}


def check(script, **kwargs):
    session = ProbeSession(script)
    sleeps = []
    result = probe.check_collector(COLLECTOR, session=session, sleep=sleeps.append, **kwargs)
    return result, session, sleeps


def test_probe_200_first_try():
    result, session, sleeps = check([200])
    assert result["ok"] is True
    assert len(result["attempts"]) == 1 and len(session.calls) == 1
    assert sleeps == [], sleeps
    assert result["status_code"] == 200 and result["failure_kind"] is None
    assert session.responses[0].closed, "ответ должен закрываться без чтения тела"
    kwargs = session.calls[0][1]
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True, kwargs


def test_probe_target_specific_codes():
    pixel = {"name": "v16", "url": "https://h/v16/i", "healthy": (200, 404)}
    session = ProbeSession([404])
    result = probe.check_collector(pixel, session=session, sleep=lambda s: None)
    assert result["ok"] is True and len(session.calls) == 1, result
    session = ProbeSession([500, 500, 500])
    result = probe.check_collector(pixel, session=session, sleep=lambda s: None, attempts=3)
    assert result["ok"] is False and len(session.calls) == 3, result
    result, _, _ = check([404, 404, 404])
    assert result["ok"] is False, "без колонки 404 это падение"


def test_probe_single_attempt_by_default():
    # AC-13: моргание не гасится повтором, следующий прогон через минуту.
    result, session, sleeps = check([503, 200])
    assert ATTEMPTS == 1
    assert result["ok"] is False and result["status_code"] == 503, result
    assert len(session.calls) == 1 and len(result["attempts"]) == 1, result
    assert sleeps == [], sleeps


def test_probe_503_then_200():
    result, session, sleeps = check([503, 200], attempts=3)
    assert result["ok"] is True
    assert len(result["attempts"]) == 2
    assert sleeps == [RETRY_PAUSE], sleeps
    assert result["status_code"] == 200


def test_probe_503_three_times():
    result, _, sleeps = check([503, 503, 503], attempts=3)
    assert result["ok"] is False
    assert len(result["attempts"]) == 3
    assert len(sleeps) == 2, "пауза после последней попытки не нужна: {}".format(sleeps)
    assert result["status_code"] == 503


def test_probe_timeout_three_times():
    exc = requests.exceptions.ConnectTimeout("connect timed out")
    result, _, _ = check([exc, exc, exc], attempts=3)
    assert result["ok"] is False
    assert result["failure_kind"] == "timeout"
    assert result["status_code"] is None
    assert "timed out" in result["error"], result["error"]
    assert [a["failure_kind"] for a in result["attempts"]] == ["timeout"] * 3


def test_probe_redirect_is_down():
    result, _, _ = check([301, 301, 301])
    assert result["ok"] is False and result["status_code"] == 301


def test_probe_attempt_fields():
    result, _, _ = check([503, requests.exceptions.SSLError("x"), 200], attempts=3)
    attempts = result["attempts"]
    assert [a["status_code"] for a in attempts] == [503, None, 200], attempts
    assert [a["failure_kind"] for a in attempts] == [None, "tls", None], attempts
    for a in attempts:
        assert set(a) == {"status_code", "failure_kind", "elapsed_ms"}, a
        assert isinstance(a["elapsed_ms"], int) and a["elapsed_ms"] >= 0


def test_probe_custom_pause_and_attempts():
    result, _, sleeps = check([500, 500], attempts=2, pause=0.5)
    assert len(result["attempts"]) == 2 and sleeps == [0.5], sleeps


class SleepingSession(ProbeSession):
    """Каждый get спит seconds настоящих секунд: потолок проверяется часами, а не подменой."""

    def __init__(self, script, seconds):
        super().__init__(script)
        self.seconds = seconds

    def get(self, url, **kwargs):
        time.sleep(self.seconds)
        return super().get(url, **kwargs)


def check_slow(script, seconds, deadline, **kwargs):
    session = SleepingSession(script, seconds)
    sleeps = []
    result = probe.check_collector(COLLECTOR, session=session, sleep=sleeps.append,
                                   deadline=deadline, **kwargs)
    return result, session, sleeps


def test_probe_timeout_is_connect_read_tuple():
    _, session, _ = check([200], timeout=7)
    assert session.calls[0][1]["timeout"] == (7, 7), session.calls[0][1]


def test_probe_attempt_over_deadline_stops_retries():
    result, _, sleeps = check_slow([503, 503, 503], 0.6, 0.2)
    assert sleeps == [] and len(result["attempts"]) == 1, result
    assert result["ok"] is False and result["failure_kind"] == "timeout", result
    assert result["status_code"] is None, "причина должна быть timeout, а не HTTP 503"


def test_probe_slow_200_is_not_ok():
    result, _, _ = check_slow([200], 0.6, 0.2)
    assert result["ok"] is False and result["failure_kind"] == "timeout", result


def test_probe_capped_attempt_returns_by_deadline():
    # Попытка висит 2 с, потолок 0.2: check_collector обязан вернуться сам, не дожидаясь.
    started = time.monotonic()
    check_slow([200], 2.0, 0.2)
    assert time.monotonic() - started < 1.0


def test_probe_attempt_within_deadline_is_normal():
    result, session, _ = check_slow([503, 200], 0.05, 1.0, attempts=3)
    assert result["ok"] is True and len(session.calls) == 2, result


def test_probe_check_all_keeps_input_order():
    def slow_first(collector, **kwargs):
        # Первый завершается последним: порядок по завершению дал бы другой список.
        time.sleep({"a": 0.15, "b": 0.05, "c": 0.0}[collector["name"]])
        return {"name": collector["name"], "ok": True}

    collectors = [{"name": n, "url": "https://h/" + n} for n in "abc"]
    with mock.patch.object(probe, "check_collector", slow_first):
        got = probe.check_all(collectors)
    assert [r["name"] for r in got] == ["a", "b", "c"], got


def test_probe_check_all_empty():
    assert probe.check_all([]) == []


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 12, 15, 0, 0)
T1 = datetime(2026, 10, 12, 16, 0, 0)


def res(name, ok, status=None):
    return {"name": name, "url": "https://h/" + name, "ok": ok,
            "status_code": 200 if ok else (503 if status is None else status),
            "failure_kind": None, "error": None, "attempts": [], "checked_at": T1}


def test_state_new_ok():
    new, events = state.apply_results({}, [res("v1", True)], now=T1)
    assert new == {"v1": {"status": "up", "since": T1}} and events == []


def test_state_new_not_ok():
    new, events = state.apply_results({}, [res("v1", False)], now=T1)
    assert new == {"v1": {"status": "pending", "since": T1}}
    assert [(e["event"], e["name"], e["since"]) for e in events] == [("down", "v1", T1)]
    assert events[0]["reason"] == "HTTP 503"


def test_state_up_not_ok():
    new, events = state.apply_results({"v1": {"status": "up", "since": T0}},
                                      [res("v1", False)], now=T1)
    assert new["v1"] == {"status": "pending", "since": T1}
    assert [e["event"] for e in events] == ["down"] and events[0]["since"] == T1


def test_state_pending_not_ok_stays_pending():
    new, events = state.apply_results({"v1": {"status": "pending", "since": T0}},
                                      [res("v1", False)], now=T1)
    assert new["v1"] == {"status": "pending", "since": T0}
    assert [e["event"] for e in events] == ["still_down"] and events[0]["since"] == T0


def test_state_pending_ok_is_quiet_up():
    new, events = state.apply_results({"v1": {"status": "pending", "since": T0}},
                                      [res("v1", True)], now=T1)
    assert new["v1"] == {"status": "up", "since": T1}
    assert [e["event"] for e in events] == ["quiet_up"], events


def test_state_lying_keeps_order():
    st = {"v1": {"status": "up", "since": T0}, "v3": {"status": "pending", "since": T0},
          "v7": {"status": "down", "since": T0}}
    assert state.lying(st) == ["v3", "v7"]
    assert state.lying({}) == []


def test_state_roundtrip_pending():
    original = {"v7": {"status": "down", "since": T0}, "v8": {"status": "pending", "since": T1}}
    assert state.parse_state(state.render_state(original)) == original


def test_state_down_not_ok_keeps_since():
    new, events = state.apply_results({"v1": {"status": "down", "since": T0}},
                                      [res("v1", False)], now=T1)
    assert new["v1"] == {"status": "down", "since": T0}
    assert [e["event"] for e in events] == ["still_down"] and events[0]["since"] == T0


def test_state_down_ok():
    new, events = state.apply_results({"v1": {"status": "down", "since": T0}},
                                      [res("v1", True)], now=T1)
    assert new["v1"] == {"status": "up", "since": T1}
    assert [(e["event"], e["since"]) for e in events] == [("up", T1)]
    assert events[0]["reason"], "reason должен быть заполнен"


def test_state_up_ok_keeps_since():
    new, events = state.apply_results({"v1": {"status": "up", "since": T0}},
                                      [res("v1", True)], now=T1)
    assert new["v1"] == {"status": "up", "since": T0} and events == []


def test_state_drops_collectors_not_in_results():
    old = {"v1": {"status": "up", "since": T0}, "gone": {"status": "down", "since": T0}}
    new, events = state.apply_results(old, [res("v1", True)], now=T1)
    assert list(new) == ["v1"], new
    assert events == [], "ушедший из конфига коллектор не должен давать событий"


def test_state_reason_for_failure_kind():
    r = res("v1", False)
    r.update({"status_code": None, "failure_kind": "timeout"})
    _, events = state.apply_results({}, [r], now=T1)
    assert events[0]["reason"] == "timeout", events


def test_state_roundtrip():
    original = {"v1": {"status": "up", "since": T0}, "v7": {"status": "down", "since": T1}}
    text = state.render_state(original)
    assert state.parse_state(text) == original
    assert "v7 down 2026-10-12T16:00:00" in text, text


def test_state_parse_bad_lines_are_skipped_with_warning():
    for bad in ("v1 up\n", "v1 sideways 2026-10-12T16:00:00\n", "v1 up not-a-date\n",
                "v1 up 2026-10-12T16:00:00 extra\n"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert state.parse_state(bad) == {}, bad
        assert "строка 1 пропущена" in err.getvalue(), err.getvalue()


def test_state_parse_keeps_good_lines_next_to_bad():
    text = "v1 up 2026-10-12T16:00:00\nv3 Up 2026-10-12T16:00:00\nv7 down 2026-10-12T15:00:00\n"
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        got = state.parse_state(text)
    assert list(got) == ["v1", "v7"] and got["v7"]["status"] == "down", got
    assert "строка 2" in err.getvalue()


def test_state_load_missing_path_warns():
    with tempfile.TemporaryDirectory() as tmp:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = state.load_state(Path(tmp) / "nope.txt")
    assert got == {}
    assert err.getvalue().strip(), "предупреждение должно быть в stderr"


def test_state_load_corrupt_file_gives_empty():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.txt"
        path.write_text("мусор\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = state.load_state(path)
    assert got == {} and err.getvalue().strip()


def test_state_save_uses_tmp_and_replace():
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((str(src), str(dst), os.path.exists(src)))
        return real_replace(src, dst)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.txt"
        with mock.patch.object(state.os, "replace", spy):
            ok = state.save_state({"v1": {"status": "up", "since": T0}}, path)
        assert ok is True
        assert calls == [(str(path) + ".tmp", str(path), True)], calls
        assert not Path(str(path) + ".tmp").exists(), ".tmp остался после записи"
        assert state.load_state(path) == {"v1": {"status": "up", "since": T0}}


def test_state_save_unwritable_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = state.save_state({}, blocker / "state.txt")
    assert ok is False and err.getvalue().strip()


# ---------------------------------------------------------------------------
# alert
# ---------------------------------------------------------------------------

def texts(batches):
    return [b["text"] for b in batches]


def test_alert_empty_names_give_nothing():
    assert alert.fall_batches([]) == [] and alert.remind_batches([]) == []
    assert alert.up_batches([]) == []


def test_alert_fall_single_and_list():
    assert texts(alert.fall_batches(["v1", "v7"])) == [
        "🔴 Обнаружено падение, v1", "🔴 Обнаружено падение, v7"]
    assert texts(alert.fall_batches(["v1", "v3", "v7"])) == [
        "🔴 Обнаружено падение коллекторов:\n- Коллектор v1\n- Коллектор v3\n- Коллектор v7"]


def test_alert_remind_single_and_list():
    assert texts(alert.remind_batches(["v7", "v8"])) == [
        "🔴 Коллектор v7 всё ещё не работает", "🔴 Коллектор v8 всё ещё не работает"]
    assert texts(alert.remind_batches(["v1", "v7", "v8"])) == [
        "🔴 Коллекторы всё ещё не работают:\n- Коллектор v1\n- Коллектор v7\n- Коллектор v8"]


def test_alert_up_single_and_list():
    # Формат Вани 2026-10-13: заголовок и по строке на коллектор.
    assert texts(alert.up_batches(["v1", "v2"])) == [
        "🟢 Коллектор v1 снова работает", "🟢 Коллектор v2 снова работает"]
    assert texts(alert.up_batches(["v5", "v8", "v10"])) == [
        "🟢 Коллектор снова работает:\n- Коллектор v5\n- Коллектор v8\n- Коллектор v10"]


def test_alert_marks_kinds_and_names():
    fall, remind, up = (alert.fall_batches(["v7"])[0], alert.remind_batches(["v7"])[0],
                        alert.up_batches(["v7"])[0])
    assert fall["text"].startswith(alert.DOWN_MARK + " ") and fall["kind"] == "fall"
    assert remind["text"].startswith(alert.DOWN_MARK + " ") and remind["kind"] == "remind"
    assert up["text"].startswith(alert.UP_MARK + " ") and up["kind"] == "up"
    group = alert.remind_batches(["v1", "v2", "v3"])
    assert [b["names"] for b in group] == [["v1", "v2", "v3"]], group


# ---------------------------------------------------------------------------
# reminder: таймер и выбор сообщений по минутам (AC-14..AC-18)
# ---------------------------------------------------------------------------

def at(h, m, s=0):
    return datetime(2026, 10, 14, h, m, s)


class Runs:
    """Прогоны подряд в том же порядке, что main.run, без сети и файлов."""

    def __init__(self, names, start=None):
        self.names = names
        self.state, self.timer, self.events = {}, None, []
        self.run(start or at(9, 0))

    def run(self, now, down=(), fail=()):
        """down - кто не ответил; fail - чьи сообщения не уходят ("*" - все)."""
        previous = self.state
        timer = reminder.drop_stale(self.timer, previous)
        new, self.events = state.apply_results(
            previous, [res(n, n not in down) for n in self.names], now=now)
        batches = reminder.plan_batches(new, self.events, timer, now)
        unsent = [n for b in batches for n in b["names"] if "*" in fail or n in fail]
        self.state, self.timer = reminder.settle(new, previous, timer, batches, unsent, now)
        return texts(batches)


def test_reminder_ac14_one_collector_every_five_minutes():
    r = Runs(["v1", "v7"])
    assert r.run(at(10, 0, 40), down={"v7"}) == ["🔴 Обнаружено падение, v7"]
    assert r.timer == {"last_change": at(10, 0, 40), "alert_last_sent": at(10, 0, 40)}
    assert r.run(at(10, 1, 30), down={"v7"}) == []
    assert r.run(at(10, 4, 59), down={"v7"}) == []
    # По минуте запуска: 10:05:20 после 10:00:40 - это уже отметка 10:05.
    assert r.run(at(10, 5, 20), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]
    assert r.run(at(10, 6, 30), down={"v7"}) == []
    assert r.run(at(10, 9, 59), down={"v7"}) == []
    assert r.run(at(10, 10, 10), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]
    assert r.state["v7"] == {"status": "down", "since": at(10, 0, 40)}
    assert [e["event"] for e in r.events] == ["still_down"]


def test_reminder_ac15_new_fall_waits_for_mark():
    # Пример Вани: 20:00 упал один - алерт и таймер; 20:03 упал второй - ждёт 20:05.
    r = Runs(["v1", "v7", "v8", "v9", "v10"])
    assert r.run(at(20, 0), down={"v7"}) == ["🔴 Обнаружено падение, v7"]
    for minute in (1, 2):
        assert r.run(at(20, minute), down={"v7"}) == []
    for minute in (3, 4):
        assert r.run(at(20, minute), down={"v7", "v8"}) == [], minute
    assert r.state["v8"]["status"] == "pending"
    assert r.run(at(20, 5), down={"v7", "v8"}) == [
        "🔴 Коллектор v7 всё ещё не работает", "🔴 Коллектор v8 всё ещё не работает"]
    assert r.state["v8"] == {"status": "down", "since": at(20, 3)}
    for minute in (6, 7, 8, 9):
        assert r.run(at(20, minute), down={"v7", "v8"}) == []
    assert len(r.run(at(20, 10), down={"v7", "v8"})) == 2
    assert r.timer["last_change"] == at(20, 0), "новое падение шаг не сдвигает"


def test_reminder_ac15_three_and_more_go_as_one_list_in_config_order():
    r = Runs(["v1", "v7", "v8", "v9", "v10"])
    r.run(at(20, 0), down={"v7"})
    assert r.run(at(20, 3), down={"v10", "v8", "v9", "v7"}) == []
    assert r.run(at(20, 5), down={"v10", "v8", "v9", "v7"}) == [
        "🔴 Коллекторы всё ещё не работают:\n- Коллектор v7\n- Коллектор v8"
        "\n- Коллектор v9\n- Коллектор v10"]


def test_reminder_ac15_fell_and_rose_between_marks_is_silent():
    r = Runs(["v1", "v7", "v8"])
    r.run(at(20, 0), down={"v7"})
    assert r.run(at(20, 3), down={"v7", "v8"}) == []
    assert r.run(at(20, 4), down={"v7"}) == []
    assert [(e["event"], e["name"]) for e in r.events] == [("still_down", "v7"), ("quiet_up", "v8")]
    assert r.run(at(20, 5), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]


def test_reminder_ac15_reported_rises_mark_stays():
    r = Runs(["v1", "v7", "v8"])
    r.run(at(20, 0), down={"v7"})
    r.run(at(20, 3), down={"v7", "v8"})
    r.run(at(20, 5), down={"v7", "v8"})
    assert r.run(at(20, 7), down={"v8"}) == ["🟢 Коллектор v7 снова работает"]
    assert r.timer["last_change"] == at(20, 0)
    assert r.run(at(20, 10), down={"v8"}) == ["🔴 Коллектор v8 всё ещё не работает"]


def test_reminder_ac16_unsent_remind_repeats_next_run_mark_unchanged():
    r = Runs(["v1", "v7"])
    r.run(at(10, 0), down={"v7"})
    assert r.run(at(10, 5), down={"v7"}, fail={"*"}) == ["🔴 Коллектор v7 всё ещё не работает"]
    assert r.timer["alert_last_sent"] == at(10, 0)
    assert r.run(at(10, 6), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]
    for minute in (7, 8, 9):
        assert r.run(at(10, minute), down={"v7"}) == []
    assert r.run(at(10, 10), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]


def test_reminder_ac16_partial_remind_failure_keeps_mark():
    r = Runs(["v1", "v7"])
    r.run(at(10, 0), down={"v1", "v7"})
    assert r.run(at(10, 5), down={"v1", "v7"}, fail={"v7"}) == [
        "🔴 Коллектор v1 всё ещё не работает", "🔴 Коллектор v7 всё ещё не работает"]
    assert r.timer["alert_last_sent"] == at(10, 0), "не ушло хоть одно - отметка прежняя"
    assert len(r.run(at(10, 6), down={"v1", "v7"})) == 2


def test_reminder_ac16_unsent_fall_stays_pending_and_repeats():
    r = Runs(["v1", "v7"])
    assert r.run(at(10, 0), down={"v7"}, fail={"*"}) == ["🔴 Обнаружено падение, v7"]
    assert r.state["v7"]["status"] == "pending" and r.timer is None
    assert r.run(at(10, 1), down={"v7"}) == ["🔴 Обнаружено падение, v7"]
    assert r.state["v7"]["status"] == "down" and r.timer["last_change"] == at(10, 1)


def test_reminder_ac17_all_up_resets_timer():
    r = Runs(["v1", "v7"])
    r.run(at(10, 0), down={"v7"})
    assert r.run(at(10, 7)) == ["🟢 Коллектор v7 снова работает"]
    assert r.timer is None
    assert r.run(at(10, 9), down={"v7"}) == ["🔴 Обнаружено падение, v7"]
    assert r.run(at(10, 10), down={"v7"}) == []
    assert r.run(at(10, 13), down={"v7"}) == []
    assert r.run(at(10, 14), down={"v7"}) == ["🔴 Коллектор v7 всё ещё не работает"]


def test_reminder_ac17_unsent_up_keeps_timer():
    r = Runs(["v1", "v7"])
    r.run(at(10, 0), down={"v7"})
    assert r.run(at(10, 2), fail={"*"}) == ["🟢 Коллектор v7 снова работает"]
    assert r.state["v7"]["status"] == "down" and r.timer is not None
    assert r.run(at(10, 3)) == ["🟢 Коллектор v7 снова работает"]
    assert r.timer is None


def test_reminder_ac17_lost_timer_reminds_now():
    previous = {"v7": {"status": "down", "since": at(9, 0)}}
    new, events = state.apply_results(previous, [res("v7", False)], now=at(10, 2))
    batches = reminder.plan_batches(new, events, None, at(10, 2))
    assert texts(batches) == ["🔴 Коллектор v7 всё ещё не работает"]
    _, timer = reminder.settle(new, previous, None, batches, [], at(10, 2))
    assert timer == {"last_change": at(10, 2), "alert_last_sent": at(10, 2)}


def test_reminder_drop_stale_timer_without_lying():
    timer = {"last_change": at(9, 0), "alert_last_sent": at(9, 0)}
    assert reminder.drop_stale(timer, {"v7": {"status": "up", "since": at(9, 0)}}) is None
    assert reminder.drop_stale(timer, {"v7": {"status": "pending", "since": at(9, 0)}}) == timer
    assert reminder.drop_stale(None, {"v7": {"status": "down", "since": at(9, 0)}}) is None


def test_reminder_ac18_three_fall_together_and_remind_before_up():
    r = Runs(["v1", "v2", "v3", "v7"])
    assert r.run(at(10, 0), down={"v1", "v3", "v7"}) == [
        "🔴 Обнаружено падение коллекторов:\n- Коллектор v1\n- Коллектор v3\n- Коллектор v7"]
    assert r.run(at(10, 5), down={"v1", "v3"}) == [
        "🔴 Коллектор v1 всё ещё не работает", "🔴 Коллектор v3 всё ещё не работает",
        "🟢 Коллектор v7 снова работает"]


def test_reminder_is_due_respects_every_min():
    timer = {"last_change": at(10, 0, 59), "alert_last_sent": at(10, 0, 59)}
    assert not reminder.is_due(timer, at(10, 4, 59))
    assert reminder.is_due(timer, at(10, 5, 0))
    assert reminder.is_due(timer, at(10, 2), every_min=2)
    assert not reminder.is_due(timer, at(9, 59)), "часы назад - не повод слать"


def test_reminder_timer_file_roundtrip_and_delete():
    with tempfile.TemporaryDirectory() as tmp:
        path = reminder.timer_path(Path(tmp) / "state.txt")
        assert path.name == "state_down.txt"
        timer = {"last_change": at(20, 0, 40), "alert_last_sent": at(20, 5, 3)}
        assert reminder.save_timer(timer, path) is True
        text = path.read_text(encoding="utf-8")
        assert "last_change=2026-10-14T20:00:40\nalert_last_sent=2026-10-14T20:05:03\n" in text, text
        assert reminder.load_timer(path) == timer
        assert not Path(str(path) + ".tmp").exists()
        assert reminder.save_timer(None, path) is True and not path.exists()
        assert reminder.save_timer(None, path) is True, "удалять нечего - тоже успех"
        assert reminder.load_timer(path) is None


def test_reminder_bad_timer_file_is_none_with_warning():
    # Черновик Вани 14.10 был в секундах unix: читается как сброшенный таймер.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state_down.txt"
        for text in ("# aleting tc\nlast_change=1728901234\nalert_last_sent=1728901200",
                     "last_change=2026-10-14T20:00:40\n", "мусор\n"):
            path.write_text(text, encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert reminder.load_timer(path) is None, text
            assert "таймер не разобран" in err.getvalue(), err.getvalue()


def test_reminder_save_timer_unwritable_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "file"
        blocker.write_text("x", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = reminder.save_timer({"last_change": at(1, 0), "alert_last_sent": at(1, 0)},
                                     blocker / "state_down.txt")
    assert ok is False and "таймер не сохранён" in err.getvalue()


def test_alert_send_batches_returns_unsent_names():
    batches = [{"text": "a", "names": ["v1", "v2"]}, {"text": "b", "names": ["v3"]},
               {"text": "c", "names": ["v4"]}]
    seen = []
    def sender(text):
        seen.append(text)
        return text != "a" and text != "c"
    assert alert.send_batches(batches, sender) == ["v1", "v2", "v4"]
    assert seen == ["a", "b", "c"], "после неудачи отправка не останавливается"


def test_state_keep_unreported_reverts_or_drops():
    old = {"v1": {"status": "down", "since": T0}, "v2": {"status": "up", "since": T0}}
    new = {"v1": {"status": "up", "since": T1}, "v2": {"status": "down", "since": T1},
           "v3": {"status": "down", "since": T1}}
    got = state.keep_unreported(new, old, ["v1", "v3"])
    assert got["v1"] == old["v1"], "неотправленный подъём: статус остаётся down"
    assert got["v2"] == new["v2"], "отправленное падение продвинуто"
    assert "v3" not in got, "первый запуск без записи: коллектор выпадает, чтобы упасть снова"


def test_alert_group_min_parameter():
    assert texts(alert.fall_batches(["v1", "v2"], group_min=2)) == [
        "🔴 Обнаружено падение коллекторов:\n- Коллектор v1\n- Коллектор v2"]
    assert len(alert.fall_batches(["v1", "v2"], group_min=GROUP_MIN_DOWN)) == 2


def test_runlog_sent_line():
    line = runlog.sent_line({"kind": "remind", "text": "t", "names": ["v7", "v8"]})
    assert re.fullmatch(STAMP + r" sent remind v7, v8", line), line




STAMP = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"


def attempts_n(n):
    return [{"status_code": 503, "failure_kind": None, "elapsed_ms": 1}] * n


def test_runlog_run_line_all_ok():
    line = runlog.run_line([res("v1", True), res("v2", True)])
    assert re.fullmatch(STAMP + r" run checked=2 down=0", line), line


def test_runlog_run_line_with_failures():
    r = res("v7", False)
    r["attempts"] = attempts_n(3)
    line = runlog.run_line([res("v1", True), r])
    assert re.fullmatch(STAMP + r" run checked=2 down=1 \| v7 HTTP 503 \(3 попытки\)", line), line


def test_runlog_attempts_declension():
    for n, word in ((1, "попытка"), (2, "попытки"), (3, "попытки"), (4, "попытки"),
                    (5, "попыток"), (11, "попыток"), (12, "попыток"), (21, "попытка")):
        r = res("v1", False)
        r["attempts"] = attempts_n(n)
        line = runlog.run_line([r])
        assert line.endswith("({} {})".format(n, word)), line


def test_runlog_event_lines():
    events = [{"event": "down", "name": "v7", "since": T0, "reason": "HTTP 503"},
              {"event": "up", "name": "v2", "since": T1, "reason": "HTTP 200"}]
    lines = runlog.event_lines(events)
    assert len(lines) == 2
    assert re.fullmatch(STAMP + " event down v7 since 2026-10-12 15:00:00 HTTP 503", lines[0]), lines[0]
    assert re.fullmatch(STAMP + " event up v2 since 2026-10-12 16:00:00 HTTP 200", lines[1]), lines[1]
    assert runlog.event_lines([]) == []


NOW14 = datetime(2026, 10, 14, 10, 0, 0)


def read_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_runlog_trims_lines_older_than_week():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        path.write_text("2026-10-06 09:00:00 run old1\n2026-10-06 23:59:00 run old2\n"
                        "2026-10-08 00:00:00 run keep1\n2026-10-13 12:00:00 run keep2\n",
                        encoding="utf-8")
        assert runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14) is True
        assert read_lines(path) == ["2026-10-08 00:00:00 run keep1",
                                    "2026-10-13 12:00:00 run keep2",
                                    "2026-10-14 10:00:00 run new"]


def test_runlog_keeps_boundary_line_and_respects_keep_days():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        # Ровно 7 суток назад: не старше срока, остаётся; старше больше чем на час запаса - уходит.
        path.write_text("2026-10-07 08:59:00 run gone\n2026-10-07 10:00:00 run edge\n",
                        encoding="utf-8")
        runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14)
        assert read_lines(path) == ["2026-10-07 10:00:00 run edge",
                                    "2026-10-14 10:00:00 run new"]
        runlog.append(["2026-10-14 10:01:00 run new2"], path, now=NOW14, keep_days=3)
        assert read_lines(path) == ["2026-10-14 10:00:00 run new",
                                    "2026-10-14 10:01:00 run new2"]


def test_runlog_unstamped_line_does_not_break_trim():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        path.write_text("2026-10-06 09:00:00 run old\nмусор без метки\n"
                        "2026-10-08 00:00:00 run keep\n", encoding="utf-8")
        assert runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14) is True
        assert read_lines(path) == ["2026-10-08 00:00:00 run keep", "2026-10-14 10:00:00 run new"]


def test_runlog_unstamped_line_after_fresh_is_kept():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        path.write_text("2026-10-06 09:00:00 run old\n2026-10-08 00:00:00 run keep\nхвост\n",
                        encoding="utf-8")
        runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14)
        assert read_lines(path) == ["2026-10-08 00:00:00 run keep", "хвост",
                                    "2026-10-14 10:00:00 run new"]


def test_runlog_hour_slack_leaves_file_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        # Первая строка старше срока на 30 минут: до часа файл не переписывается.
        path.write_text("2026-10-07 09:30:00 run barely_old\n", encoding="utf-8")
        runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14)
        assert read_lines(path) == ["2026-10-07 09:30:00 run barely_old",
                                    "2026-10-14 10:00:00 run new"]
        # Старше срока больше чем на час - режется.
        path.write_text("2026-10-07 08:30:00 run old\n", encoding="utf-8")
        runlog.append(["2026-10-14 10:00:00 run new"], path, now=NOW14)
        assert read_lines(path) == ["2026-10-14 10:00:00 run new"]


def make_daily_logs(tmp):
    logs = Path(tmp) / "logs"
    logs.mkdir()
    names = ["collector_checker_2026-10-06.log", "collector_checker_2026-10-07.log",
             "collector_checker_2026-10-08.log", "collector_checker_2026-10-14.log",
             "notes.txt", "collector_checker_мусор.log"]
    for n in names:
        (logs / n).write_text("x", encoding="utf-8")
    return logs


def test_prune_daily_logs_removes_only_old_daily_files():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_daily_logs(tmp)
        assert runlog.prune_daily_logs(logs, today=date(2026, 10, 14)) == 1
        assert sorted(p.name for p in logs.iterdir()) == [
            "collector_checker_2026-10-07.log", "collector_checker_2026-10-08.log",
            "collector_checker_2026-10-14.log", "collector_checker_мусор.log", "notes.txt"]


def test_prune_daily_logs_keep_days_and_missing_dir():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_daily_logs(tmp)
        assert runlog.prune_daily_logs(logs, today=date(2026, 10, 14), keep_days=1) == 3
        assert (logs / "collector_checker_2026-10-14.log").exists()
        assert (logs / "notes.txt").exists()
        assert runlog.prune_daily_logs(Path(tmp) / "нет", today=date(2026, 10, 14)) == 0


def test_prune_daily_logs_os_error_is_warning():
    with tempfile.TemporaryDirectory() as tmp:
        logs = make_daily_logs(tmp)
        err = io.StringIO()
        with mock.patch.object(runlog.os, "remove", side_effect=PermissionError("нет прав")):
            with contextlib.redirect_stderr(err):
                assert runlog.prune_daily_logs(logs, today=date(2026, 10, 14)) == 0
        assert "не удалён" in err.getvalue()


def test_runlog_append_empty_makes_no_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        assert runlog.append([], path) is True
        assert not path.exists()


def test_runlog_append_unwritable_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        assert runlog.append(["x"], Path(tmp) / "no_dir" / "runs.log") is False


# ---------------------------------------------------------------------------
# main.choose_sender
# ---------------------------------------------------------------------------

def choose(no_notify, env):
    err = io.StringIO()
    clean = {k: v for k, v in os.environ.items()
             if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    clean.update(env)
    with mock.patch.dict(os.environ, clean, clear=True), contextlib.redirect_stderr(err):
        return main_module.choose_sender(no_notify), err.getvalue()


def test_main_no_notify_gives_printing_sender():
    sender, _ = choose(True, {})
    assert sender is notifier.printing_sender


def test_main_missing_both_secrets():
    sender, err = choose(False, {})
    assert sender is None
    assert "TELEGRAM_BOT_TOKEN" in err and "TELEGRAM_CHAT_ID" in err, err


def test_main_missing_chat_only():
    sender, err = choose(False, {"TELEGRAM_BOT_TOKEN": "secret-value"})
    assert sender is None
    assert "TELEGRAM_CHAT_ID" in err and "TELEGRAM_BOT_TOKEN" not in err, err
    assert "secret-value" not in err


def test_main_missing_token_only():
    sender, err = choose(False, {"TELEGRAM_CHAT_ID": "42"})
    assert sender is None
    assert "TELEGRAM_BOT_TOKEN" in err and "TELEGRAM_CHAT_ID" not in err, err


def test_main_both_secrets_give_callable():
    sender, err = choose(False, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"})
    assert callable(sender) and sender is not notifier.printing_sender and err == ""


def test_main_choose_sender_strips_cr_and_spaces():
    seen = []
    original = main_module.make_sender
    main_module.make_sender = lambda token, chat: seen.append((token, chat)) or (lambda t: True)
    try:
        choose(False, {"TELEGRAM_BOT_TOKEN": "123:abc\r", "TELEGRAM_CHAT_ID": " 1 "})
    finally:
        main_module.make_sender = original
    assert seen == [("123:abc", "1")], seen


# ---------------------------------------------------------------------------
# main.run (без сети: подмены атрибутов модуля main)
# ---------------------------------------------------------------------------

def run_main(argv, env=None, **patches):
    clean = {k: v for k, v in os.environ.items()
             if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    clean.update(env or {})
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, clean, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        for name, value in patches.items():
            setattr(main_module, name, value)
        try:
            code = main_module.run(main_module.parse_args(argv))
        finally:
            for name, value in saved_main.items():
                setattr(main_module, name, value)
    return code, out.getvalue(), err.getvalue()


saved_main = {n: getattr(main_module, n)
              for n in ("check_all", "load_collectors", "save_state", "send_batches")}


def forbidden_check_all(collectors):
    raise AssertionError("check_all не должен вызываться")


def test_run_without_secrets_does_not_probe():
    # Список существует и читается: отказ должен прийти от секретов, а не от load_collectors.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.txt"
        path.write_text("v1 http://127.0.0.1:1/v1\n", encoding="utf-8")
        code, _, err = run_main(["--collectors", str(path),
                                 "--state", str(Path(tmp) / "s.txt"),
                                 "--runlog", str(Path(tmp) / "r.log")],
                                check_all=forbidden_check_all)
    assert code == 1 and "TELEGRAM_BOT_TOKEN" in err, (code, err)


def test_run_failed_send_keeps_state_for_retry_and_returns_0():
    saved = []
    down = {"name": "v7", "url": "u", "ok": False, "status_code": 503, "failure_kind": None,
            "error": None, "attempts": [{"status_code": 503, "failure_kind": None,
                                         "elapsed_ms": 1}], "checked_at": T0}
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "runs.log"
        code, _, err = run_main(
            ["--runlog", str(log), "--state", str(Path(tmp) / "s.txt")],
            env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
            load_collectors=lambda path: [{"name": "v7", "url": "u"}],
            check_all=lambda collectors: [down],
            send_batches=lambda batches, sender: ["v7"],
            save_state=lambda st, path: saved.append(st) or True)
        assert "unsent v7" in log.read_text(encoding="utf-8")
    # Сбой канала - не сбой скрипта: код 0, а v7 остаётся несообщённым (pending),
    # и следующий прогон отправит падение снова.
    assert code == 0, (code, err)
    assert [{n: e["status"] for n, e in st.items()} for st in saved] == [{"v7": "pending"}], saved
    assert "не отправлено: v7" in err, err


def test_run_runlog_flag_sets_path():
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "mine.log"
        code, _, err = run_main(
            ["--no-notify", "--runlog", str(log), "--state", str(Path(tmp) / "s.txt")],
            load_collectors=lambda path: [{"name": "v7", "url": "u"}],
            check_all=lambda collectors: [{
                "name": "v7", "url": "u", "ok": True, "status_code": 200,
                "failure_kind": None, "error": None, "checked_at": T0,
                "attempts": [{"status_code": 200, "failure_kind": None, "elapsed_ms": 1}]}])
        assert code == 0, err
        assert "run checked=1" in log.read_text(encoding="utf-8")


def test_run_cp1251_list_gives_readable_error():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.txt"
        path.write_bytes("# коллекторы\nv1 https://h/v1\n".encode("cp1251"))
        code, _, err = run_main(["--no-notify", "--collectors", str(path)],
                                check_all=forbidden_check_all)
    assert code == 1 and "список не загружен" in err and "UTF-8" in err, (code, err)


def test_run_directory_instead_of_list_gives_readable_error():
    with tempfile.TemporaryDirectory() as tmp:
        code, _, err = run_main(["--no-notify", "--collectors", tmp],
                                check_all=forbidden_check_all)
    assert code == 1 and "список не загружен" in err, (code, err)


def test_run_unreadable_list_gives_readable_error():
    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    code, _, err = run_main(["--no-notify"], load_collectors=denied,
                            check_all=forbidden_check_all)
    assert code == 1 and "список не загружен" in err, (code, err)


# ---------------------------------------------------------------------------
# notifier (без сети: urlopen подменяется на уровне модуля)
# ---------------------------------------------------------------------------

TOKEN = "TESTTOKEN:abc_DEF-123"
TG_ENDPOINTS = list(TELEGRAM_IPS) + [TELEGRAM_HOST]








def test_probe_session_ignores_env_proxy():
    with probe._new_session() as session:
        assert session.trust_env is False


def test_sources_accepts_url_with_query():
    # Пиксель v16/i отвечает 200 только с параметрами, они живут прямо в адресе.
    got = sources.parse_collectors("v16 https://h/v16/i?t=event&dl=collector-checker\n")
    assert got == [{"name": "v16", "url": "https://h/v16/i?t=event&dl=collector-checker"}], got


def test_sources_load_collectors_accepts_bom():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.txt"
        path.write_bytes("\ufeffv1 https://h/v1\n".encode("utf-8"))
        loaded = sources.load_collectors(path)
    assert [c["name"] for c in loaded] == ["v1"], loaded


def test_state_load_accepts_bom():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.txt"
        path.write_bytes("\ufeffv1 up 2026-10-12T15:00:00\n".encode("utf-8"))
        loaded = state.load_state(path)
    assert list(loaded) == ["v1"], loaded

















def call_notifier(actions, token=TOKEN, **kwargs):
    """actions: по одному на вызов notifier._post - кортеж (status, тело) или исключение.
    Возвращает результат, список (endpoint, path, payload) и stderr."""
    seen = []
    queue = list(actions)

    def fake_post(endpoint, path, payload, timeout):
        seen.append((endpoint, path, payload))
        action = queue.pop(0)
        if isinstance(action, BaseException):
            raise action
        status, body = action
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    original = notifier._post
    notifier._post = fake_post
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            result = notifier.send_to_telegram(token, "42", "привет", sleep=kwargs.pop("sleep", lambda s: None), **kwargs)
    finally:
        notifier._post = original
    return result, seen, err.getvalue()


def test_ok_200():
    result, seen, _ = call_notifier([(200, {"ok": True})])
    assert result is True and len(seen) == 1
    assert seen[0][0] == TELEGRAM_IPS[0], "первым идёт IP, как у коллег"


def test_non_200_with_ok_true():
    assert call_notifier([(500, {"ok": True})])[0] is False


def test_200_but_ok_false():
    assert call_notifier([(200, {"ok": False})])[0] is False


def test_401_description_without_token():
    result, seen, err = call_notifier([(401, {"ok": False, "description": "Unauthorized"})])
    assert result is False and len(seen) == 1, "ответ сервера, перебор адресов не нужен"
    assert "Unauthorized" in err and TOKEN not in err, err


def test_429_retries_once_and_succeeds():
    sleeps = []
    result, seen, _ = call_notifier([(429, {"parameters": {"retry_after": 0}}), (200, {"ok": True})],
                                    sleep=sleeps.append)
    assert result is True and len(seen) == 2 and sleeps == [0], (result, len(seen), sleeps)


def test_429_twice_gives_up():
    body = {"parameters": {"retry_after": 0}}
    result, seen, _ = call_notifier([(429, body), (429, body)])
    assert result is False and len(seen) == 2


def test_429_retry_after_is_capped_at_60():
    sleeps = []
    call_notifier([(429, {"parameters": {"retry_after": 3600}}), (200, {"ok": True})], sleep=sleeps.append)
    assert sleeps == [60], sleeps


def test_token_with_crlf_gives_clean_path():
    result, seen, _ = call_notifier([(200, {"ok": True})], token="T:abc\r\n")
    path = seen[0][1]
    assert result is True and "\r" not in path and "\n" not in path, path
    assert path == "/botT:abc/sendMessage", path


def test_network_error_hides_token():
    exc = notifier.NetworkError("не достучаться до /bot{}/sendMessage".format(TOKEN))
    result, _, err = call_notifier([exc] * len(TG_ENDPOINTS))
    assert result is False and err.strip() and TOKEN not in err and "***" in err, err


def test_network_error_falls_back_to_next_endpoint():
    exc = notifier.NetworkError("timed out")
    sleeps = []
    result, seen, err = call_notifier([exc, exc, (200, {"ok": True})], sleep=sleeps.append)
    assert result is True, err
    assert [e for e, _, _ in seen] == TG_ENDPOINTS[:3], seen
    assert sleeps == [TELEGRAM_ENDPOINT_PAUSE, TELEGRAM_ENDPOINT_PAUSE], sleeps
    assert "ушло через {}".format(TG_ENDPOINTS[2]) in err, err


def test_all_endpoints_down():
    exc = notifier.NetworkError("timed out")
    sleeps = []
    result, seen, err = call_notifier([exc] * len(TG_ENDPOINTS), sleep=sleeps.append)
    assert result is False and len(seen) == len(TG_ENDPOINTS)
    assert len(sleeps) == len(TG_ENDPOINTS) - 1, "после последнего адреса пауза не нужна"
    assert "ни по одному" in err, err


def test_http_error_stops_endpoint_iteration():
    result, seen, _ = call_notifier([(400, {"description": "Bad Request"})])
    assert result is False and len(seen) == 1


def test_request_shape():
    _, seen, _ = call_notifier([(200, {"ok": True})])
    endpoint, path, payload = seen[0]
    assert path == "/bot{}/sendMessage".format(TOKEN), path
    body = json.loads(payload.decode("utf-8"))
    assert body["chat_id"] == "42" and body["text"] == "привет" and body["disable_web_page_preview"] is True, body


def test_invalid_json():
    assert call_notifier([(200, b"<html>not json")])[0] is False


def test_endpoints_order_is_host_then_ips():
    assert notifier.endpoints() == TG_ENDPOINTS


def test_cert_name_matching():
    cert = {"subjectAltName": (("DNS", "*.telegram.org"), ("DNS", "telegram.org"))}
    assert notifier._name_matches(cert, "api.telegram.org")
    assert notifier._name_matches({"subjectAltName": (("DNS", "api.telegram.org"),)}, "api.telegram.org")
    assert not notifier._name_matches(cert, "api.evil.com")
    assert not notifier._name_matches(cert, "deep.api.telegram.org"), "маска покрывает один уровень"
    assert not notifier._name_matches({}, "api.telegram.org")



def test_ip_connection_rejects_foreign_certificate():
    class FakeSock:
        def getpeercert(self):
            return {"subjectAltName": (("DNS", "*.evil.com"),)}

    class FakeConn:
        def __init__(self, *a, **k):
            self.sock = FakeSock()
            self.closed = False

        def connect(self):
            pass

        def close(self):
            self.closed = True

    made = []
    original = notifier.http.client.HTTPSConnection
    notifier.http.client.HTTPSConnection = lambda *a, **k: made.append(FakeConn(*a, **k)) or made[-1]
    try:
        raises(notifier.NetworkError, notifier._connection, TELEGRAM_IPS[0], 5)
    finally:
        notifier.http.client.HTTPSConnection = original
    assert made and made[0].closed, "соединение с чужим сертификатом должно закрываться"


def test_ip_connection_accepts_anything_when_verify_off():
    class FakeConn:
        def __init__(self, *a, **k):
            self.sock = type("S", (), {"getpeercert": lambda self: {"subjectAltName": (("DNS", "*.evil.com"),)}})()
            self.context = k.get("context")

        def connect(self):
            pass

    made = []
    original_conn, original_flag = notifier.http.client.HTTPSConnection, notifier.TELEGRAM_VERIFY_CERT
    notifier.http.client.HTTPSConnection = lambda *a, **k: made.append(FakeConn(*a, **k)) or made[-1]
    notifier.TELEGRAM_VERIFY_CERT = False
    try:
        conn = notifier._connection(TELEGRAM_IPS[0], 5)
    finally:
        notifier.http.client.HTTPSConnection, notifier.TELEGRAM_VERIFY_CERT = original_conn, original_flag
    assert conn is made[0] and conn.context.verify_mode == notifier.ssl.CERT_NONE

def test_notify_tool_works_without_requests():
    # Обёртка зовёт notify.py и со сломанным venv: без requests он обязан дойти
    # до отправки. Сеть подменена: соединения падают, паузы нулевые.
    code = (
        "import sys, socket, time, runpy\n"
        "sys.modules['requests'] = None\n"
        "socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError('нет сети'))\n"
        "time.sleep = lambda s: None\n"
        "sys.argv = ['notify.py', 'test']\n"
        "runpy.run_path('tools/notify.py', run_name='__main__')\n")
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    env.update({"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_CHAT_ID": "1"})
    proc = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "недоступен" in proc.stderr and "requests" not in proc.stderr, proc.stderr


def test_make_sender_binds_arguments():
    seen = []
    original = notifier.send_to_telegram
    notifier.send_to_telegram = lambda t, c, text, *a, **k: seen.append((t, c, text)) or True
    try:
        assert notifier.make_sender("T", "C")("msg") is True
    finally:
        notifier.send_to_telegram = original
    assert seen == [("T", "C", "msg")], seen


def test_no_real_token_in_sources():
    import re
    root = Path(__file__).parent.parent
    for path in list((root / "modules").glob("**/*.py")) + [Path(__file__)]:
        assert not re.search(r"bot[0-9]", path.read_text(encoding="utf-8")), path.name


# ---------------------------------------------------------------------------
# Секреты в исходниках
# ---------------------------------------------------------------------------

def source_files():
    return sorted((ROOT / "modules").glob("**/*.py")) + [ROOT / "main.py"]


def test_no_bot_url_fragment_in_app_sources():
    paths = source_files()
    assert len(paths) >= 9, paths
    for path in paths:
        assert not re.search(r"bot[0-9]", path.read_text(encoding="utf-8")), path.name


def test_no_token_like_values_in_app_sources():
    for path in source_files():
        found = re.search(r"[0-9]{6,}:[A-Za-z0-9_-]{20,}", path.read_text(encoding="utf-8"))
        assert not found, "{}: {}".format(path.name, found and found.group(0)[:6])


# ---------------------------------------------------------------------------
# Раннер
# ---------------------------------------------------------------------------

def run_all() -> int:
    # Пути отказа пишут в лог по делу; в выводе тестов это выглядело бы как поломка.
    logging.disable(logging.CRITICAL)
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
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
    sys.exit(1 if run_all() else 0)
