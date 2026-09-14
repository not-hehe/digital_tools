#!/usr/bin/env python3
"""Мини-исследование: серия прогонов из одной точки, чтобы найти моргающие домены.

    venv/bin/python tools/survey.py                       # 12 прогонов раз в 5 минут
    venv/bin/python tools/survey.py 24 300 /tmp/s.csv     # прогонов, секунд, файл
    venv/bin/python tools/survey.py --report /tmp/s.csv   # только сводка по готовому CSV

Ничего не отправляет и состояние не трогает: это наблюдение, а не прогон.
Каждый прогон дописывает по строке на домен в CSV, так что при обрыве
(Ctrl+C, обрыв сессии) собранное не пропадает - сводку можно построить потом.

Зачем отдельно от боевого прогона: тот сообщает только о смене статуса
и скрывает перечень за предохранителем. Здесь нужна сырая картина по каждому
домену за каждый прогон, чтобы посчитать, кто меняет статус чаще всех.
"""

import csv
import logging
import sys
import time
import warnings
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Python на Mac собран с LibreSSL, и urllib3 предупреждает об этом при каждом
# импорте. На проверку это не влияет, а в выводе серии выглядит как ошибка.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from modules.config import now_msk
from modules.probe import check_all
from modules.probe.checker import reason_of
from modules.sources import load_targets

FIELDS = ["pass", "time", "label", "contour", "ok", "code", "reason"]


def observe(passes: int, interval: int, out: Path) -> None:
    targets = load_targets()["targets"]
    new_file = not out.exists()
    with out.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for n in range(1, passes + 1):
            stamp = now_msk().strftime("%Y-%m-%d %H:%M:%S")
            results = check_all(targets)
            for r in results:
                w.writerow({"pass": n, "time": stamp, "label": r["label"],
                            "contour": r["contour"], "ok": int(bool(r["ok"])),
                            "code": r["status_code"] or "", "reason": reason_of(r)})
            fh.flush()
            down = sum(1 for r in results if not r["ok"])
            print("{} прогон {}/{}: не отвечают {} из {}".format(
                stamp, n, passes, down, len(results)), flush=True)
            if n < passes:
                time.sleep(interval)


def report(path: Path) -> None:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    by_label = defaultdict(list)
    for r in rows:
        by_label[r["label"]].append(r)
    # По времени, а не по номеру прогона: в один файл могут дописаться
    # несколько серий, и номера в них повторяются.
    passes = len({r["time"] for r in rows})
    print("Прогонов: {}, доменов: {}, файл: {}".format(passes, len(by_label), path))

    flappers = []
    always_down = []
    for label, obs in by_label.items():
        oks = [o["ok"] for o in obs]
        changes = sum(1 for a, b in zip(oks, oks[1:]) if a != b)
        if changes:
            flappers.append((changes, label, oks, obs))
        elif oks[0] == "0":
            always_down.append((label, Counter(o["reason"] for o in obs).most_common(1)[0][0]))

    print("\nМеняли статус ({}): число смен, домен, картина по прогонам".format(len(flappers)))
    for changes, label, oks, obs in sorted(flappers, reverse=True):
        picture = "".join("." if ok == "1" else "x" for ok in oks)
        reasons = Counter(o["reason"] for o in obs if o["ok"] == "0").most_common(2)
        print("  {:>3}  {:<40} {}  {}".format(
            changes, label, picture, "; ".join(r for r, _ in reasons)))

    print("\nНе отвечали ни разу ({}), по причинам:".format(len(always_down)))
    for reason, count in Counter(r for _, r in always_down).most_common():
        print("  {:>4}  {}".format(count, reason))

    stable_up = len(by_label) - len(flappers) - len(always_down)
    print("\nОтвечали всегда: {}".format(stable_up))


def main(argv) -> int:
    logging.disable(logging.WARNING)
    if len(argv) > 1 and argv[1] == "--report":
        report(Path(argv[2]))
        return 0
    passes = int(argv[1]) if len(argv) > 1 else 12
    interval = int(argv[2]) if len(argv) > 2 else 300
    out = Path(argv[3]) if len(argv) > 3 else Path("/tmp/survey.csv")
    try:
        observe(passes, interval, out)
    except KeyboardInterrupt:
        print("\nПрервано, собранное сохранено.")
    report(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
