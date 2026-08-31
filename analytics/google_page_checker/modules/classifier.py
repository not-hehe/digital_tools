"""
Классификация сайта по собранным признакам.
"""

import re
from typing import Optional

from .config import (
    STATUS_WORKING,
    STATUS_BROKEN,
    STATUS_SUSPICIOUS,
    STATUS_NONE,
    GA_POSITIVE_STATUSES,
)

# Идентификатор ресурса Universal Analytics. Система отключена: сбор данных
# остановлен 01.07.2023, ресурсы, интерфейс и API удалены 01.07.2024.
# Тег с таким ID физически не может ничего отправить — это мёртвый код
# в вёрстке, а не «аналитика, которую выключили конфигом».
UA_ID_RE = re.compile(r"UA-\d{4,}-\d+")


def classify_site(has_collect: bool,
                  has_strong_html_markers: bool,
                  has_error_telemetry: bool) -> str:
    if has_collect:
        return STATUS_WORKING
    if has_strong_html_markers:
        return STATUS_BROKEN
    if has_error_telemetry:
        return STATUS_SUSPICIOUS
    return STATUS_NONE


def is_ga_positive(status: str) -> bool:
    """True, если сайт с таким статусом считается имеющим Google Analytics."""
    return status in GA_POSITIVE_STATUSES


def is_universal_analytics(container_id: Optional[str]) -> bool:
    """Идентификатор относится к отключённой Universal Analytics."""
    return bool(container_id and UA_ID_RE.fullmatch(container_id))
