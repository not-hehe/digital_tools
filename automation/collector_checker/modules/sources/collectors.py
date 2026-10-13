"""Список коллекторов из текстового файла: по строке `<имя> <адрес>`."""

from pathlib import Path
from typing import List, Optional

from ..config import COLLECTORS_FILE


class EmptyCollectorList(Exception):
    pass


class BadCollectorLine(Exception):
    pass


def parse_collectors(text: str) -> List[dict]:
    collectors = []
    seen = set()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) not in (2, 3):
            raise BadCollectorLine(
                "строка {}: ожидается '<имя> <адрес> [коды]', получено {!r}".format(number, raw))
        name, url = parts[0], parts[1]
        # http:// только для тестов на локальном сервере.
        if not url.startswith(("https://", "http://")):
            raise BadCollectorLine(
                "строка {}: адрес должен начинаться с http:// или https://, получено {!r}".format(number, url))
        if name in seen:
            raise BadCollectorLine("строка {}: имя {} уже встречалось".format(number, name))
        seen.add(name)
        collector = {"name": name, "url": url}
        if len(parts) == 3:
            # Допустимые коды ответа для цели, например 200,404 у пикселя без параметров.
            collector["healthy"] = _parse_codes(parts[2], number, raw)
        collectors.append(collector)
    if not collectors:
        raise EmptyCollectorList("в списке нет ни одного коллектора")
    return collectors


def _parse_codes(text: str, number: int, raw: str) -> tuple:
    try:
        codes = tuple(sorted({int(c) for c in text.split(",") if c}))
    except ValueError:
        codes = ()
    if not codes or any(c < 100 or c > 599 for c in codes):
        raise BadCollectorLine(
            "строка {}: коды должны быть числами через запятую, например 200,404; получено {!r}".format(number, raw))
    return codes


def load_collectors(path: Optional[Path] = None) -> List[dict]:
    path = COLLECTORS_FILE if path is None else Path(path)
    if not path.exists():
        raise FileNotFoundError(
            "список коллекторов не найден: {}. Образец рядом: collectors.example.txt".format(path))
    # utf-8-sig: файл может начинаться с BOM.
    return parse_collectors(path.read_text(encoding="utf-8-sig"))
