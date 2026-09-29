import sys
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

    @property
    def has_display(self) -> bool:
        """Determines if the current system environment has a graphical display capable of rendering windows."""
        return sys.platform == "win32" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    @property
    def headless(self) -> bool:
        """Determines whether browser instances should be launched in headless mode."""
        headless_env = os.environ.get("HEADLESS_BROWSER")
        if headless_env is not None:
            return headless_env.lower() in ("true", "1", "yes")
        if not self.has_display:
            return True
        return False

    @property
    def is_headless(self) -> bool:
        return self.headless

    @property
    def is_cloud_mode(self) -> bool:
        """Returns True if the backend is running in a headless / cloud environment without a direct user desktop."""
        return not self.has_display or os.environ.get("HEADLESS_BROWSER", "").lower() in ("true", "1", "yes")

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
            headless_env = os.environ.get("HEADLESS_BROWSER")

            if headless_env is not None:
                headless = headless_env.lower() in ("true", "1", "yes")
            elif not self.has_display:
                headless = True
            else:
                headless = app_settings.headless_browser

            if not self.playwright:
                self.playwright = await async_playwright().start()

            # Check for system Google Chrome across Windows and Linux
            channel = None
            google_chrome_paths = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path("/usr/bin/google-chrome"),
            ]
            if any(p.exists() for p in google_chrome_paths):
                channel = "chrome"

            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-breakpad",
                "--disable-component-extensions-with-background-pages",
                "--disable-ipc-flooding-protection",
                "--disable-renderer-backgrounding",
                "--mute-audio",
            ]
            if not headless:
                args.append("--start-maximized")

            viewport = None if not headless else {"width": 1280, "height": 800}

            def _clean_stale_locks():
                for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
                    lock_file = self.profile_dir / lock_name
                    try:
                        if lock_file.exists() or lock_file.is_symlink():
                            lock_file.unlink()
                    except Exception:
                        pass

            def _clean_orphaned_chrome():
                if sys.platform == "win32":
                    try:
                        import subprocess, base64
                        cmd = 'Get-CimInstance Win32_Process -Filter "Name = \'chrome.exe\'" | Where-Object { $_.CommandLine -like "*browser_profile*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }'
                        enc = base64.b64encode(cmd.encode("utf-16le")).decode("ascii")
                        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc], capture_output=True, timeout=5)
                    except Exception:
                        pass
                else:
                    try:
                        import subprocess
                        subprocess.run(["pkill", "-f", "browser_profile"], capture_output=True, timeout=3)
                    except Exception:
                        pass

            _clean_stale_locks()

            launch_opts = {
                "user_data_dir": str(self.profile_dir),
                "headless": headless,
                "args": args,
                "viewport": viewport,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }

            try:
                if channel:
                    self.context = await self.playwright.chromium.launch_persistent_context(
                        channel=channel,
                        **launch_opts
                    )
                else:
                    self.context = await self.playwright.chromium.launch_persistent_context(**launch_opts)
            except Exception as e:
                logger.warning(f"Initial browser launch failed ({e}). Retrying with default Chromium...")
                _clean_orphaned_chrome()
                _clean_stale_locks()
                try:
                    self.context = await self.playwright.chromium.launch_persistent_context(**launch_opts)
                except Exception as retry_err:
                    logger.error(f"Fallback Chromium launch failed: {retry_err}")
                    raise

            # Restore saved cookies from MongoDB if available
            try:
                from app.database import get_database
                db = get_database()
                if db is not None:
                    settings_doc = await db.app_settings.find_one({})
                    if settings_doc and settings_doc.get("saved_cookies"):
                        await self.context.add_cookies(settings_doc["saved_cookies"])
                        logger.info(f"Restored {len(settings_doc['saved_cookies'])} saved Dice cookies from database.")
            except Exception as e:
                logger.debug(f"Could not restore saved cookies on startup: {e}")

            return self.context

    async def get_new_page(self) -> Page:
        for attempt in range(2):
            try:
                context = await self.get_context()
                # Clean up any closed or zombie pages in context to prevent memory leaks in production
                open_pages = [p for p in context.pages if not p.is_closed()]
                if len(open_pages) > 3:
                    for old_p in open_pages[:-2]:
                        try:
                            await old_p.close()
                        except Exception:
                            pass
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
