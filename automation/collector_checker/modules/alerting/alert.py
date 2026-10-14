"""Тексты сообщений и отправка списка. Кому и когда писать, решает reminder."""

from typing import Callable, List, Optional

from ..config import GROUP_MIN_DOWN

DOWN_MARK = "\U0001F534"   # красный кружок
UP_MARK = "\U0001F7E2"     # зелёный кружок

KIND_FALL = "fall"
KIND_REMIND = "remind"
KIND_UP = "up"


def _batches(kind: str, names: List[str], group_min: Optional[int],
             single: str, header: str) -> List[dict]:
    """От group_min имён одно сообщение списком, меньше - по одному."""
    group_min = GROUP_MIN_DOWN if group_min is None else group_min
    if not names:
        return []
    if len(names) >= group_min:
        text = "{}\n{}".format(header, "\n".join("- Коллектор {}".format(n) for n in names))
        return [{"kind": kind, "text": text, "names": list(names)}]
    return [{"kind": kind, "text": single.format(name), "names": [name]} for name in names]


def fall_batches(names: List[str], group_min: Optional[int] = None) -> List[dict]:
    return _batches(KIND_FALL, names, group_min,
                    DOWN_MARK + " Обнаружено падение, {}",
                    DOWN_MARK + " Обнаружено падение коллекторов:")


def remind_batches(names: List[str], group_min: Optional[int] = None) -> List[dict]:
    return _batches(KIND_REMIND, names, group_min,
                    DOWN_MARK + " Коллектор {} всё ещё не работает",
                    DOWN_MARK + " Коллекторы всё ещё не работают:")


def up_batches(names: List[str], group_min: Optional[int] = None) -> List[dict]:
    return _batches(KIND_UP, names, group_min,
                    UP_MARK + " Коллектор {} снова работает",
                    UP_MARK + " Коллектор снова работает:")


def delivered(batch: dict, unsent: List[str]) -> bool:
    return not set(unsent).intersection(batch["names"])


def send_batches(batches: List[dict], sender: Callable[[str], bool]) -> List[str]:
    """Имена из неотправленных сообщений. Перебор не прерывается после неудачи."""
    unsent = []
    for batch in batches:
        if not sender(batch["text"]):
            unsent.extend(batch["names"])
    return unsent
