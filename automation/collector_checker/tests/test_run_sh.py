#!/usr/bin/env python3
"""Обёртка run.sh на копии проекта во временной папке: настоящие data/ и logs/ не трогаются.
tools/notify.py в копии подменён заглушкой, сеть не нужна.

    venv/bin/python -B -W ignore tests/test_run_sh.py
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Заглушка печатает метку; задержка и код возврата задаются окружением.
STUB = ("import os, sys, time\n"
        "time.sleep(float(os.environ.get('STUB_NOTIFY_SLEEP', '0')))\n"
        "print('STUB notify')\n"
        "sys.exit(int(os.environ.get('STUB_NOTIFY_EXIT', '0')))\n")


def make_copy(tmp: Path) -> Path:
    app = tmp / "app"
    app.mkdir()
    for name in ("run.sh", "main.py"):
        shutil.copy2(str(ROOT / name), str(app / name))
    shutil.copytree(str(ROOT / "modules"), str(app / "modules"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    (app / "tools").mkdir()
    (app / "tools" / "notify.py").write_text(STUB, encoding="utf-8")
    # venv того интерпретатора, которым запущен тест: в публичной копии своего venv нет.
    os.symlink(sys.prefix, str(app / "venv"))
    return app


def start_run_sh(app: Path, *args, collectors="/nonexistent", notify_exit=0,
                 notify_sleep=0) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TELEGRAM_", "CC_", "PYTHON"))}
    env.update({"CC_COLLECTORS_FILE": collectors, "STUB_NOTIFY_EXIT": str(notify_exit),
                "STUB_NOTIFY_SLEEP": str(notify_sleep)})
    # Как cron: через /bin/sh -c, чтобы строка #! действительно решала, кто исполняет файл.
    return subprocess.Popen(["/bin/sh", "-c", "./run.sh " + " ".join(args)], cwd=str(app),
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_sh(app: Path, *args, **kwargs) -> int:
    return start_run_sh(app, *args, **kwargs).wait(timeout=60)


def away_from_minute_edge():
    # run.sh считает целые минуты по своим часам; отметка, поставленная тестом за секунду
    # до смены минуты, превратила бы 4 минуты в 5.
    if time.time() % 60 > 55:
        time.sleep(61 - time.time() % 60)


def log_text(app: Path) -> str:
    path = app / "logs" / "collector_checker_{}.log".format(date.today().isoformat())
    return path.read_text(encoding="utf-8") if path.exists() else ""


def stamp(app: Path) -> Path:
    return app / "data" / ".fail_alert"


def dead_target_list(tmp: Path) -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    path = tmp / "c.txt"
    path.write_text("v1 http://127.0.0.1:{}/v1\n".format(port), encoding="utf-8")
    return str(path)


def test_shebang_is_first_bytes():
    # Пробел перед #! отдавал файл dash-у: код 2 на каждом прогоне, сбой не оповещался (T-11).
    assert (ROOT / "run.sh").read_bytes()[:11] == b"#!/bin/bash"


def test_gitignore_closes_new_files():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for name in ("data/state_down.txt", "data/state_down.txt.tmp", "data/.fail_alert"):
        assert name in lines, name


def test_ac22_failure_alert_once_per_five_minutes():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        codes = [run_sh(app) for _ in range(5)]
        log = log_text(app)
        assert codes == [1] * 5, codes
        assert log.count("STUB notify") == 1, log
        assert log.count("сообщение о сбое отложено, ещё") == 4, log
        assert stamp(app).read_text().strip().isdigit()


def test_ac22_stamp_five_minutes_old_sends_four_minutes_old_waits():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        (app / "data").mkdir()
        away_from_minute_edge()
        stamp(app).write_text(str(int(time.time()) - 240))
        run_sh(app)
        assert log_text(app).count("STUB notify") == 0, log_text(app)
        assert "ещё 1 мин" in log_text(app), log_text(app)
        stamp(app).write_text(str(int(time.time()) - 300))
        run_sh(app)
        assert log_text(app).count("STUB notify") == 1, log_text(app)


def test_ac22_failed_send_sets_no_stamp_and_retries():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        run_sh(app, notify_exit=1)
        assert not stamp(app).exists()
        run_sh(app, notify_exit=1)
        assert log_text(app).count("STUB notify") == 2, log_text(app)


def test_ac22_successful_run_clears_stamp():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        run_sh(app)
        assert stamp(app).exists()
        assert run_sh(app, "--no-notify", collectors=dead_target_list(Path(tmp))) == 0
        assert not stamp(app).exists()
        run_sh(app)
        assert log_text(app).count("STUB notify") == 2, "сбой после починки - сразу"


def wait_for(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "не дождались"
        time.sleep(0.05)


def test_lock_held_while_failure_alert_is_sent():
    # Аудит 2026-10-14: лок с pid подоболочки протухал, пока уходило сообщение о сбое,
    # и следующий запуск шёл параллельно.
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        first = start_run_sh(app, notify_sleep=3)
        wait_for(lambda: "сообщение о сбое" in log_text(app)
                 or (app / "data" / ".run.lock" / "pid").exists())
        time.sleep(1.0)
        assert first.poll() is None, "первый прогон ещё должен отправлять сообщение"
        assert run_sh(app) == 0
        assert "прогон уже идёт" in log_text(app), log_text(app)
        assert first.wait(timeout=30) == 1
        assert not (app / "data" / ".run.lock").exists(), "свой лок снимается на выходе"
        assert log_text(app).count("STUB notify") == 1, log_text(app)


def test_exit_keeps_foreign_lock():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_copy(Path(tmp))
        first = start_run_sh(app, notify_sleep=2)
        pid_file = app / "data" / ".run.lock" / "pid"
        wait_for(pid_file.exists)
        time.sleep(0.5)
        pid_file.write_text(str(os.getpid()))
        first.wait(timeout=30)
        assert pid_file.read_text() == str(os.getpid()), "чужой лок снят на выходе"


def run_all() -> int:
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
            print("OK   {:<56} {:.2f} с".format(name, time.monotonic() - started))
    print()
    print("Провалено {} из {}.".format(failed, len(tests)) if failed
          else "Все {} тестов run.sh пройдены.".format(len(tests)))
    return failed


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
