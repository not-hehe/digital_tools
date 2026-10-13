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
import sys
import tempfile
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

import main as main_module
from modules import sources
from modules.alerting import alert, notifier, runlog, state
from modules.probe import checker as probe
from modules.probe import failures
from modules.config import ATTEMPT_DEADLINE, ATTEMPTS, GROUP_MIN_DOWN, RETRY_PAUSE

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


def test_sources_three_fields():
    e = raises(sources.BadCollectorLine, sources.parse_collectors, "# c\nv1 https://a/1 лишнее\n")
    assert "строка 2" in str(e), str(e)


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


def test_probe_503_then_200():
    result, session, sleeps = check([503, 200])
    assert result["ok"] is True
    assert len(result["attempts"]) == 2
    assert sleeps == [RETRY_PAUSE], sleeps
    assert result["status_code"] == 200


def test_probe_503_three_times():
    result, _, sleeps = check([503, 503, 503])
    assert result["ok"] is False
    assert len(result["attempts"]) == ATTEMPTS == 3
    assert len(sleeps) == 2, "пауза после последней попытки не нужна: {}".format(sleeps)
    assert result["status_code"] == 503


def test_probe_timeout_three_times():
    exc = requests.exceptions.ConnectTimeout("connect timed out")
    result, _, _ = check([exc, exc, exc])
    assert result["ok"] is False
    assert result["failure_kind"] == "timeout"
    assert result["status_code"] is None
    assert "timed out" in result["error"], result["error"]
    assert [a["failure_kind"] for a in result["attempts"]] == ["timeout"] * 3


def test_probe_redirect_is_down():
    result, _, _ = check([301, 301, 301])
    assert result["ok"] is False and result["status_code"] == 301


def test_probe_attempt_fields():
    result, _, _ = check([503, requests.exceptions.SSLError("x"), 200])
    attempts = result["attempts"]
    assert [a["status_code"] for a in attempts] == [503, None, 200], attempts
    assert [a["failure_kind"] for a in attempts] == [None, "tls", None], attempts
    for a in attempts:
        assert set(a) == {"status_code", "failure_kind", "elapsed_ms"}, a
        assert isinstance(a["elapsed_ms"], int) and a["elapsed_ms"] >= 0


def test_probe_custom_pause_and_attempts():
    result, _, sleeps = check([500, 500], attempts=2, pause=0.5)
    assert len(result["attempts"]) == 2 and sleeps == [0.5], sleeps


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowSession(ProbeSession):
    """Каждый get «длится» seconds по поддельным часам."""

    def __init__(self, script, clock, seconds):
        super().__init__(script)
        self.clock, self.seconds = clock, seconds

    def get(self, url, **kwargs):
        self.clock.now += self.seconds
        return super().get(url, **kwargs)


def check_slow(script, seconds):
    clock = FakeClock()
    session = SlowSession(script, clock, seconds)
    sleeps = []
    with mock.patch.object(probe.time, "monotonic", clock):
        result = probe.check_collector(COLLECTOR, session=session, sleep=sleeps.append)
    return result, session, sleeps


def test_probe_timeout_is_connect_read_tuple():
    _, session, _ = check([200], timeout=7)
    assert session.calls[0][1]["timeout"] == (7, 7), session.calls[0][1]


def test_probe_attempt_over_deadline_stops_retries():
    result, session, sleeps = check_slow([503, 503, 503], ATTEMPT_DEADLINE + 1)
    assert len(session.calls) == 1 and sleeps == [], (len(session.calls), sleeps)
    assert result["ok"] is False and result["failure_kind"] == "timeout", result
    assert result["status_code"] is None, "причина должна быть timeout, а не HTTP 503"


def test_probe_slow_200_is_not_ok():
    result, _, _ = check_slow([200], ATTEMPT_DEADLINE + 1)
    assert result["ok"] is False and result["failure_kind"] == "timeout", result


def test_probe_attempt_at_deadline_is_still_normal():
    result, session, _ = check_slow([503, 200], ATTEMPT_DEADLINE)
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
    assert new == {"v1": {"status": "down", "since": T1}}
    assert [(e["event"], e["name"], e["since"]) for e in events] == [("down", "v1", T1)]
    assert events[0]["reason"] == "HTTP 503"


def test_state_up_not_ok():
    new, events = state.apply_results({"v1": {"status": "up", "since": T0}},
                                      [res("v1", False)], now=T1)
    assert new["v1"] == {"status": "down", "since": T1}
    assert [e["event"] for e in events] == ["down"] and events[0]["since"] == T1


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


