#!/usr/bin/env python3
"""Черновая разметка доменов по контурам.

ЗАПУСКАТЬ ТОЛЬКО с машины, которая внутренний контур НЕ видит. Смысл всей
затеи в том, что мы спрашиваем публичный DNS: если он про домен не знает,
значит наружу домен не опубликован. С виртуалки, видящей оба контура,
разрешится всё подряд, и разметка получится бессмысленной.

Признак это результат разрешения имени, а не ответ сайта:

    имя не разрешается            -> internal, публичной записи нет
    разрешается в частный адрес   -> internal, запись есть, но ведёт внутрь
    разрешается в публичный адрес -> external

Почему не по ответу сайта. Домен может резолвиться публично и при этом
не отвечать: битый сертификат, закрытый порт, лежащий сервис. К контуру
это отношения не имеет, он всё равно внешний. Первая версия этого скрипта
смотрела на HTTP и уводила такие домены в unknown, а их полторы сотни.

HTTP-запросов скрипт не делает вообще, поэтому отрабатывает за секунды.

Результат это ЧЕРНОВИК: просмотрите глазами, поправьте спорное.

    venv/bin/python tools/classify_contours.py [выходной_файл.xlsx]
"""

import concurrent.futures
import ipaddress
import socket
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent))

from openpyxl import Workbook

from modules.config import DATA_DIR, MAX_WORKERS
from modules.sources import (CONTOUR_EXTERNAL, CONTOUR_INTERNAL,
                             CONTOUR_UNKNOWN, load_targets)

DEFAULT_OUT = DATA_DIR / "input_urls.draft.xlsx"
DNS_TIMEOUT = 5


def resolve(host: str):
    """Адреса, в которые разрешается имя. Пусто, если не разрешается."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    except Exception:
        return []
    return sorted({info[4][0] for info in infos})


def classify(url: str):
    """Возвращает (контур, признак) по одному адресу."""
    host = urlsplit(url).hostname or ""
    if not host:
        return CONTOUR_UNKNOWN, "адрес без имени хоста"

    addresses = resolve(host)
    if not addresses:
        return CONTOUR_INTERNAL, "имя не разрешается"

    private = []
    public = []
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        (private if ip.is_private or ip.is_loopback or ip.is_link_local
         else public).append(addr)

    if public:
        return CONTOUR_EXTERNAL, "публичный адрес {}".format(public[0])
    if private:
        return CONTOUR_INTERNAL, "частный адрес {}".format(private[0])
    return CONTOUR_UNKNOWN, "адреса не разобраны"


def main(argv):
    socket.setdefaulttimeout(DNS_TIMEOUT)
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT

    targets = load_targets()["targets"]
    print("Целей: {}. Разрешаем имена...".format(len(targets)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        verdicts = list(ex.map(lambda t: classify(t["url"]), targets))

    counts = Counter(v[0] for v in verdicts)
    wb = Workbook()
    ws = wb.active
    ws.title = "urls"
    ws.append(["url", "contour", "признак"])
    for target, (contour, why) in zip(targets, verdicts):
        ws.append([target["url"], contour, why])
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)

    print()
    print("Черновик сохранён: {}".format(out))
    print("  external : {:>4}".format(counts[CONTOUR_EXTERNAL]))
    print("  internal : {:>4}".format(counts[CONTOUR_INTERNAL]))
    print("  unknown  : {:>4}".format(counts[CONTOUR_UNKNOWN]))
    print()
    print("Проверьте разметку, удалите колонку 'признак' и положите файл")
    print("на место рабочего списка.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
