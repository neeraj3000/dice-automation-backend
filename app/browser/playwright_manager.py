import os
import asyncio
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Playwright, BrowserContext, Page
from app.config import settings
from app.services.settings_service import settings_service

import logging

logger = logging.getLogger(__name__)

class PlaywrightManager:
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.profile_dir = settings.DATA_DIR / "browser_profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    async def get_context(self) -> BrowserContext:
        async with self._lock:
            if self.context:
                try:
                    # Check if context was closed
                    if hasattr(self.context, "is_closed") and self.context.is_closed():
                        self.context = None
                    else:
                        # Verify context is responsive
                        _ = self.context.pages
                        return self.context
                except Exception:
                    self.context = None

            app_settings = await settings_service.get_settings()
            headless = app_settings.headless_browser

            if not self.playwright:
                self.playwright = await async_playwright().start()

            # Check for system Chrome
            channel = None
            chrome_paths = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
            if any(p.exists() for p in chrome_paths):
                channel = "chrome"

            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--start-maximized",
            ]

            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    channel=channel,
                    headless=headless,
                    args=args,
                    viewport=None, # uses full screen
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                )
            except Exception as e:
                # Fallback to default chromium without channel if custom channel fails
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir),
                    headless=headless,
                    args=args,
                    viewport=None,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                )
            return self.context

    async def get_new_page(self) -> Page:
        for attempt in range(2):
            try:
                context = await self.get_context()
                return await context.new_page()
            except Exception as e:
                logger.warning(f"Failed to create new page on attempt {attempt + 1}: {e}. Resetting browser context...")
                await self.close()
                if attempt == 1:
                    raise

    async def close(self):
        async with self._lock:
            if self.context:
                try:
                    await self.context.close()
                except Exception:
                    pass
                self.context = None
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None


playwright_manager = PlaywrightManager()