def test_state_parse_bad_lines():
    for bad in ("v1 up\n", "v1 sideways 2026-10-12T16:00:00\n", "v1 up not-a-date\n",
                "v1 up 2026-10-12T16:00:00 extra\n"):
        raises(ValueError, state.parse_state, bad)


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

def ev(kind, name):
    return {"event": kind, "name": name, "since": T1, "reason": "HTTP 503"}


def test_alert_no_events():
    assert alert.build_messages([]) == []


def test_alert_one_down():
    assert alert.build_messages([ev("down", "v7")]) == ["🔴 Обнаружено падение, v7"]


def test_alert_two_down_are_separate():
    got = alert.build_messages([ev("down", "v1"), ev("down", "v7")])
    assert got == ["🔴 Обнаружено падение, v1", "🔴 Обнаружено падение, v7"], got


def test_alert_three_down_grouped_in_input_order():
    events = [ev("down", "v1"), ev("down", "v3"), ev("down", "v7")]
    assert alert.build_messages(events) == ["🔴 Обнаружено падение: v1, v3, v7"]


def test_alert_still_down_is_silent():
    # Правило Вани 2026-10-13: про лежащий коллектор сообщаем один раз, не каждый час.
    events = [ev("still_down", "v1"), ev("still_down", "v2"), ev("still_down", "v3")]
    assert alert.build_messages(events) == []
    assert alert.build_messages([ev("still_down", "v3"), ev("down", "v7")]) == ["🔴 Обнаружено падение, v7"]


def test_alert_marks_in_texts():
    assert alert.build_messages([ev("down", "v7")])[0].startswith(alert.DOWN_MARK + " ")
    assert alert.build_messages([ev("up", "v7")])[0].startswith(alert.UP_MARK + " ")


def test_alert_up_message_after_downs():
    events = [ev("up", "v2"), ev("down", "v7")]
    assert alert.build_messages(events) == ["🔴 Обнаружено падение, v7",
                                            "🟢 Коллектор v2 снова работает"]


def test_alert_ups_are_never_grouped():
    events = [ev("up", "v1"), ev("up", "v2"), ev("up", "v3")]
    assert alert.build_messages(events) == ["🟢 Коллектор v1 снова работает",
                                            "🟢 Коллектор v2 снова работает",
                                            "🟢 Коллектор v3 снова работает"]


def test_alert_group_min_parameter():
    events = [ev("down", "v1"), ev("down", "v2")]
    assert alert.build_messages(events, group_min=2) == ["🔴 Обнаружено падение: v1, v2"]
    assert len(alert.build_messages(events, group_min=GROUP_MIN_DOWN)) == 2


def test_alert_send_all_counts_failures_and_does_not_stop():
    seen = []

    def sender(text):
        seen.append(text)
        return text != "b"

    assert alert.send_all(["a", "b", "c", "d"], sender) == 1
    assert seen == ["a", "b", "c", "d"], "после неудачи отправка продолжается"
    assert alert.send_all([], sender) == 0
    assert alert.send_all(["x", "y"], lambda t: False) == 2


# ---------------------------------------------------------------------------
# runlog
# ---------------------------------------------------------------------------

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


def line_count(path):
    return len(path.read_text(encoding="utf-8").splitlines())


def test_runlog_trims_over_slack():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        lines = ["line {}".format(i) for i in range(6000)]
        assert runlog.append(lines, path, max_lines=5000) is True
        content = path.read_text(encoding="utf-8").splitlines()
        assert len(content) == 5000, len(content)
        assert content[0] == "line 1000" and content[-1] == "line 5999", (content[0], content[-1])


def test_runlog_slack_leaves_file_alone():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "runs.log"
        assert runlog.append(["l{}".format(i) for i in range(5400)], path, max_lines=5000)
        assert line_count(path) == 5400, "в пределах люфта файл не переписывается"


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
              for n in ("check_all", "load_collectors", "save_state", "send_all")}


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


