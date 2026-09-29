import os
import sys
import asyncio
import json
import logging
from typing import Optional, Set, Dict, Any, AsyncGenerator, List
from playwright.async_api import Page

from app.browser.playwright_manager import playwright_manager
from app.services.settings_service import settings_service

logger = logging.getLogger(__name__)

class DiceSessionManager:
    """
    Production-grade session manager for Dice.com authentication.
    
    Features:
    - Event-driven login detection using native Playwright navigation and lifecycle hooks (no busy-waiting loops).
    - Single-flight concurrency: prevents duplicate browser instances and conflicting watchers.
    - Real-time Server-Sent Events (SSE) broadcasting to keep all frontend consumers in sync with zero polling.
    - Safe lifecycle supervision: automatic cancellation on window close, timeout, or application shutdown.
    """

    def __init__(self):
        self._active_page: Optional[Page] = None
        self._watcher_task: Optional[asyncio.Task] = None
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._status: str = "IDLE"  # IDLE, WAITING_FOR_LOGIN, CONNECTED, CANCELLED

    @property
    def current_status(self) -> str:
        return self._status

    async def subscribe(self) -> asyncio.Queue:
        """Subscribes an SSE client to receive real-time session status events."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subscribers.add(queue)

        # Send current status immediately upon connection
        current_dice_status = await settings_service.get_dice_status(check_live=False)
        init_event = {
            "type": "INIT_STATE",
            "manager_status": self._status,
            "dice_session": current_dice_status
        }
        await queue.put(json.dumps(init_event))
        return queue

    def unsubscribe(self, queue: asyncio.Queue):
        """Unsubscribes an SSE client."""
        self._subscribers.discard(queue)

    async def broadcast(self, event_type: str, data: Dict[str, Any]):
        """Broadcasts an event to all connected SSE clients."""
        payload = json.dumps({"type": event_type, **data})
        dead_queues = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # If subscriber client is too slow, pop oldest
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    dead_queues.append(q)
            except Exception:
                dead_queues.append(q)

        for dq in dead_queues:
            self._subscribers.discard(dq)

    async def event_generator(self) -> AsyncGenerator[str, None]:
        """Async generator streaming SSE events to HTTP clients."""
        queue = await self.subscribe()
        try:
            while True:
                try:
                    # Wait for next event or send keepalive comment every 15 seconds
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            self.unsubscribe(queue)

    DICE_AUTH_COOKIE_NAMES = (
        "identity",
        "refreshtoken",
        "cms_cookie",
        "candidate_id",
        "peopleid",
        "dli",
        "dice_member_id",
        "dice_session",
        "dice-user-id",
        "_oauth2_proxy",
        "session",
        "cognito",
    )

    def _has_authenticated_cookies(self, cookies: List[Dict[str, Any]]) -> bool:
        """Checks if the cookies list contains any known Dice authentication tokens."""
        for c in cookies:
            name = (c.get("name") or "").lower()
            if any(auth_key in name for auth_key in self.DICE_AUTH_COOKIE_NAMES):
                val = c.get("value") or ""
                if "identity" in name:
                    if len(val) > 10:
                        return True
                elif len(val) > 0:
                    return True
        return False

    async def start_interactive_login(self) -> Dict[str, Any]:
        """
        Starts an interactive browser login session.
        - In cloud/headless mode: Returns login URL and instructions for client-side sign-in & sync.
        - In local desktop mode: Opens a visible Chromium window on the user's desktop with full login supervision.
        """
        if playwright_manager.is_cloud_mode:
            logger.info("start_interactive_login requested in headless/cloud mode.")
            status = await settings_service.get_dice_status(check_live=True)
            if status.get("is_connected"):
                return {
                    "status": "success",
                    "login_url": "https://www.dice.com/dashboard/login",
                    "message": "Dice session is already active in the cloud environment.",
                    "is_connected": True,
                    "cloud_mode": True,
                    "browser_opened": False
                }
            self._status = "WAITING_FOR_LOGIN"
            await self.broadcast("LOGIN_STARTED", {
                "message": "Dice login tab opened. Complete sign in, then sync your session.",
                "manager_status": "WAITING_FOR_LOGIN",
                "cloud_mode": True
            })
            return {
                "status": "success",
                "login_url": "https://www.dice.com/dashboard/login",
                "message": "Opening Dice login tab. Complete sign in, then sync your session.",
                "is_connected": False,
                "cloud_mode": True,
                "browser_opened": False
            }

        async with self._lock:
            # If a login window is already open and responsive, bring it to front
            if self._active_page and not self._active_page.is_closed():
                try:
                    await self._active_page.bring_to_front()
                    return {
                        "status": "success",
                        "login_url": "https://www.dice.com/dashboard/login",
                        "message": "Dice login window is already open on your desktop. Please complete sign in.",
                        "manager_status": self._status,
                        "cloud_mode": False,
                        "browser_opened": True
                    }
                except Exception:
                    self._active_page = None

            # Cancel any previous watcher task
            if self._watcher_task and not self._watcher_task.done():
                self._watcher_task.cancel()
                self._watcher_task = None

            try:
                page = await playwright_manager.get_new_page()
                self._active_page = page
                self._status = "WAITING_FOR_LOGIN"

                await page.goto("https://www.dice.com/dashboard/login", wait_until="domcontentloaded", timeout=25000)

                # Broadcast login start to frontend
                await self.broadcast("LOGIN_STARTED", {
                    "message": "Browser opened to Dice login page on desktop.",
                    "manager_status": "WAITING_FOR_LOGIN",
                    "cloud_mode": False
                })

                # Spawn supervised watcher
                self._watcher_task = asyncio.create_task(
                    self._supervise_login_lifecycle(page),
                    name="dice_login_supervisor"
                )

                return {
                    "status": "success",
                    "login_url": "https://www.dice.com/dashboard/login",
                    "message": "Visible browser opened to Dice login page on your desktop.",
                    "manager_status": "WAITING_FOR_LOGIN",
                    "cloud_mode": False,
                    "browser_opened": True
                }
            except Exception as e:
                self._status = "IDLE"
                self._active_page = None
                logger.error(f"Failed to initiate interactive login: {e}", exc_info=True)
                raise

    def _is_authenticated_url(self, url: str) -> bool:
        """Determines if the given URL represents an authenticated area of Dice.com."""
        u = url.lower()
        if "dice.com" not in u:
            return False
        # If still on auth/login URLs, not yet authenticated
        auth_tokens = ["/dashboard/login", "/signin", "login.dice.com", "/auth0", "/authorize", "/oauth2"]
        if any(token in u for token in auth_tokens):
            return False
        # Standard post-login landing pages or portal root
        return any(path in u for path in ["/home", "/dashboard", "/jobs", "/profile", "/candidates", "/search", "/feed", "/my-dice", "/applications"]) or u.rstrip("/").endswith("dice.com")

    async def _supervise_login_lifecycle(self, page: Page):
        """
        Event-driven supervisor for the login page.
        Watches for navigation away from login, authentication cookie detection, or window close.
        When authentication completes, captures cookies, verifies session, and notifies subscribers in real-time.
        """
        logger.info("[DiceSessionManager] Watching for login completion...")
        login_completed = asyncio.Event()
        window_closed = asyncio.Event()

        def on_navigated(frame):
            if frame == page.main_frame:
                current_url = page.url
                if self._is_authenticated_url(current_url):
                    logger.info(f"[DiceSessionManager] Main frame navigated to authenticated URL: {current_url}")
                    login_completed.set()

        def on_close(p):
            logger.info("[DiceSessionManager] Login page was closed.")
            window_closed.set()

        page.on("framenavigated", on_navigated)
        page.on("close", on_close)

        # Immediate check if profile was already authenticated upon page load
        try:
            if not page.is_closed():
                initial_cookies = await page.context.cookies()
                dice_cookies = [c for c in initial_cookies if "dice.com" in c.get("domain", "")]
                if self._has_authenticated_cookies(dice_cookies) or self._is_authenticated_url(page.url):
                    login_completed.set()
        except Exception:
            pass

        try:
            start_time = asyncio.get_event_loop().time()
            max_duration = 600.0  # 10 minutes

            while not login_completed.is_set() and not window_closed.is_set():
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed >= max_duration:
                    logger.warning("[DiceSessionManager] Login session timed out after 10 minutes.")
                    break

                try:
                    await asyncio.wait(
                        [
                            asyncio.create_task(login_completed.wait()),
                            asyncio.create_task(window_closed.wait()),
                        ],
                        timeout=2.0,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                except Exception:
                    pass

                if login_completed.is_set() or window_closed.is_set():
                    break

                # Periodic cookie check (fallback for SPAs without full navigation)
                try:
                    if not page.is_closed():
                        current_url = page.url
                        cookies = await page.context.cookies()
                        dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]

                        if self._has_authenticated_cookies(dice_cookies):
                            logger.info(f"[DiceSessionManager] Detected authenticated session cookies (count: {len(dice_cookies)}).")
                            login_completed.set()
                            break

                        if self._is_authenticated_url(current_url) and len(dice_cookies) >= 5:
                            logger.info(f"[DiceSessionManager] Main frame reached authenticated URL: {current_url}")
                            login_completed.set()
                            break
                except Exception:
                    pass

            if login_completed.is_set():
                logger.info("[DiceSessionManager] Login succeeded! Extracting session cookies...")
                self._status = "CONNECTED"
                try:
                    cookies = await page.context.cookies()
                    dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]

                    # Import session into MongoDB and active context
                    import_result = await settings_service.import_dice_session(cookies=dice_cookies)
                    candidate_username = import_result.get("username", "")

                    # Broadcast connected event to all SSE subscribers
                    await self.broadcast("DICE_CONNECTED", {
                        "is_connected": True,
                        "username": candidate_username,
                        "cookies_count": len(dice_cookies),
                        "manager_status": "CONNECTED",
                        "message": f"Dice account connected successfully! Account: {candidate_username or 'Authenticated Candidate'}"
                    })
                    logger.info("[DiceSessionManager] Real-time DICE_CONNECTED event broadcast to all clients.")

                    # Wait briefly for user to see success, then close the login page
                    await asyncio.sleep(2.5)
                    if not page.is_closed():
                        await page.close()
                except Exception as e:
                    logger.error(f"[DiceSessionManager] Failed to import session after login: {e}")
            elif window_closed.is_set():
                logger.info("[DiceSessionManager] User closed login window. Checking if authentication completed prior to close...")
                try:
                    if playwright_manager.context:
                        cookies = await playwright_manager.context.cookies()
                        dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]
                        if self._has_authenticated_cookies(dice_cookies):
                            logger.info("[DiceSessionManager] Valid auth cookies found on window close! Connecting...")
                            self._status = "CONNECTED"
                            import_result = await settings_service.import_dice_session(cookies=dice_cookies)
                            candidate_username = import_result.get("username", "")
                            await self.broadcast("DICE_CONNECTED", {
                                "is_connected": True,
                                "username": candidate_username,
                                "cookies_count": len(dice_cookies),
                                "manager_status": "CONNECTED",
                                "message": f"Dice account connected successfully! Account: {candidate_username or 'Authenticated Candidate'}"
                            })
                            return
                except Exception as check_e:
                    logger.debug(f"Window close cookie inspection error: {check_e}")

                self._status = "CANCELLED"
                await self.broadcast("LOGIN_CANCELLED", {
                    "manager_status": "CANCELLED",
                    "message": "Dice login window was closed."
                })
            else:
                self._status = "IDLE"
                await self.broadcast("LOGIN_TIMEOUT", {
                    "manager_status": "IDLE",
                    "message": "Dice login session timed out."
                })
        except asyncio.CancelledError:
            logger.info("[DiceSessionManager] Login supervisor cancelled.")
            self._status = "IDLE"
        finally:
            self._active_page = None
            self._watcher_task = None

    async def cleanup(self):
        """Cleans up active tasks and pages on shutdown."""
        logger.info("[DiceSessionManager] Cleaning up active login tasks...")
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            self._watcher_task = None
        self._active_page = None
        self._status = "IDLE"

dice_session_manager = DiceSessionManager()
