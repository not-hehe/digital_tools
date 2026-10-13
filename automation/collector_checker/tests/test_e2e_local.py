#!/usr/bin/env python3
"""Прогоны main.run на живом сокете, интернет не нужен: всё на 127.0.0.1.

    venv/bin/python -B -W ignore tests/test_e2e_local.py

test_logic.py проверяет решения на подставных данных, здесь проверяется,
что чекер правильно читает то, что реально приходит по сети, и что
сообщения, state.txt и runs.log сходятся с критериями AC-1..AC-8.
"""

import contextlib
import io
import logging
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import main  # noqa: E402
from modules.probe import checker as probe  # noqa: E402

TIMEOUT = 2
HANG_SECONDS = 3  # дольше TIMEOUT, но не настолько, чтобы тест долго ждал

GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"

NAMES = ["v{}".format(i) for i in range(1, 16)]


class Handler(BaseHTTPRequestHandler):
    """Поведение по пути /vN берётся из server.behavior: 200, 503, "hang"
    или "flaky" (первый запрос 503, дальше 200)."""

    def do_GET(self):
        name = self.path.strip("/")
        server = self.server
        with server.lock:
            server.hits[name] = server.hits.get(name, 0) + 1
            hit = server.hits[name]
        mode = server.behavior.get(name, 200)
        if mode == "hang":
            time.sleep(HANG_SECONDS)
            return
        if mode == "flaky":
            mode = 503 if hit == 1 else 200
        if mode == 200:
            self.send_response(200)
            self.send_header("Content-Type", "image/gif")
            self.send_header("Content-Length", str(len(GIF)))
            self.end_headers()
            self.wfile.write(GIF)
        else:
            self.send_response(mode)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args):
        pass


class Server(ThreadingHTTPServer):
    # 15 проверок стартуют разом; с очередью по умолчанию (5) macOS отказывает
    # в соединении, и тест ловил бы ложные connection.
    request_queue_size = 64
    daemon_threads = True


@contextlib.contextmanager
def collector_server(behavior):
    server = Server(("127.0.0.1", 0), Handler)
    server.behavior = behavior
    server.hits = {}
    server.lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def closed_port():
    """Порт, который только что был свободен и никем не слушается."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_state(path):
    result = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, status, since = line.split()
        result[name] = (status, datetime.fromisoformat(since))
    return result


def sent(out):
    """Сообщения среди stdout: построчные отчёты о проверках начинаются с имени."""
    prefix = "[сообщение] "
    return [l[len(prefix):] for l in out.splitlines() if l.startswith(prefix)]


def run_case(behavior, *, state_text=None, overrides=None, collectors_text=None,
             state_path=None):
    """Один прогон main.run во временной папке. overrides: {имя: полный url}."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        runs_log = tmp / "runs.log"
        state = tmp / "state.txt" if state_path is None else state_path(tmp)
        if state_text is not None:
            state.write_text(state_text, encoding="utf-8")
        with collector_server(behavior) as server:
            base = "http://127.0.0.1:{}".format(server.server_address[1])
            if collectors_text is None:
                lines = ["{} {}".format(n, (overrides or {}).get(n, "{}/{}".format(base, n)))
                         for n in NAMES]
                collectors_text = "\n".join(lines) + "\n"
            collectors = tmp / "collectors.txt"
            collectors.write_text(collectors_text, encoding="utf-8")

            # Дефолты читаются из глобалов probe при вызове, подмена безопасна.
            saved = (probe.RETRY_PAUSE, probe.REQUEST_TIMEOUT)
            probe.RETRY_PAUSE, probe.REQUEST_TIMEOUT = 0, TIMEOUT
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main.run(main.parse_args(
                        ["--no-notify", "--collectors", str(collectors), "--state", str(state),
                         "--runlog", str(runs_log)]))
            finally:
                probe.RETRY_PAUSE, probe.REQUEST_TIMEOUT = saved
            return {
                "code": code, "out": out.getvalue(), "err": err.getvalue(),
                "state": read_state(state) if state.exists() else None,
                "state_exists": state.exists(),
                "runlog": runs_log.read_text(encoding="utf-8") if runs_log.exists() else "",
                "hits": dict(server.hits),
            }


def state_text_all_up_except(down_name, since):
    lines = ["# collector status since(MSK)"]
    for n in NAMES:
        status = "down" if n == down_name else "up"
        lines.append("{} {} {}".format(n, status, since.isoformat(timespec="seconds")))
    return "\n".join(lines) + "\n"


def an_hour_ago():
    return (datetime.now() - timedelta(hours=1)).replace(microsecond=0)


# ---------------------------------------------------------------------------
# AC-1..AC-8
# ---------------------------------------------------------------------------

def test_ac1_all_ok_first_run():
    r = run_case({})
    assert r["code"] == 0, r["err"]
    assert "Сообщать не о чем." in r["out"], r["out"]
    assert sent(r["out"]) == [], sent(r["out"])
    assert len(r["state"]) == 15 and all(s == "up" for s, _ in r["state"].values()), r["state"]
    assert list(r["state"]) == NAMES
    assert "down=0" in r["runlog"], r["runlog"]


