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
        self._install_lock = asyncio.Lock()
        self._current_headless: Optional[bool] = None

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

    async def ensure_browser_installed(self) -> bool:
        """
        Ensures that the required Playwright browser binary (Chromium) is installed.
        If missing (e.g. in cloud environments like Render native Python where
        'playwright install' wasn't run during build), downloads it automatically.
        """
        async with self._install_lock:
            if not self.playwright:
                try:
                    self.playwright = await async_playwright().start()
                except Exception as e:
                    logger.error(f"Failed to start async_playwright in ensure_browser_installed: {e}")
                    return False

            try:
                exec_path = self.playwright.chromium.executable_path
                if exec_path and Path(exec_path).exists():
                    logger.info(f"Playwright Chromium binary verified at: {exec_path}")
                    return True
            except Exception as e:
                logger.debug(f"Could not verify Chromium executable_path: {e}")

            logger.warning(
                "Playwright Chromium binary not found. Auto-installing Chromium now (running: python -m playwright install chromium)..."
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    logger.info("Successfully installed Playwright Chromium binary.")
                    return True
                else:
                    err_msg = stderr.decode().strip() or stdout.decode().strip()
                    logger.error(f"Failed to auto-install Playwright Chromium (code {proc.returncode}): {err_msg}")
            except Exception as install_err:
                logger.error(f"Exception during Playwright auto-install: {install_err}")

            return False

    async def get_browser_status(self) -> dict:
        """Returns diagnostic info about browser binary presence and execution mode."""
        if not self.playwright:
            try:
                self.playwright = await async_playwright().start()
            except Exception as e:
                return {
                    "installed": False,
                    "error": str(e),
                    "headless": self.headless,
                    "is_cloud_mode": self.is_cloud_mode,
                    "has_display": self.has_display
                }

        exec_path = None
        is_installed = False
        try:
            exec_path = self.playwright.chromium.executable_path
            is_installed = bool(exec_path and Path(exec_path).exists())
        except Exception as e:
            logger.debug(f"Could not check chromium executable path: {e}")

        return {
            "installed": is_installed,
            "executable_path": exec_path,
            "headless": self.headless,
            "is_cloud_mode": self.is_cloud_mode,
            "has_display": self.has_display
        }

    async def add_cookies_safely(self, cookies: list) -> int:
        """
        Safely sanitizes and injects cookies into the active browser context.
        Uses per-cookie fallback so that a single rejected cookie cannot block others.
        """
        if not self.context or not cookies:
            return 0

        from app.services.settings_service import sanitize_cookie_for_playwright

        sanitized = []
        for c in cookies:
            clean = sanitize_cookie_for_playwright(c)
            if clean:
                sanitized.append(clean)

        if not sanitized:
            return 0

        # Try bulk insert first
        try:
            await self.context.add_cookies(sanitized)
            return len(sanitized)
        except Exception as bulk_err:
            logger.warning(f"Bulk add_cookies failed ({bulk_err}). Falling back to item-by-item injection...")

        success_count = 0
        for sc in sanitized:
            try:
                await self.context.add_cookies([sc])
                success_count += 1
            except Exception as single_err:
                logger.debug(f"Skipping problematic cookie '{sc.get('name')}': {single_err}")

        return success_count

    async def get_context(self, headless: Optional[bool] = None) -> BrowserContext:
        async with self._lock:
            # Determine target headless mode
            if headless is not None:
                target_headless = headless
            else:
                app_settings = await settings_service.get_settings()
                headless_env = os.environ.get("HEADLESS_BROWSER")
                if headless_env is not None:
                    target_headless = headless_env.lower() in ("true", "1", "yes")
                elif not self.has_display:
                    target_headless = True
                else:
                    target_headless = app_settings.headless_browser

            if self.context:
                try:
                    if hasattr(self.context, "is_closed") and self.context.is_closed():
                        self.context = None
                    elif self._current_headless is not None and self._current_headless != target_headless:
                        logger.info(f"Switching browser context from headless={self._current_headless} to headless={target_headless}")
                        try:
                            await self.context.close()
                        except Exception:
                            pass
                        self.context = None
                    else:
                        _ = self.context.pages
                        return self.context
                except Exception:
                    self.context = None

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

            # If no system Chrome, proactively ensure bundled Playwright Chromium binary is installed
            if not channel:
                try:
                    exec_path = self.playwright.chromium.executable_path
                    if not exec_path or not Path(exec_path).exists():
                        await self.ensure_browser_installed()
                except Exception:
                    pass

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
            if not target_headless:
                args.append("--start-maximized")

            viewport = None if not target_headless else {"width": 1280, "height": 800}

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

            # Proactively clean stale locks and orphaned profile processes before launch
            _clean_orphaned_chrome()
            _clean_stale_locks()

            launch_opts = {
                "user_data_dir": str(self.profile_dir),
                "headless": target_headless,
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
                err_str = str(e)
                if "Executable doesn't exist" in err_str or "playwright install" in err_str:
                    logger.warning("Chromium executable missing during launch. Running auto-install...")
                    installed = await self.ensure_browser_installed()
                    if installed:
                        _clean_stale_locks()
                        self.context = await self.playwright.chromium.launch_persistent_context(**launch_opts)
                    else:
                        raise
                else:
                    logger.warning(f"Initial browser launch failed ({e}). Retrying with default Chromium...")
                    _clean_orphaned_chrome()
                    _clean_stale_locks()
                    try:
                        self.context = await self.playwright.chromium.launch_persistent_context(**launch_opts)
                    except Exception as retry_err:
                        retry_str = str(retry_err)
                        if "Executable doesn't exist" in retry_str or "playwright install" in retry_str:
                            logger.warning("Chromium executable missing on retry. Attempting auto-install...")
                            installed = await self.ensure_browser_installed()
                            if installed:
                                _clean_stale_locks()
                                self.context = await self.playwright.chromium.launch_persistent_context(**launch_opts)
                            else:
                                raise
                        else:
                            logger.error(f"Fallback Chromium launch failed: {retry_err}")
                            raise

            self._current_headless = target_headless

            # Restore saved cookies from MongoDB safely into context
            try:
                from app.database import get_database
                db = get_database()
                if db is not None:
                    settings_doc = await db.app_settings.find_one({})
                    if settings_doc and settings_doc.get("saved_cookies"):
                        added = await self.add_cookies_safely(settings_doc["saved_cookies"])
                        logger.info(f"Restored {added} saved Dice cookies into browser context.")
            except Exception as e:
                logger.debug(f"Could not restore saved cookies on startup: {e}")

            return self.context

    async def get_new_page(self, headless: Optional[bool] = None) -> Page:
        for attempt in range(2):
            try:
                context = await self.get_context(headless=headless)
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
            self._current_headless = None


playwright_manager = PlaywrightManager()
