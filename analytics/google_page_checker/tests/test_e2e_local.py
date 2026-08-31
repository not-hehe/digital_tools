#!/usr/bin/env python3
"""
Интеграционная проверка на Chromium, без выхода в интернет.

test_logic.py проверяет логику на заглушках — быстро, но мимо реального
браузера. Здесь наоборот: поднимаются локальные HTTP- и HTTPS-серверы,
а Chromium получает --host-resolver-rules, который разводит выдуманные
домены по этим серверам и позволяет смоделировать ровно те отказы,
которые лежат в боевом логе:

    ok.test        -> 200 с контейнером GTM в вёрстке
    err404.test    -> 404 с тем же контейнером (страница есть — разбираем)
    flaky.test     -> 503 на первых попытках, затем 200
    httponly.test  -> по 443 TLS не поднимается, по 80 отвечает
    shop.test      -> нет записи DNS, но есть у www.shop.test
    dead.test      -> нет DNS ни в одном варианте

Тот же механизм (--host-resolver-rules) — заготовка под разделение
чекера на внешний и внутренний контур.

Запуск: python3 tests/test_e2e_local.py
Требуется установленный Chromium: playwright install chromium
"""

import asyncio
import http.server
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

# Playwright подхватывает прокси из окружения и заворачивает в него HTTPS,
# из-за чего --host-resolver-rules перестаёт работать для https-адресов.
# Тесту нужны прямые соединения с локальными серверами.
for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_var, None)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING)

from modules import config as cfg  # noqa: E402
import modules.page_worker as page_worker_module  # noqa: E402
page_worker_module.MIN_WAIT_AFTER_LOAD = 0.2
page_worker_module.NETWORKIDLE_TIMEOUT = 2000
page_worker_module.LOAD_STATE_TIMEOUT = 2000
page_worker_module.TARGETED_WAIT_TIMEOUT = 1000
page_worker_module.PROBE_TIMEOUT = 2000
page_worker_module.PAGE_TIMEOUT = 10000
page_worker_module.RETRY_PAUSE = 0.1
from modules.browser_manager import BrowserManager  # noqa: E402
from modules.page_worker import PageWorker  # noqa: E402

GTM_PAGE = b"""<!doctype html><html><head>
<script>(function(w,d,s,l,i){})(window,document,'script','dataLayer','GTM-LOCAL01');</script>
</head><body><h1>page</h1></body></html>"""

CLEAN_PAGE = b"<!doctype html><html><body><h1>clean</h1></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    """Отдаёт разный код ответа в зависимости от запрошенного хоста."""

    protocol_version = "HTTP/1.1"
    flaky_hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host == "err404.test":
            status, body = 404, GTM_PAGE
        elif host == "flaky.test":
            Handler.flaky_hits += 1
            if Handler.flaky_hits < cfg.MAX_RETRIES:
                status, body = 503, b"try later"
            else:
                status, body = 200, GTM_PAGE
        elif host in ("httponly.test", "www.shop.test"):
            status, body = 200, CLEAN_PAGE
        else:
            status, body = 200, GTM_PAGE
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _self_signed(tmp: Path):
    key, crt = tmp / "k.pem", tmp / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(crt), "-days", "2",
         "-subj", "/CN=local.test"],
        check=True, capture_output=True)
    return key, crt


def _serve(port: int, ssl_ctx=None):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if ssl_ctx:
        srv.socket = ssl_ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def run_checks(http_port: int, https_port: int):
    rules = ",".join([
        f"MAP ok.test 127.0.0.1:{https_port}",
        f"MAP err404.test 127.0.0.1:{https_port}",
        f"MAP flaky.test 127.0.0.1:{https_port}",
        # по 443 здесь обычный HTTP-сервер: TLS не поднимется
        f"MAP httponly.test 127.0.0.1:{http_port}",
        "MAP shop.test ~NOTFOUND",
        f"MAP www.shop.test 127.0.0.1:{https_port}",
        "MAP dead.test ~NOTFOUND",
        "MAP www.dead.test ~NOTFOUND",
    ])
    bm = BrowserManager(
        headless=True, slow_mo=0, block_resources=False,
        max_concurrent_pages=4, allow_legacy_tls=True,
        extra_launch_args=[f"--host-resolver-rules={rules}",
                           "--no-proxy-server"])
    results = {}
    async with bm:
        worker = PageWorker(bm)
        targets = ["ok.test", "err404.test", "flaky.test", "httponly.test",
                   "shop.test", "dead.test", "нет"]
        for target in targets:
            results[target] = await worker.check_pageview_only(target)
    return results


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        key, crt = _self_signed(tmp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        http_port, https_port = _free_port(), _free_port()
        http_srv = _serve(http_port)
        https_srv = _serve(https_port, ctx)
        try:
            results = asyncio.run(run_checks(http_port, https_port))
        finally:
            http_srv.shutdown()
            https_srv.shutdown()

    checks = []

    def check(name, condition, detail=""):
        checks.append((name, condition, detail))

    r = results["ok.test"]
    check("ok.test открылся и тег найден",
          r["status"] == cfg.STATUS_BROKEN and r["container_id"] == "GTM-LOCAL01",
          f"{r['status']} / {r['container_id']}")

    r = results["err404.test"]
    check("404 разобран, а не выброшен в ERROR",
          r["status"] == cfg.STATUS_BROKEN and r["http_status"] == 404
          and r["http_error"],
          f"{r['status']} / HTTP {r['http_status']}")

    r = results["flaky.test"]
    check("503 добит повторами до 200",
          r["status"] == cfg.STATUS_BROKEN and r["http_status"] == 200,
          f"{r['status']} / HTTP {r['http_status']} / попыток {r['attempts']}")

    r = results["httponly.test"]
    check("сайт без рабочего TLS открыт по http",
          r["status"] != cfg.STATUS_ERROR
          and (r["opened_url"] or "").startswith("http://"),
          f"{r['status']} / {r['opened_url']}")

    r = results["shop.test"]
    check("нет DNS у голого домена — открыт www-вариант",
          r["status"] != cfg.STATUS_ERROR
          and r["opened_url"] == "https://www.shop.test",
          f"{r['status']} / {r['opened_url']}")

    r = results["dead.test"]
    check("мёртвый домен помечен как dns, а не «прочее»",
          r["status"] == cfg.STATUS_ERROR and r["failure_kind"] == "dns"
          and len(r["tried_urls"]) == 2,
          f"{r['failure_kind']} / {r['tried_urls']}")

    r = results["нет"]
    check("мусорная строка не доходит до браузера",
          r["status"] == cfg.STATUS_ERROR and r["failure_kind"] == "invalid_url",
          str(r["failure_kind"]))

    failed = 0
    for name, ok, detail in checks:
        if ok:
            print(f"OK  {name}")
        else:
            failed += 1
            print(f"FAIL {name} — {detail}")
    if failed:
        print(f"\nПровалено проверок: {failed} из {len(checks)}.")
        sys.exit(1)
    print(f"\nВсе {len(checks)} проверок на живом Chromium пройдены.")


if __name__ == "__main__":
    main()
