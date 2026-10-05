#!/usr/bin/env python3
"""Проверки на живом сокете. Интернет не нужен, всё крутится на 127.0.0.1.

    venv/bin/python tests/test_e2e_local.py

Тесты логики (test_logic.py) работают на подставных данных и проверяют
решения. Здесь наоборот: данные настоящие, а проверяется, что чекер
правильно читает то, что реально приходит по сети - коды ответов, цепочки
редиректов, закрытый порт, поведение пула под нагрузкой.
"""

import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from modules.probe import failures as f
from modules.probe.checker import check_all, check_domain

SLOW_SECONDS = 1.0


class Handler(BaseHTTPRequestHandler):
    """Маршруты, каждый из которых воспроизводит один случай из жизни."""

    def do_GET(self):
        path = self.path
        if path == "/ok":
            self._send(200, b"ok")
        elif path == "/404":
            self._send(404, b"not found")
        elif path == "/503":
            self._send(503, b"unavailable")
        elif path.startswith("/hop"):
            # Цепочка: /hop1 -> /hop2 -> /hop3 -> /ok
            step = int(path[4:] or 1)
            self._redirect("/ok" if step >= 3 else "/hop{}".format(step + 1))
        elif path == "/loop":
            self._redirect("/loop")
        elif path == "/slow":
            time.sleep(SLOW_SECONDS)
            self._send(200, b"slow but alive")
        else:
            self._send(404, b"unknown route")

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        """Сервер не должен засорять вывод тестов."""


def free_port() -> int:
    """Порт, который сейчас никто не слушает."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class LocalServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path):
        return self.base + path


# ---------------------------------------------------------------------------
# Коды ответа
# ---------------------------------------------------------------------------

def test_live_status_codes():
    with LocalServer() as srv:
        alive = check_domain(srv.url("/ok"))
        assert alive["ok"] is True
        assert alive["status_code"] == 200
        assert alive["elapsed_ms"] is not None

        for path, code in (("/404", 404), ("/503", 503)):
            r = check_domain(srv.url(path))
            assert r["ok"] is False, path
            assert r["status_code"] == code, path
            # Сервер ответил, значит это не отказ соединения.
            assert r["failure_kind"] is None, path


# ---------------------------------------------------------------------------
# Редиректы
# ---------------------------------------------------------------------------

def test_redirect_chain_is_followed_and_recorded():
    with LocalServer() as srv:
        r = check_domain(srv.url("/hop1"))
        assert r["ok"] is True
        assert r["status_code"] == 200
        assert len(r["redirects"]) == 3, r["redirects"]
        assert r["final_url"].endswith("/ok")


def test_redirect_loop_is_not_a_healthy_site():
    """Бесконечная цепочка ответа не даёт, значит это отказ, а не 3xx."""
    with LocalServer() as srv:
        r = check_domain(srv.url("/loop"), max_redirects=3)
        assert r["ok"] is False
        assert r["status_code"] is None
        assert r["failure_kind"] == f.FAIL_REDIRECT_LOOP


# ---------------------------------------------------------------------------
# Недоступность
# ---------------------------------------------------------------------------

def test_closed_port_is_refused():
    r = check_domain("http://127.0.0.1:{}/".format(free_port()))
    assert r["ok"] is False
    assert r["failure_kind"] == f.FAIL_REFUSED
    assert r["status_code"] is None


# ---------------------------------------------------------------------------
# Пул потоков
# ---------------------------------------------------------------------------

def test_pool_returns_result_for_every_target():
    """Одна битая цель среди двадцати не должна менять число результатов."""
    with LocalServer() as srv:
        targets = [{"url": srv.url("/ok"), "label": "ok{}".format(i)}
                   for i in range(19)]
        targets.append({"label": "битая цель без адреса"})
        results = check_all(targets, max_workers=5)
        assert len(results) == 20
        assert sum(1 for r in results if r["ok"]) == 19


def test_pool_actually_runs_in_parallel():
    """Десять секундных запросов при пуле в пять должны уложиться в пять секунд.

    Последовательный обход занял бы десять. Без этого замера выродившийся
    в последовательный пул выглядит полностью рабочим.
    """
    with LocalServer() as srv:
        targets = [{"url": srv.url("/slow"), "label": "slow{}".format(i)}
                   for i in range(10)]
        started = time.monotonic()
        results = check_all(targets, max_workers=5, timeout=10)
        elapsed = time.monotonic() - started

    assert all(r["ok"] for r in results)
    sequential = 10 * SLOW_SECONDS
    assert elapsed < sequential / 2, (
        "обход занял {:.1f} с при последовательных {:.0f} с: "
        "похоже, пул не параллелит".format(elapsed, sequential))


# ---------------------------------------------------------------------------
# Раннер
# ---------------------------------------------------------------------------

def run_all() -> int:
    logging.disable(logging.CRITICAL)
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
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
        print("Все {} проверок пройдены.".format(len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
