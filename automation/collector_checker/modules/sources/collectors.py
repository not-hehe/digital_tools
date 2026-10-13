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
        if len(parts) != 2:
            raise BadCollectorLine(
                "строка {}: ожидается '<имя> <адрес>', получено {!r}".format(number, raw))
        name, url = parts
        # http:// нужен тестам на локальном сервере, боевой список целиком https.
        if not url.startswith(("https://", "http://")):
            raise BadCollectorLine(
                "строка {}: адрес должен начинаться с http:// или https://, получено {!r}".format(number, url))
        if name in seen:
            raise BadCollectorLine("строка {}: имя {} уже встречалось".format(number, name))
        seen.add(name)
        collectors.append({"name": name, "url": url})
    if not collectors:
        raise EmptyCollectorList("в списке нет ни одного коллектора")
    return collectors


def load_collectors(path: Optional[Path] = None) -> List[dict]:
    path = COLLECTORS_FILE if path is None else Path(path)
    if not path.exists():
        raise FileNotFoundError(
            "список коллекторов не найден: {}. Образец рядом: collectors.example.txt".format(path))
    # utf-8-sig: файл, сохранённый редактором с BOM, иначе ломает первую строку.
    return parse_collectors(path.read_text(encoding="utf-8-sig"))