def test_ac2_one_down():
    r = run_case({"v7": 503}, state_text=state_text_all_up_except(None, an_hour_ago()))
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == ["🔴 Обнаружено падение, v7"], sent(r["out"])
    assert r["state"]["v7"][0] == "down"
    assert all(r["state"][n][0] == "up" for n in NAMES if n != "v7")
    assert "event down v7" in r["runlog"], r["runlog"]
    # время падения - этот прогон, а не час назад
    assert r["state"]["v7"][1] > an_hour_ago() + timedelta(minutes=30), r["state"]["v7"]


def test_ac3_still_down_is_silent():
    since = an_hour_ago()
    r = run_case({"v7": 503}, state_text=state_text_all_up_except("v7", since))
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == [], sent(r["out"])
    assert "Сообщать не о чем." in r["out"], r["out"]
    assert r["state"]["v7"] == ("down", since), r["state"]["v7"]
    assert "event still_down v7" in r["runlog"], r["runlog"]
    assert "event down v7" not in r["runlog"]


def test_ac4_recovery():
    r = run_case({}, state_text=state_text_all_up_except("v7", an_hour_ago()))
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == ["🟢 Коллектор v7 снова работает"], sent(r["out"])
    assert r["state"]["v7"][0] == "up"
    assert r["state"]["v7"][1] > an_hour_ago() + timedelta(minutes=30), r["state"]["v7"]
    assert "event up v7" in r["runlog"], r["runlog"]


def test_ac5_group_message_from_three():
    r = run_case({"v1": 503, "v3": 503, "v7": 503})
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == ["🔴 Обнаружено падение:"], sent(r["out"])
    assert "- Коллектор v1\n- Коллектор v3\n- Коллектор v7" in r["out"], r["out"]
    assert [n for n, (s, _) in r["state"].items() if s == "down"] == ["v1", "v3", "v7"]

    r = run_case({"v1": 503, "v7": 503})
    assert sent(r["out"]) == ["🔴 Обнаружено падение, v1", "🔴 Обнаружено падение, v7"], sent(r["out"])
    assert not any("падение:" in m for m in sent(r["out"]))


def test_ac6_retry_hides_blink():
    r = run_case({"v2": "flaky"})
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == [], sent(r["out"])
    assert r["hits"]["v2"] == 2, r["hits"]
    assert any(l.startswith("v2 ") and "2 попытки" in l and " ok " in l
               for l in r["out"].splitlines()), r["out"]
    assert "v2 ok (2 попытки)" in r["runlog"], r["runlog"]
    assert r["state"]["v2"][0] == "up"


def test_ac7_connection_refused_and_timeout():
    dead = "http://127.0.0.1:{}/v4".format(closed_port())
    started = time.monotonic()
    r = run_case({"v5": "hang"}, overrides={"v4": dead})
    assert time.monotonic() - started < 15
    assert r["code"] == 0, r["err"]
    assert sent(r["out"]) == ["🔴 Обнаружено падение, v4", "🔴 Обнаружено падение, v5"], sent(r["out"])
    assert r["state"]["v4"][0] == "down" and r["state"]["v5"][0] == "down"
    run_line = [l for l in r["runlog"].splitlines() if " run " in l][0]
    assert "v4 connection (3 попытки)" in run_line, run_line
    assert "v5 timeout (3 попытки)" in run_line, run_line
    for text in sent(r["out"]):
        assert "connection" not in text and "timeout" not in text


def test_ac8_empty_list():
    r = run_case({}, collectors_text="# только комментарий\n\n")
    assert r["code"] == 1
    assert not r["state_exists"]
    assert sent(r["out"]) == [] and "Сообщать не о чем" not in r["out"], r["out"]
    assert r["err"].strip(), "причина должна быть в stderr"
    assert r["hits"] == {}


def test_unsaveable_state_still_sends_message():
    def inside_a_file(tmp):
        blocker = tmp / "blocker"
        blocker.write_text("не каталог", encoding="utf-8")
        return blocker / "state.txt"

    r = run_case({"v7": 503}, state_path=inside_a_file)
    assert r["code"] == 1
    assert sent(r["out"]) == ["🔴 Обнаружено падение, v7"], sent(r["out"])
    assert "состояние не сохранено" in r["err"], r["err"]
    assert not r["state_exists"]


# ---------------------------------------------------------------------------
# Раннер
# ---------------------------------------------------------------------------

def run_all() -> int:
    logging.disable(logging.CRITICAL)
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    began = time.monotonic()
    for name, fn in tests:
        started = time.monotonic()
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("FAIL {}: {}".format(name, e))
        except Exception as e:
            failed += 1
            print("ERROR {}: {}: {}".format(name, type(e).__name__, e))
        else:
            print("OK   {:<44} {:.2f} с".format(name, time.monotonic() - started))
    print()
    if failed:
        print("Провалено {} из {}.".format(failed, len(tests)))
    else:
        print("Все {} сценариев пройдены за {:.1f} с.".format(len(tests), time.monotonic() - began))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
