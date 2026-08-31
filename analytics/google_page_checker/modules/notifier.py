import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)



def send_to_mattermost(webhook_url: str, text: str, timeout: int = 10) -> bool:
    """
    Отправляет сообщение в Mattermost.
    """
    if not webhook_url:
        return False

    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 200:
                logger.info("Краткий отчёт отправлен в Mattermost")
                return True
            logger.warning("Mattermost ответил статусом %s", response.status)
    except urllib.error.URLError as e:
        logger.warning("Не удалось отправить отчёт в Mattermost: %s", e)
    except Exception as e:
        logger.warning("Ошибка при отправке в Mattermost: %s", e)
    return False