def test_run_failed_send_returns_1_but_saves_state():
    saved = []
    down = {"name": "v7", "url": "u", "ok": False, "status_code": 503, "failure_kind": None,
            "error": None, "attempts": [{"status_code": 503, "failure_kind": None,
                                         "elapsed_ms": 1}], "checked_at": T0}
    with tempfile.TemporaryDirectory() as tmp:
        code, _, _ = run_main(
            ["--runlog", str(Path(tmp) / "runs.log"), "--state", str(Path(tmp) / "s.txt")],
            env={"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"},
            load_collectors=lambda path: [{"name": "v7", "url": "u"}],
            check_all=lambda collectors: [down],
            send_all=lambda messages, sender: 1,
            save_state=lambda st, path: saved.append(st) or True)
    assert code == 1, code
    assert len(saved) == 1, "состояние должно сохраниться и при сбое отправки"


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


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.telegram.org/bot{}/sendMessage".format(TOKEN), code, "err", {},
        io.BytesIO(json.dumps(body).encode()))


def call_notifier(actions, **kwargs):
    """actions: по одному на вызов urlopen; исключение бросается, остальное возвращается."""
    seen = []
    queue = list(actions)

    def fake_urlopen(request, timeout=None):
        seen.append(request)
        action = queue.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    original = notifier.urllib.request.urlopen
    notifier.urllib.request.urlopen = fake_urlopen
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            result = notifier.send_to_telegram(TOKEN, "42", "привет", **kwargs)
    finally:
        notifier.urllib.request.urlopen = original
    return result, seen, err.getvalue()


def test_ok_200():
    result, seen, err = call_notifier([FakeResponse(200, {"ok": True, "result": {}})])
    assert result is True, "ожидали True"
    assert len(seen) == 1


def test_non_200_with_ok_true():
    result, _, _ = call_notifier([FakeResponse(201, {"ok": True})])
    assert result is False, "успех только при HTTP 200"


def test_200_but_ok_false():
    result, _, _ = call_notifier([FakeResponse(200, {"ok": False})])
    assert result is False, "ok:false при 200 не успех"


def test_401_description_without_token():
    result, seen, err = call_notifier([http_error(401, {"ok": False, "description": "Unauthorized"})])
    assert result is False
    assert "Unauthorized" in err, err
    assert TOKEN not in err, "токен в stderr"
    assert len(seen) == 1, "на 401 повтора быть не должно"


def test_429_retries_once_and_succeeds():
    slept = []
    result, seen, _ = call_notifier(
        [http_error(429, {"ok": False, "parameters": {"retry_after": 0}}),
         FakeResponse(200, {"ok": True})],
        sleep=slept.append)
    assert result is True
    assert len(seen) == 2, "ждали ровно два вызова urlopen"
    assert slept == [0], slept


def test_429_twice_gives_up():
    slept = []
    result, seen, _ = call_notifier(
        [http_error(429, {"parameters": {"retry_after": 0}}),
         http_error(429, {"parameters": {"retry_after": 0}})],
        sleep=slept.append)
    assert result is False
    assert len(seen) == 2, "третьей попытки быть не должно"
    assert len(slept) == 1


def test_429_retry_after_is_capped_at_60():
    slept = []
    result, _, _ = call_notifier(
        [http_error(429, {"parameters": {"retry_after": 3600}}),
         FakeResponse(200, {"ok": True})],
        sleep=slept.append)
    assert result is True and slept == [60], slept


def test_token_with_crlf_gives_clean_url():
    seen = []

    def fake_urlopen(request, timeout=None):
        seen.append(request.full_url)
        return FakeResponse(200, {"ok": True})

    original = notifier.urllib.request.urlopen
    notifier.urllib.request.urlopen = fake_urlopen
    try:
        assert notifier.send_to_telegram("123:abc\r\n", "42", "x") is True
    finally:
        notifier.urllib.request.urlopen = original
    assert len(seen) == 1 and "\r" not in seen[0] and "\n" not in seen[0], seen
    assert "/bot" + "123:abc/sendMessage" in seen[0], seen


def test_probe_session_ignores_env_proxy():
    with probe._new_session() as session:
        assert session.trust_env is False


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


def test_url_error_hides_token():
    exc = urllib.error.URLError("не достучаться до /bot{}/sendMessage".format(TOKEN))
    result, _, err = call_notifier([exc])
    assert result is False
    assert err.strip(), "причина должна быть в stderr"
    assert TOKEN not in err, "токен в stderr"
    assert "***" in err


def test_invalid_json():
    result, _, _ = call_notifier([FakeResponse(200, b"<html>not json")])
    assert result is False


def test_request_shape():
    _, seen, _ = call_notifier([FakeResponse(200, {"ok": True})])
    request = seen[0]
    assert request.full_url.endswith("/bot{}/sendMessage".format(TOKEN)), request.full_url
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    assert body["chat_id"] == "42" and body["text"] == "привет", body
    assert body["disable_web_page_preview"] is True


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
