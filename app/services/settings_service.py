import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.database import get_database
from app.config import settings
from app.schemas.user_profile import UserProfileSchema, AppSettingsSchema

logger = logging.getLogger(__name__)

class SettingsService:
    @property
    def db(self):
        return get_database()

    async def get_profile(self) -> UserProfileSchema:
        doc = await self.db.user_profile.find_one({})
        if not doc:
            return UserProfileSchema()
        doc.pop("_id", None)
        return UserProfileSchema(**doc)

    async def update_profile(self, profile: UserProfileSchema) -> UserProfileSchema:
        data = profile.model_dump()
        data["updated_at"] = datetime.now(timezone.utc)
        await self.db.user_profile.update_one({}, {"$set": data}, upsert=True)
        return profile

    async def get_settings(self) -> AppSettingsSchema:
        doc = await self.db.app_settings.find_one({})
        if not doc:
            return AppSettingsSchema()
        doc.pop("_id", None)
        doc.pop("openai_api_key", None)
        doc.pop("openai_model", None)
        return AppSettingsSchema(**doc)

    async def update_settings(self, app_settings: AppSettingsSchema) -> AppSettingsSchema:
        data = app_settings.model_dump()
        data.pop("openai_api_key", None)
        data.pop("openai_model", None)
        data["updated_at"] = datetime.now(timezone.utc)
        await self.db.app_settings.update_one(
            {},
            {
                "$set": data,
                "$unset": {"openai_api_key": "", "openai_model": ""}
            },
            upsert=True
        )
        return app_settings

    def _is_hardcoded_name(self, name: Optional[str]) -> bool:
        """Check if a name string matches the obsolete hardcoded placeholder."""
        if not name:
            return True
        clean = name.strip()
        return clean in ("VS (Veera Sekhar)", "Veera Sekhar", "VS", "vs")

    async def _resolve_generic_username(self) -> str:
        """
        Dynamically resolve a user display name generically from the user profile,
        or fallback to 'Dice User'. Never returns hardcoded personal names.
        """
        try:
            profile = await self.get_profile()
            first = (profile.first_name or "").strip()
            last = (profile.last_name or "").strip()
            full_name = f"{first} {last}".strip()
            if full_name and not self._is_hardcoded_name(full_name):
                initials = "".join([part[0].upper() for part in full_name.split() if part])
                return f"{initials} ({full_name})" if initials else full_name
            if profile.email:
                return profile.email.split("@")[0]
        except Exception:
            pass
        return "Dice User"

    async def _verify_cookies_via_http(self, cookies: list) -> Dict[str, Any]:
        """
        Production-grade lightweight session verification using HTTP requests.
        Pings the authenticated Dice endpoint with the cookies, avoiding the
        overhead, process lock conflicts, and latency of launching a full browser.
        """
        if not cookies:
            return {"is_connected": False, "reason": "No cookies provided"}

        cookie_jar = {}
        for c in cookies:
            domain = c.get("domain", "")
            if "dice.com" in domain:
                name = c.get("name")
                value = c.get("value")
                if name and value:
                    cookie_jar[name] = value

        if len(cookie_jar) < 3:
            return {"is_connected": False, "reason": "Insufficient Dice cookies found"}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.dice.com/",
        }

        try:
            import httpx
            async with httpx.AsyncClient(headers=headers, cookies=cookie_jar, follow_redirects=False, timeout=8.0) as client:
                resp = await client.get("https://www.dice.com/home")

                # If redirected to login/signin, session is definitely expired
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "").lower()
                    if "login" in location or "signin" in location:
                        return {"is_connected": False, "reason": "Session expired (redirected to login)"}
                    if "/home" in location or "/dashboard" in location:
                        return {"is_connected": True, "reason": "Active session"}

                if resp.status_code == 200:
                    text = resp.text[:5000].lower()
                    if "sign in" in text and "create account" in text and "/dashboard/login" in text:
                        return {"is_connected": False, "reason": "Login page rendered"}
                    return {"is_connected": True, "reason": "Active session"}

                return {"is_connected": False, "reason": f"Unexpected HTTP status {resp.status_code}"}
        except Exception as e:
            logger.warning(f"HTTP session verification network error: {e}")
            return {"is_connected": False, "reason": f"Network error ({e})"}

    async def get_dice_status(self, check_live: bool = False) -> Dict[str, Any]:
        """
        Returns the current Dice account connection status.
        Uses lightweight HTTP verification (production standard) when check_live is True,
        completely avoiding the need to open a browser tab.
        """
        doc = await self.db.app_settings.find_one({}) or {}
        
        # Fast path: Return cached status immediately when check_live is False
        if not check_live and doc.get("dice_session_connected") is not None:
            is_connected = bool(doc.get("dice_session_connected", False))
            username = doc.get("dice_username", "")
            if is_connected and self._is_hardcoded_name(username):
                username = await self._resolve_generic_username()
            return {
                "is_connected": is_connected,
                "username": username if is_connected else "",
                "cookies_count": doc.get("dice_cookies_count", 0),
                "last_verified": doc.get("dice_last_verified") or datetime.now(timezone.utc).isoformat()
            }

        # 1. Collect cookies from MongoDB saved_cookies or active browser context
        cookies = doc.get("saved_cookies", [])
        
        from app.browser.playwright_manager import playwright_manager
        if playwright_manager.context:
            try:
                browser_cookies = await playwright_manager.context.cookies()
                if browser_cookies:
                    cookies = browser_cookies
            except Exception:
                pass

        dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]
        
        # 2. Verify session
        is_connected = False
        username = ""
        
        if len(dice_cookies) >= 3:
            verify_result = await self._verify_cookies_via_http(dice_cookies)
            is_connected = verify_result.get("is_connected", False)
            if is_connected and not doc.get("saved_cookies"):
                # If cookies were extracted from browser context, persist to database
                await self.import_dice_session(cookies=dice_cookies)
        else:
            is_connected = False

        if is_connected:
            stored_user = doc.get("dice_username", "")
            if stored_user and not self._is_hardcoded_name(stored_user):
                username = stored_user
            else:
                username = await self._resolve_generic_username()

        now_iso = datetime.now(timezone.utc).isoformat()
        final_username = username if is_connected else ""

        await self.db.app_settings.update_one(
            {},
            {"$set": {
                "dice_session_connected": is_connected,
                "dice_username": final_username,
                "dice_cookies_count": len(dice_cookies),
                "dice_last_verified": now_iso
            }},
            upsert=True
        )

        return {
            "is_connected": is_connected,
            "username": final_username,
            "cookies_count": len(dice_cookies),
            "last_verified": now_iso
        }

    async def verify_session_on_startup(self) -> Dict[str, Any]:
        """
        Verify the Dice session when the application starts up,
        log/display the current connection status, and store the result in MongoDB.
        """
        print("[Dice-Automation] ==================================================")
        print("[Dice-Automation] Verifying Dice session on startup...")
        try:
            status = await self.get_dice_status(check_live=True)
            is_connected = status.get("is_connected", False)
            username = status.get("username", "")
            cookies_count = status.get("cookies_count", 0)
            last_verified = status.get("last_verified", "")

            if is_connected:
                display_user = username or "Authenticated User"
                print(f"[Dice-Automation] Session Status : CONNECTED")
                print(f"[Dice-Automation] Active User    : {display_user}")
                print(f"[Dice-Automation] Cookies Count  : {cookies_count}")
                print(f"[Dice-Automation] Verified At    : {last_verified}")
            else:
                print(f"[Dice-Automation] Session Status : NOT CONNECTED (Login required)")
                print(f"[Dice-Automation] Cookies Count  : {cookies_count}")
                if cookies_count > 0:
                    print(f"[Dice-Automation] Reason         : Cookies expired or invalid session")
                else:
                    print(f"[Dice-Automation] Reason         : No Dice cookies found")
                print(f"[Dice-Automation] Action         : Import session via UI or run 'python scripts/login_dice.py'")
            print("[Dice-Automation] ==================================================")
            return status
        except Exception as e:
            logger.error(f"Error during startup Dice session verification: {e}")
            print(f"[Dice-Automation] Session Status : VERIFICATION FAILED ({e})")
            print("[Dice-Automation] ==================================================")
            return {
                "is_connected": False,
                "username": "",
                "cookies_count": 0,
                "last_verified": datetime.now(timezone.utc).isoformat(),
                "error": str(e)
            }

    async def import_dice_session(
        self,
        cookies: Optional[list] = None,
        cookie_string: Optional[str] = None,
        username: Optional[str] = None
    ) -> Dict[str, Any]:
        parsed_cookies = []

        if cookies:
            for c in cookies:
                cookie_dict = dict(c)
                if "domain" not in cookie_dict or not cookie_dict["domain"]:
                    cookie_dict["domain"] = ".dice.com"
                if "path" not in cookie_dict or not cookie_dict["path"]:
                    cookie_dict["path"] = "/"
                parsed_cookies.append(cookie_dict)

        elif cookie_string:
            parts = cookie_string.split(";")
            for part in parts:
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    if k.strip():
                        parsed_cookies.append({
                            "name": k.strip(),
                            "value": v.strip(),
                            "domain": ".dice.com",
                            "path": "/"
                        })

        if not parsed_cookies:
            return {
                "status": "error",
                "message": "No valid cookies found to import. Provide a cookie string or cookie array."
            }

        # Inject cookies into active Playwright context if already running
        from app.browser.playwright_manager import playwright_manager
        if playwright_manager.context:
            try:
                await playwright_manager.context.add_cookies(parsed_cookies)
            except Exception:
                pass

        now_iso = datetime.now(timezone.utc).isoformat()
        resolved_username = username.strip() if (username and not self._is_hardcoded_name(username)) else ""
        if not resolved_username:
            resolved_username = await self._resolve_generic_username()

        # Save to database
        await self.db.app_settings.update_one(
            {},
            {"$set": {
                "saved_cookies": parsed_cookies,
                "dice_session_connected": True,
                "dice_username": resolved_username,
                "dice_cookies_count": len(parsed_cookies),
                "dice_last_verified": now_iso
            }},
            upsert=True
        )

        return {
            "status": "success",
            "message": f"Successfully imported {len(parsed_cookies)} session cookies! Dice is now connected.",
            "is_connected": True,
            "username": resolved_username,
            "cookies_count": len(parsed_cookies),
            "last_verified": now_iso
        }

settings_service = SettingsService()

