import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, List, Optional

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Playwright
)

logger = logging.getLogger(__name__)


class BrowserManager:

    """
    Управляет жизненным циклом браузера Chromium с антидетект-настройками.
    Позволяет:
    - запускать браузер в headless-режиме;
    - внедрять JavaScript-код для сокрытия признаков автоматизации (stealth);
    - ограничивать число одновременно открытых страниц (семафор;
      лимит действует при работе через page_context);
    - разрешать устаревшие версии TLS, чтобы сайты на TLS 1.0/1.1 вообще
      открывались, а не отваливались с ERR_SSL_VERSION_OR_CIPHER_MISMATCH;
    - использовать контекстные менеджеры для автоматического закрытия страниц и браузера.
    """

    def __init__(self, max_concurrent_pages, allow_legacy_tls=True,
                 extra_launch_args=None):
        self.max_concurrent_pages = max_concurrent_pages
        self.allow_legacy_tls = allow_legacy_tls
        # Точка расширения: сюда попадут, например, --host-resolver-rules,
        # когда чекер будет разделён на внешний и внутренний контур.
        self.extra_launch_args: List[str] = list(extra_launch_args or [])
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._semaphore = asyncio.Semaphore(max_concurrent_pages)

    def _launch_args(self) -> List[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-software-rasterizer",
        ]
        if self.allow_legacy_tls:
            # Современный Chromium отвергает TLS 1.0/1.1 до отправки запроса,
            # и сайт получает ERR_SSL_VERSION_OR_CIPHER_MISMATCH вместо
            # страницы. Нам важно снять показания, а не защитить пользователя,
            # поэтому опускаем нижнюю границу и не спорим о сертификатах.
            args += [
                "--ssl-version-min=tls1",
                "--ignore-certificate-errors",
                "--allow-insecure-localhost",
            ]
        args += ["--headless=new", "--window-size=1920,1080",
                 "--start-maximized"]
        args += self.extra_launch_args
        return args

    async def start(self):
        self._playwright = await async_playwright().start()
        launch_options = {
            "headless": True,
            "args": self._launch_args(),
        }
        self._browser = await self._playwright.chromium.launch(**launch_options)
        context_options = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "permissions": ["geolocation"],
            "color_scheme": "light",
            "device_scale_factor": 1,
            "is_mobile": False,
            "has_touch": False,
            "java_script_enabled": True,
            "accept_downloads": False,
            "ignore_https_errors": True,
        }
        self._context = await self._browser.new_context(**context_options)
        logger.info("Браузер запущен")

    async def _apply_stealth(self, page: Page):
        try:
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
                window.chrome = {runtime: {}};
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
            """)
        except Exception as e:
            logger.warning("Stealth не применён: %s", e)

    async def new_page(self) -> Page:
        page = await self._context.new_page()
        # Раньше stealth вешался ещё и обработчиком на событие 'page'
        # контекста: скрипт добавлялся дважды, причём вторая установка
        # гонялась с началом навигации. Ставим ровно один раз и здесь.
        await self._apply_stealth(page)
        return page

    def is_alive(self) -> bool:
        """Жив ли браузер.

        Если Chromium умер посреди прогона, каждая следующая страница
        падает на new_page, а результаты уходят в ERROR. Без этой проверки
        оркестратор честно отрабатывал оставшиеся прогоны на мёртвом
        браузере и записывал в отчёт сотни выдуманных недоступных сайтов.
        """
        if self._browser is None:
            return False
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    async def close(self):
        try:
            if self._context: await self._context.close()
            if self._browser: await self._browser.close()
            if self._playwright: await self._playwright.stop()
        except Exception as e:
            logger.error("Ошибка завершения: %s", e)

    @asynccontextmanager
    async def page_context(self) -> AsyncGenerator[Page, None]:
        # Семафор удерживается ВСЁ время жизни страницы
        async with self._semaphore:
            page = await self.new_page()
            try:
                yield page
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()
