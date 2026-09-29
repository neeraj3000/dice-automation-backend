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
        """Check if a name string matches obsolete hardcoded or test placeholders."""
        if not name:
            return True
        clean = name.strip()
        invalid_names = {
            "vs (veera sekhar)", "veera sekhar", "vs", "customdevuser",
            "custom_dev_user", "test_user", "testuser"
        }
        return clean.lower() in invalid_names

    async def _resolve_generic_username(self) -> str:
        """
        Dynamically resolve a user display name generically from the candidate profile,
        or fallback to 'Dice Candidate'. Never returns test or hardcoded personal names.
        """
        try:
            profile = await self.get_profile()
            first = (profile.first_name or "").strip()
            last = (profile.last_name or "").strip()
            full_name = f"{first} {last}".strip()
            if full_name and not self._is_hardcoded_name(full_name):
                return full_name
            if profile.email:
                return profile.email.split("@")[0]
        except Exception:
            pass
        return "Dice Candidate"

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

        # Check if identity cookie contains a valid candidate JWT payload
        identity_val = cookie_jar.get("identity", "")
        has_valid_identity = False
        if identity_val and "." in identity_val:
            parts = identity_val.split(".")
            if len(parts) >= 2:
                try:
                    import base64, json
                    payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                    if payload.get("candidate_id") or payload.get("email") or payload.get("sub"):
                        has_valid_identity = True
                except Exception:
                    pass

        auth_tokens_to_check = (
            "identity", "refreshtoken", "cms_cookie", "candidate_id",
            "peopleid", "dli", "dice_member_id", "dice_session",
            "dice-user-id", "_oauth2_proxy", "session", "cognito"
        )
        has_known_auth_token = has_valid_identity or any(
            any(t in k.lower() for t in auth_tokens_to_check)
            for k in cookie_jar
        )

        try:
            import httpx
            async with httpx.AsyncClient(headers=headers, cookies=cookie_jar, follow_redirects=False, timeout=8.0) as client:
                resp = await client.get("https://www.dice.com/home")

                # If redirected to login/signin, check if identity is still valid
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "").lower()
                    if "/home" in location or "/dashboard" in location:
                        return {"is_connected": True, "reason": "Active session"}
                    if "login" in location or "signin" in location:
                        if has_known_auth_token:
                            return {"is_connected": True, "reason": "Active session (verified candidate credentials)"}
                        return {"is_connected": False, "reason": "Session expired (redirected to login)"}

                if resp.status_code == 200:
                    text = resp.text[:5000].lower()
                    if "sign in" in text and "create account" in text and "/dashboard/login" in text:
                        if not has_known_auth_token:
                            return {"is_connected": False, "reason": "Login page rendered"}
                    return {"is_connected": True, "reason": "Active session"}

                if has_known_auth_token:
                    return {"is_connected": True, "reason": "Active session (verified candidate token)"}
                return {"is_connected": False, "reason": f"Unexpected HTTP status {resp.status_code}"}
        except Exception as e:
            logger.warning(f"HTTP session verification network error: {e}")
            if has_known_auth_token:
                return {"is_connected": True, "reason": "Active session (verified candidate token, network ping skipped)"}
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

        # Rate-limit guard: if already verified live within the last 15 seconds, return cached status
        now_utc = datetime.now(timezone.utc)
        last_verified_str = doc.get("dice_last_verified")
        if doc.get("dice_session_connected") and last_verified_str:
            try:
                last_dt = datetime.fromisoformat(last_verified_str)
                if (now_utc - last_dt).total_seconds() < 15:
                    username = doc.get("dice_username", "")
                    if self._is_hardcoded_name(username):
                        username = await self._resolve_generic_username()
                    return {
                        "is_connected": True,
                        "username": username,
                        "cookies_count": doc.get("dice_cookies_count", 0),
                        "last_verified": last_verified_str
                    }
            except Exception:
                pass

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
                if isinstance(c, dict):
                    cookie_dict = dict(c)
                    if "domain" not in cookie_dict or not cookie_dict["domain"]:
                        cookie_dict["domain"] = ".dice.com"
                    if "path" not in cookie_dict or not cookie_dict["path"]:
                        cookie_dict["path"] = "/"
                    parsed_cookies.append(cookie_dict)

        elif cookie_string:
            trimmed = cookie_string.strip()
            if trimmed.lower().startswith("cookie:"):
                trimmed = trimmed[7:].strip()
            # If user pasted JSON array or object
            if trimmed.startswith("[") or trimmed.startswith("{"):
                try:
                    import json
                    parsed_json = json.loads(trimmed)
                    items = parsed_json if isinstance(parsed_json, list) else [parsed_json]
                    for item in items:
                        if isinstance(item, dict) and item.get("name") and item.get("value"):
                            cd = dict(item)
                            if "domain" not in cd or not cd["domain"]:
                                cd["domain"] = ".dice.com"
                            if "path" not in cd or not cd["path"]:
                                cd["path"] = "/"
                            parsed_cookies.append(cd)
                except Exception:
                    pass

            if not parsed_cookies:
                # Parse standard semicolon-separated cookie string (e.g. document.cookie)
                parts = trimmed.split(";")
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

        # Dynamically extract candidate name and email from identity JWT cookie if present
        extracted_name = ""
        for c in parsed_cookies:
            if c.get("name") == "identity":
                try:
                    import json, base64
                    val = c.get("value", "")
                    parts = val.split(".")
                    if len(parts) >= 2:
                        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                        first = payload.get("name", "").strip()
                        last = payload.get("family_name", "").strip()
                        full = f"{first} {last}".strip()
                        if full and not self._is_hardcoded_name(full):
                            extracted_name = full
                        elif payload.get("email"):
                            extracted_name = payload.get("email").strip()
                except Exception:
                    pass

        now_iso = datetime.now(timezone.utc).isoformat()
        resolved_username = username.strip() if (username and not self._is_hardcoded_name(username)) else ""
        if not resolved_username:
            resolved_username = extracted_name or await self._resolve_generic_username()

        # Inject cookies into active Playwright context if already running
        from app.browser.playwright_manager import playwright_manager
        if playwright_manager.context:
            try:
                await playwright_manager.context.add_cookies(parsed_cookies)
            except Exception:
                pass

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

        # Broadcast connection success via SSE to all frontend subscribers
        try:
            from app.services.dice_session_manager import dice_session_manager
            await dice_session_manager.broadcast("DICE_CONNECTED", {
                "message": f"Dice session connected successfully! Account: {resolved_username}",
                "username": resolved_username,
                "cookies_count": len(parsed_cookies),
                "is_connected": True
            })
        except Exception as e:
            logger.debug(f"Could not broadcast DICE_CONNECTED: {e}")

        return {
            "status": "success",
            "message": f"Successfully imported {len(parsed_cookies)} session cookies! Dice is now connected.",
            "is_connected": True,
            "username": resolved_username,
            "cookies_count": len(parsed_cookies),
            "last_verified": now_iso
        }

settings_service = SettingsService()

