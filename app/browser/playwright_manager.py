import sys
import os
import re
import json
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from playwright.async_api import async_playwright, Playwright, BrowserContext, Page
from app.config import settings

logger = logging.getLogger(__name__)

class PlaywrightManager:
    """
    Production-grade reusable browser infrastructure for Playwright.
    
    Supports:
    - Server/Headless mode & Local/Headed mode via BROWSER_MODE and HEADLESS env vars
    - Persistent browser contexts
    - Isolated profiles per user/session under BROWSER_DATA_DIR / {profile_id}
    - Safe cookie injection with per-cookie fallback
    - LocalStorage injection via context init_scripts
    - Browser reuse with graceful recovery on disconnects/crashes
    - Cross-platform lockfile cleanup without OS-specific assumptions
    """

    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self._contexts: Dict[str, BrowserContext] = {}
        self._context_headless: Dict[str, bool] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[asyncio.Lock] = None
        self._install_lock: Optional[asyncio.Lock] = None
        self._profile_locks: Dict[str, asyncio.Lock] = {}

    def _ensure_loop_and_locks(self):
        """Ensures that asyncio locks and driver references are bound to the currently running event loop."""
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._loop != current_loop or self._lock is None:
            self._loop = current_loop
            self._lock = asyncio.Lock()
            self._install_lock = asyncio.Lock()
            self._profile_locks.clear()
            self._contexts.clear()
            self._context_headless.clear()
            self.playwright = None

    @property
    def browser_mode(self) -> str:
        """Returns 'local' or 'server'."""
        mode = os.environ.get("BROWSER_MODE", "").lower().strip()
        if mode in ("local", "server"):
            return mode
        if os.environ.get("RENDER"):
            return "server"
        return getattr(settings, "BROWSER_MODE", "local")

    @property
    def headless(self) -> bool:
        """
        Determines default headless state based on env vars and browser mode:
        1. Explicit HEADLESS env var ('true'/'false')
        2. Explicit HEADLESS_BROWSER env var (legacy alias)
        3. Explicit settings.HEADLESS / settings.HEADLESS_BROWSER
        4. If BROWSER_MODE == 'server' -> True
        5. Default for 'local' -> False
        """
        env_headless = os.environ.get("HEADLESS")
        if env_headless is not None:
            return env_headless.lower() in ("true", "1", "yes")

        legacy_env = os.environ.get("HEADLESS_BROWSER")
        if legacy_env is not None:
            return legacy_env.lower() in ("true", "1", "yes")

        if settings.HEADLESS is not None:
            return bool(settings.HEADLESS)

        if settings.HEADLESS_BROWSER is not None:
            return bool(settings.HEADLESS_BROWSER)

        if self.browser_mode == "server":
            return True

        return False

    @property
    def is_headless(self) -> bool:
        return self.headless

    @property
    def is_cloud_mode(self) -> bool:
        """True if running in server mode or headless mode."""
        return self.browser_mode == "server" or self.headless

    @property
    def base_data_dir(self) -> Path:
        """Base directory storing isolated browser profiles."""
        env_dir = os.environ.get("BROWSER_DATA_DIR")
        if env_dir:
            p = Path(env_dir)
        elif settings.BROWSER_DATA_DIR:
            p = Path(settings.BROWSER_DATA_DIR)
        else:
            p = settings.DATA_DIR / "browser_profiles"
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Backward-compatibility alias for single profile path
    @property
    def profile_dir(self) -> Path:
        return self.get_profile_dir("default")

    @property
    def context(self) -> Optional[BrowserContext]:
        """Provides backward-compatible access to the active default BrowserContext."""
        ctx = self._contexts.get("default")
        if ctx and not (hasattr(ctx, "is_closed") and ctx.is_closed()):
            return ctx
        for c in self._contexts.values():
            if c and not (hasattr(c, "is_closed") and c.is_closed()):
                return c
        return None

    @context.setter
    def context(self, value: Optional[BrowserContext]):
        if value is None:
            self._contexts.pop("default", None)
            self._context_headless.pop("default", None)
        else:
            self._contexts["default"] = value

    def _get_profile_lock(self, profile_id: str) -> asyncio.Lock:
        self._ensure_loop_and_locks()
        if profile_id not in self._profile_locks:
            self._profile_locks[profile_id] = asyncio.Lock()
        return self._profile_locks[profile_id]

    def get_profile_dir(self, profile_id: str = "default") -> Path:
        """Returns isolated filesystem directory for the given profile ID."""
        clean_id = re.sub(r'[^a-zA-Z0-9_\-\.]', '_', str(profile_id or "default").strip()) or "default"
        # Support legacy folder structure if default exists at data/browser_profile
        if clean_id == "default":
            legacy_dir = settings.DATA_DIR / "browser_profile"
            if legacy_dir.exists() and not (self.base_data_dir / "default").exists():
                return legacy_dir

        p = self.base_data_dir / clean_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def clean_stale_locks(self, profile_dir: Path):
        """Cross-platform removal of stale Chromium lockfiles within a profile directory."""
        lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")
        for name in lock_names:
            lock_path = profile_dir / name
            try:
                if lock_path.exists() or lock_path.is_symlink():
                    lock_path.unlink()
            except Exception as e:
                logger.debug(f"Could not unlink stale lockfile '{lock_path}': {e}")

    async def start(self) -> Playwright:
        """Starts the shared Playwright driver if not running."""
        self._ensure_loop_and_locks()
        async with self._lock:
            if not self.playwright:
                self.playwright = await async_playwright().start()
            return self.playwright

    async def ensure_browser_installed(self) -> bool:
        """
        Verifies that the required Playwright browser binary (Chromium) is installed.
        Does NOT download or install at runtime; browsers must be installed during the build phase.
        """
        self._ensure_loop_and_locks()
        async with self._install_lock:
            if not self.playwright:
                try:
                    await self.start()
                except Exception as e:
                    logger.error(f"Failed to start async_playwright in ensure_browser_installed: {e}")
                    return False

            try:
                exec_path = self.playwright.chromium.executable_path
                if exec_path and Path(exec_path).exists():
                    logger.info(f"Playwright Chromium binary verified at: {exec_path}")
                    return True
                logger.error(
                    f"Playwright Chromium binary not found at '{exec_path}'. "
                    "Chromium must be installed during the build phase using 'python -m playwright install --with-deps chromium'."
                )
            except Exception as e:
                logger.error(
                    f"Could not verify Chromium executable_path ({e}). "
                    "Ensure Chromium was installed during the build phase."
                )

            return False

    async def get_browser_status(self) -> dict:
        """Returns diagnostic info about browser binary presence and execution mode."""
        self._ensure_loop_and_locks()
        if not self.playwright:
            try:
                await self.start()
            except Exception as e:
                return {
                    "installed": False,
                    "error": str(e),
                    "browser_mode": self.browser_mode,
                    "headless": self.headless,
                    "is_cloud_mode": self.is_cloud_mode,
                    "browser_data_dir": str(self.base_data_dir),
                    "active_profiles": list(self._contexts.keys())
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
            "browser_mode": self.browser_mode,
            "headless": self.headless,
            "is_cloud_mode": self.is_cloud_mode,
            "browser_data_dir": str(self.base_data_dir),
            "active_profiles": list(self._contexts.keys())
        }

    async def add_cookies_safely(self, cookies: list, profile_id: str = "default") -> int:
        """
        Safely sanitizes and injects cookies into the browser context for the given profile.
        Uses per-cookie fallback so that a single rejected cookie cannot block others.
        """
        ctx = self._contexts.get(profile_id) or self.context
        if not ctx or not cookies:
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
            await ctx.add_cookies(sanitized)
            return len(sanitized)
        except Exception as bulk_err:
            logger.warning(f"Bulk add_cookies failed ({bulk_err}). Falling back to item-by-item injection...")

        success_count = 0
        for sc in sanitized:
            try:
                await ctx.add_cookies([sc])
                success_count += 1
            except Exception as single_err:
                logger.debug(f"Skipping problematic cookie '{sc.get('name')}': {single_err}")

        return success_count

    async def inject_local_storage(self, items: Dict[str, str], profile_id: str = "default", domain_pattern: str = "dice.com"):
        """Registers an init_script to inject localStorage items for a target domain."""
        ctx = self._contexts.get(profile_id) or self.context
        if not ctx or not items:
            return

        escaped_json = json.dumps(items)
        script = f"""
        (() => {{
            try {{
                if (window.location.hostname.includes("{domain_pattern}")) {{
                    const items = {escaped_json};
                    for (const [k, v] of Object.entries(items)) {{
                        if (!window.localStorage.getItem(k)) {{
                            window.localStorage.setItem(k, v);
                        }}
                    }}
                }}
            }} catch (e) {{}}
        }})();
        """
        await ctx.add_init_script(script)
        logger.info(f"Registered init_script for profile '{profile_id}' to inject {len(items)} localStorage tokens on {domain_pattern}")

    async def get_context(
        self,
        profile_id: str = "default",
        headless: Optional[bool] = None,
        restore_session: bool = True
    ) -> BrowserContext:
        """
        Returns or creates an isolated persistent BrowserContext for the specified profile_id.
        Profiles are fully segregated on the filesystem.
        """
        self._ensure_loop_and_locks()
        profile_lock = self._get_profile_lock(profile_id)
        async with profile_lock:
            # 1. Determine target headless mode
            if headless is not None:
                target_headless = headless
            else:
                target_headless = self.headless
                if os.environ.get("HEADLESS") is None and os.environ.get("HEADLESS_BROWSER") is None and settings.HEADLESS is None and settings.HEADLESS_BROWSER is None and self.browser_mode != "server":
                    try:
                        from app.services.settings_service import settings_service
                        app_cfg = await settings_service.get_settings()
                        if getattr(app_cfg, "headless_browser", None) is not None:
                            target_headless = bool(app_cfg.headless_browser)
                    except Exception:
                        pass

            # 2. Check if existing context can be reused
            existing_ctx = self._contexts.get(profile_id)
            if existing_ctx:
                try:
                    if hasattr(existing_ctx, "is_closed") and existing_ctx.is_closed():
                        self._contexts.pop(profile_id, None)
                    elif self._context_headless.get(profile_id) != target_headless:
                        logger.info(f"Switching profile '{profile_id}' context headless={self._context_headless.get(profile_id)} -> {target_headless}")
                        try:
                            await existing_ctx.close()
                        except Exception:
                            pass
                        self._contexts.pop(profile_id, None)
                    else:
                        _ = existing_ctx.pages
                        return existing_ctx
                except Exception:
                    self._contexts.pop(profile_id, None)

            # 3. Ensure Playwright driver is running
            if not self.playwright:
                await self.start()

            # 4. Resolve isolated profile directory & clean stale locks
            profile_dir = self.get_profile_dir(profile_id)
            self.clean_stale_locks(profile_dir)

            # 5. Build launch arguments (cross-platform, container-friendly, low-memory optimized)
            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--disable-setuid-sandbox",
                "--mute-audio",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
                "--disable-breakpad",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
                "--force-color-profile=srgb",
                "--metrics-recording-only",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=128",
            ]
            if not target_headless:
                args.append("--start-maximized")

            viewport = None if not target_headless else {"width": 1280, "height": 800}

            launch_opts = {
                "user_data_dir": str(profile_dir),
                "headless": target_headless,
                "args": args,
                "viewport": viewport,
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }

            # Optional custom channel if set in env
            channel = os.environ.get("BROWSER_CHANNEL") or None
            if channel:
                launch_opts["channel"] = channel

            # 6. Launch persistent context with crash recovery
            try:
                ctx = await self.playwright.chromium.launch_persistent_context(**launch_opts)
            except Exception as e:
                err_str = str(e)
                if "Executable doesn't exist" in err_str or "playwright install" in err_str:
                    logger.error(
                        f"Chromium executable missing during launch: {e}. "
                        "Chromium must be installed during the build phase using 'python -m playwright install --with-deps chromium'."
                    )
                    raise

                logger.warning(f"Initial launch failed for profile '{profile_id}' ({e}). Cleaning stale locks and retrying...")
                self.clean_stale_locks(profile_dir)

                # If connection was severed or loop detached, reinitialize driver
                if "has no attribute 'send'" in err_str or "Connection closed" in err_str or "Target closed" in err_str:
                    self.playwright = None
                    await self.start()

                try:
                    ctx = await self.playwright.chromium.launch_persistent_context(**launch_opts)
                except Exception as retry_err:
                    logger.error(f"Failed to launch persistent context on retry for profile '{profile_id}': {retry_err}")
                    raise

            # Attach lightweight route filter to block images, media, fonts, and trackers in headless/server mode
            if target_headless or self.browser_mode == "server":
                async def _route_filter(route):
                    try:
                        req = route.request
                        rtype = req.resource_type
                        url_lower = req.url.lower()
                        if rtype in ("image", "media", "font"):
                            await route.abort()
                        elif any(ad in url_lower for ad in (
                            "doubleclick.net", "sift.com", "google-analytics.com",
                            "googletagmanager.com", "gtm.js", "adnxs.com",
                            "hotjar.com", "amplitude.com", "segment.io"
                        )):
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:
                        pass

                try:
                    await ctx.route("**/*", _route_filter)
                except Exception as re:
                    logger.debug(f"Route handler attachment notice: {re}")

            self._contexts[profile_id] = ctx
            self._context_headless[profile_id] = target_headless

            # 7. Restore authenticated session state for profile context via SessionStore & restore_dice_session
            if restore_session:
                try:
                    from app.services.session_store import get_session_store
                    from app.services.dice_session_restorer import restore_dice_session
                    store = get_session_store()
                    sess_model = await store.get_session(profile_id)
                    if sess_model:
                        await restore_dice_session(ctx, sess_model)
                    elif profile_id == "default":
                        from app.services.settings_service import settings_service
                        local_sess = settings_service.get_local_session()
                        if local_sess:
                            await restore_dice_session(ctx, local_sess)
                except Exception as restore_err:
                    logger.debug(f"Could not restore session for profile '{profile_id}': {restore_err}")

            return ctx

    async def get_new_page(
        self,
        profile_id: str = "default",
        headless: Optional[bool] = None
    ) -> Page:
        """
        Creates a new Page in the isolated context for profile_id.
        Features automatic crash recovery and zombie page cleanup.
        """
        self._ensure_loop_and_locks()
        for attempt in range(2):
            try:
                context = await self.get_context(profile_id=profile_id, headless=headless)
                # Cleanup older zombie pages to prevent memory leaks
                open_pages = [p for p in context.pages if not p.is_closed()]
                if len(open_pages) > 3:
                    for old_p in open_pages[:-2]:
                        try:
                            await old_p.close()
                        except Exception:
                            pass
                return await context.new_page()
            except Exception as e:
                logger.warning(f"Failed to create new page for profile '{profile_id}' on attempt {attempt + 1}: {e}. Resetting context...")
                await self.close_context(profile_id)
                if attempt == 1:
                    raise

    async def close_context(self, profile_id: str):
        """Closes a specific profile context and unlinks lockfiles."""
        self._ensure_loop_and_locks()
        ctx = self._contexts.pop(profile_id, None)
        self._context_headless.pop(profile_id, None)
        if ctx:
            try:
                await ctx.close()
            except Exception as e:
                logger.debug(f"Error closing context for profile '{profile_id}': {e}")
        profile_dir = self.get_profile_dir(profile_id)
        self.clean_stale_locks(profile_dir)

    async def close(self):
        """Closes all active profile contexts and stops Playwright."""
        self._ensure_loop_and_locks()
        async with self._lock:
            for profile_id, ctx in list(self._contexts.items()):
                try:
                    await ctx.close()
                except Exception:
                    pass
                profile_dir = self.get_profile_dir(profile_id)
                self.clean_stale_locks(profile_dir)

            self._contexts.clear()
            self._context_headless.clear()

            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None

    async def close_all(self):
        """Alias for close()."""
        await self.close()

playwright_manager = PlaywrightManager()
