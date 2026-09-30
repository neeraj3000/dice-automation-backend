import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.database import get_database
from app.config import settings
from app.schemas.user_profile import UserProfileSchema, AppSettingsSchema

logger = logging.getLogger(__name__)

def sanitize_cookie_for_playwright(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Sanitizes any cookie dictionary into the exact schema required by Playwright's add_cookies().
    Strips browser extension metadata (e.g., hostOnly, session, storeId), normalizes sameSite to
    case-sensitive 'Strict'|'Lax'|'None', converts expirationDate/expires to float seconds,
    and ensures clean domains and paths.
    """
    if not isinstance(c, dict):
        return None
    name = str(c.get("name", "")).strip()
    value = str(c.get("value", ""))
    if not name:
        return None

    clean: Dict[str, Any] = {
        "name": name,
        "value": value,
    }

    # Normalize domain
    domain = str(c.get("domain", "") or "").strip()
    if domain:
        if "://" in domain:
            domain = domain.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
        domain = domain.split("/", 1)[0].split(":", 1)[0]
        clean["domain"] = domain
    else:
        clean["domain"] = ".dice.com"

    # Normalize path
    path = str(c.get("path", "") or "").strip()
    clean["path"] = path if path else "/"

    # Normalize sameSite (Playwright strictly requires 'Strict', 'Lax', or 'None')
    same_site = c.get("sameSite")
    if same_site and isinstance(same_site, str):
        ss_lower = same_site.strip().lower()
        if ss_lower == "strict":
            clean["sameSite"] = "Strict"
        elif ss_lower == "lax":
            clean["sameSite"] = "Lax"
        elif ss_lower in ("none", "no_restriction"):
            clean["sameSite"] = "None"

    # Normalize expires / expirationDate
    exp = c.get("expires") if c.get("expires") is not None else c.get("expirationDate")
    if exp is not None:
        try:
            exp_float = float(exp)
            if exp_float > 1e11:  # Timestamp in milliseconds
                exp_float = exp_float / 1000.0
            if exp_float > 0 or exp_float == -1:
                clean["expires"] = exp_float
        except (ValueError, TypeError):
            pass

    # Normalize booleans
    if "httpOnly" in c:
        clean["httpOnly"] = bool(c["httpOnly"])
    if "secure" in c:
        clean["secure"] = bool(c["secure"])

    return clean


def parse_raw_cookie_input(raw: str) -> list:
    """
    Parses cookies from any format:
    - JSON array of cookies [{name, value, ...}]
    - JSON object with 'cookies' array or key-value pairs
    - cURL command copied from DevTools (-H 'cookie: ...')
    - Netscape HTTP Cookie File format (tab-separated)
    - Standard semicolon-separated cookie string (e.g. document.cookie)
    """
    cookies = []
    trimmed = raw.strip()

    # 1. cURL header check
    import re
    curl_match = re.search(r'-H\s+[\'"][Cc]ookie:\s*([^\'"]+)[\'"]', trimmed)
    if curl_match:
        trimmed = curl_match.group(1).strip()
    elif trimmed.lower().startswith("cookie:"):
        trimmed = trimmed[7:].strip()

    # 2. JSON check
    if trimmed.startswith("{") or trimmed.startswith("["):
        try:
            import json
            data = json.loads(trimmed)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        cookies.append(item)
            elif isinstance(data, dict):
                if "cookies" in data and isinstance(data["cookies"], list):
                    cookies.extend(data["cookies"])
                elif "name" in data and "value" in data:
                    cookies.append(data)
                else:
                    for k, v in data.items():
                        if isinstance(v, str):
                            cookies.append({"name": k, "value": v, "domain": ".dice.com", "path": "/"})
            if cookies:
                return cookies
        except Exception:
            pass

    # 3. Netscape format (tab-separated lines)
    if "\t" in trimmed and not trimmed.startswith("http"):
        for line in trimmed.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append({
                    "domain": parts[0],
                    "path": parts[2],
                    "secure": parts[3].lower() == "true",
                    "expires": float(parts[4]) if parts[4].replace(".", "", 1).isdigit() else -1,
                    "name": parts[5],
                    "value": parts[6]
                })
        if cookies:
            return cookies

    # 4. Standard semicolon separated: foo=bar; baz=qux
    parts = trimmed.split(";")
    for part in parts:
        if "=" in part:
            k, v = part.strip().split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                cookies.append({"name": k, "value": v, "domain": ".dice.com", "path": "/"})

    return cookies


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
        Pings authenticated Dice endpoints with the cookies, avoiding browser overhead.
        Accurately detects when sessions are redirected to login/signin (i.e. expired).
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

        if len(cookie_jar) < 2:
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
            "identity", "refreshtoken", "candidate_id", "peopleid",
            "dice_member_id", "dice_session", "dice-user-id", "_oauth2_proxy"
        )
        has_known_auth_token = has_valid_identity or any(
            any(t == k.lower() or t in k.lower() for t in auth_tokens_to_check)
            for k in cookie_jar
        )

        try:
            import httpx
            async with httpx.AsyncClient(headers=headers, cookies=cookie_jar, follow_redirects=False, timeout=8.0) as client:
                # Test primary candidate dashboard endpoint
                resp = await client.get("https://www.dice.com/dashboard")

                # If redirected, check where it redirects to
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "").lower()
                    if any(bad in location for bad in ("login", "signin", "auth0", "authorize")):
                        return {"is_connected": False, "reason": "Session expired (redirected to login)"}
                    if any(good in location for good in ("/home", "/dashboard", "/profile", "/jobs", "/candidates")):
                        return {"is_connected": True, "reason": "Active session"}

                if resp.status_code == 200:
                    text = resp.text[:5000].lower()
                    if ("/dashboard/login" in text or "login.dice.com" in text) and "sign in" in text:
                        return {"is_connected": False, "reason": "Login page rendered (session expired)"}
                    return {"is_connected": True, "reason": "Active session"}

                if resp.status_code in (401, 403):
                    return {"is_connected": False, "reason": f"Authentication required (HTTP {resp.status_code})"}

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
        username: Optional[str] = None,
        local_storage: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        raw_cookies: list = []

        if cookies:
            if isinstance(cookies, list):
                raw_cookies.extend(cookies)
            elif isinstance(cookies, dict):
                raw_cookies.append(cookies)

        if cookie_string and isinstance(cookie_string, str):
            parsed = parse_raw_cookie_input(cookie_string)
            raw_cookies.extend(parsed)

        # Handle localStorage payload (e.g. AWS Cognito tokens exported from browser console)
        if local_storage and isinstance(local_storage, dict):
            id_token = None
            refresh_token = None
            access_token = None
            last_user = None

            for k, v in local_storage.items():
                if not isinstance(v, str):
                    continue
                k_lower = k.lower()
                if "idtoken" in k_lower:
                    id_token = v
                elif "refreshtoken" in k_lower:
                    refresh_token = v
                elif "accesstoken" in k_lower:
                    access_token = v
                elif "lastauthuser" in k_lower:
                    last_user = v

            if id_token:
                raw_cookies.append({"name": "identity", "value": id_token, "domain": ".dice.com", "path": "/"})
            if refresh_token:
                raw_cookies.append({"name": "refreshToken", "value": refresh_token, "domain": ".dice.com", "path": "/"})
            if access_token:
                raw_cookies.append({"name": "access", "value": access_token, "domain": ".dice.com", "path": "/"})
            if last_user and not username:
                username = last_user

        # Sanitize and deduplicate cookies for Playwright
        sanitized_cookies = []
        seen_keys = set()
        for c in raw_cookies:
            clean = sanitize_cookie_for_playwright(c)
            if clean:
                dedup_key = (clean["name"].lower(), clean.get("domain", "").lower(), clean.get("path", "/"))
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    sanitized_cookies.append(clean)

        if not sanitized_cookies:
            return {
                "status": "error",
                "message": "No valid cookies found to import. Provide a cookie string, JSON array, or localStorage tokens.",
                "is_connected": False
            }

        # Dynamically extract candidate name and email from identity JWT cookie if present
        extracted_name = ""
        for c in sanitized_cookies:
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

        # Perform live HTTP verification of the imported cookies
        is_connected = True
        verify_reason = "Active session"
        if len(sanitized_cookies) >= 3:
            verify_result = await self._verify_cookies_via_http(sanitized_cookies)
            if verify_result.get("reason") == "Session expired (redirected to login)":
                is_connected = False
                verify_reason = verify_result.get("reason", "Session expired")
            else:
                is_connected = verify_result.get("is_connected", True)
                verify_reason = verify_result.get("reason", "Active session")

        # Inject sanitized cookies into active Playwright context if running
        from app.browser.playwright_manager import playwright_manager
        try:
            await playwright_manager.add_cookies_safely(sanitized_cookies)
        except Exception as e:
            logger.debug(f"Could not inject cookies into active browser context: {e}")

        # Save sanitized cookies and status to database
        await self.db.app_settings.update_one(
            {},
            {"$set": {
                "saved_cookies": sanitized_cookies,
                "dice_session_connected": is_connected,
                "dice_username": resolved_username if is_connected else "",
                "dice_cookies_count": len(sanitized_cookies),
                "dice_last_verified": now_iso
            }},
            upsert=True
        )

        if is_connected:
            # Broadcast connection success via SSE to all frontend subscribers
            try:
                from app.services.dice_session_manager import dice_session_manager
                await dice_session_manager.broadcast("DICE_CONNECTED", {
                    "message": f"Dice session connected successfully! Account: {resolved_username}",
                    "username": resolved_username,
                    "cookies_count": len(sanitized_cookies),
                    "is_connected": True
                })
            except Exception as e:
                logger.debug(f"Could not broadcast DICE_CONNECTED: {e}")

            return {
                "status": "success",
                "message": f"Successfully connected {len(sanitized_cookies)} session cookies! Dice account: {resolved_username}.",
                "is_connected": True,
                "username": resolved_username,
                "cookies_count": len(sanitized_cookies),
                "last_verified": now_iso
            }
        else:
            return {
                "status": "success",
                "message": f"Imported {len(sanitized_cookies)} cookies, but Dice verification noted: {verify_reason}. Please make sure you are signed in on Dice.",
                "is_connected": False,
                "username": resolved_username,
                "cookies_count": len(sanitized_cookies),
                "last_verified": now_iso
            }

settings_service = SettingsService()

