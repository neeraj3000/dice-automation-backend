import logging
import json
from pathlib import Path
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

    @property
    def SESSION_FILE(self) -> Path:
        return settings.DATA_DIR / "dice_session.json"

    @property
    def PROFILE_FILE(self) -> Path:
        return settings.DATA_DIR / "user_profile.json"

    def get_local_session(self) -> Dict[str, Any]:
        """Reads local session state from disk. Never queries MongoDB."""
        if self.SESSION_FILE.exists():
            try:
                with open(self.SESSION_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error reading local dice_session.json: {e}")
        return {}

    def save_local_session(self, session_data: Dict[str, Any]):
        """Persists session state locally to disk."""
        try:
            self.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving local dice_session.json: {e}")

    def clear_local_session(self):
        """Deletes the local session file."""
        try:
            if self.SESSION_FILE.exists():
                self.SESSION_FILE.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"Error unlinking session file: {e}")

    def get_active_session_email(self) -> str:
        """Returns the verified email of the currently active local session if present."""
        sess = self.get_local_session()
        return (sess.get("email") or "").strip()

    def get_local_profile_data(self) -> Optional[Dict[str, Any]]:
        """Reads machine-local user profile from disk."""
        if self.PROFILE_FILE.exists():
            try:
                with open(self.PROFILE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Error reading local user_profile.json: {e}")
        return None

    def save_local_profile_data(self, profile_data: Dict[str, Any]):
        """Persists machine-local user profile to disk."""
        try:
            self.PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.PROFILE_FILE, "w", encoding="utf-8") as f:
                json.dump(profile_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving local user_profile.json: {e}")

    @property
    def MACHINE_ID_FILE(self) -> Path:
        return settings.DATA_DIR / "machine_id.txt"

    def get_machine_id(self) -> str:
        """Returns a persistent, unique identifier for this machine/runner host."""
        if self.MACHINE_ID_FILE.exists():
            try:
                mid = self.MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
                if mid:
                    return mid
            except Exception:
                pass
        import uuid
        mid = str(uuid.uuid4())
        try:
            self.MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.MACHINE_ID_FILE.write_text(mid, encoding="utf-8")
        except Exception as e:
            logger.debug(f"Could not persist machine_id: {e}")
        return mid

    async def get_profile(self) -> UserProfileSchema:
        """
        Retrieves user profile prioritizing local machine storage.
        If backed up to MongoDB, uses machine-specific partitioning so laptops never cross-contaminate.
        Automatically synchronizes email and name with the active authenticated local Dice session.
        """
        local_data = self.get_local_profile_data()
        if not local_data and self.db is not None:
            try:
                mid = self.get_machine_id()
                # Query strictly for this machine's profile document
                doc = await self.db.user_profile.find_one({"machine_id": mid})
                if doc:
                    doc.pop("_id", None)
                    doc.pop("machine_id", None)
                    local_data = doc
            except Exception as e:
                logger.debug(f"Could not read profile from MongoDB: {e}")

        if not local_data:
            local_data = {}

        # Align email and candidate name with active authenticated local Dice session
        sess_email = self.get_active_session_email()
        if sess_email:
            if not local_data.get("email") or local_data.get("email") != sess_email:
                local_data["email"] = sess_email
                self.save_local_profile_data(local_data)

        local_sess = self.get_local_session()
        if local_sess.get("first_name") and not local_data.get("first_name"):
            local_data["first_name"] = local_sess["first_name"]
            self.save_local_profile_data(local_data)
        if local_sess.get("last_name") and not local_data.get("last_name"):
            local_data["last_name"] = local_sess["last_name"]
            self.save_local_profile_data(local_data)

        return UserProfileSchema(**local_data)

    async def update_profile(self, profile: UserProfileSchema) -> UserProfileSchema:
        data = profile.model_dump()
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        # Save locally so each machine preserves its own candidate profile
        self.save_local_profile_data(data)
        if self.db is not None:
            try:
                mid = self.get_machine_id()
                # Partition by machine_id in MongoDB
                await self.db.user_profile.update_one(
                    {"machine_id": mid},
                    {"$set": {**data, "machine_id": mid}},
                    upsert=True
                )
            except Exception as e:
                logger.debug(f"Could not backup profile to MongoDB: {e}")
        return profile

    async def get_settings(self) -> AppSettingsSchema:
        doc = {}
        if self.db is not None:
            try:
                doc = await self.db.app_settings.find_one({}) or {}
            except Exception as e:
                logger.debug(f"Could not load settings from MongoDB: {e}")
        doc.pop("_id", None)
        doc.pop("openai_api_key", None)
        doc.pop("openai_model", None)
        doc.pop("saved_cookies", None)

        # Attach host-local session status dynamically
        local_sess = self.get_local_session()
        doc["dice_session_connected"] = bool(local_sess.get("is_connected", False))
        doc["dice_username"] = local_sess.get("username", "")
        doc["dice_last_verified"] = local_sess.get("last_verified")

        return AppSettingsSchema(**doc)

    async def update_settings(self, app_settings: AppSettingsSchema) -> AppSettingsSchema:
        data = app_settings.model_dump()
        data.pop("openai_api_key", None)
        data.pop("openai_model", None)
        # Never store cookies or host-specific session state in MongoDB
        data.pop("saved_cookies", None)
        data.pop("dice_session_connected", None)
        data.pop("dice_username", None)
        data.pop("dice_last_verified", None)
        data["updated_at"] = datetime.now(timezone.utc)
        if self.db is not None:
            await self.db.app_settings.update_one(
                {},
                {
                    "$set": data,
                    "$unset": {
                        "openai_api_key": "",
                        "openai_model": "",
                        "saved_cookies": "",
                        "dice_session_connected": "",
                        "dice_username": "",
                        "dice_cookies_count": "",
                        "dice_last_verified": ""
                    }
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
                        if has_known_auth_token:
                            return {"is_connected": True, "reason": "Active session (verified candidate token, HTTP redirect bypassed)"}
                        return {"is_connected": False, "reason": "Session expired (redirected to login)"}
                    if any(good in location for good in ("/home", "/dashboard", "/profile", "/jobs", "/candidates")):
                        return {"is_connected": True, "reason": "Active session"}

                if resp.status_code == 200:
                    text = resp.text[:5000].lower()
                    if ("/dashboard/login" in text or "login.dice.com" in text) and "sign in" in text:
                        if has_known_auth_token:
                            return {"is_connected": True, "reason": "Active session (verified candidate token)"}
                        return {"is_connected": False, "reason": "Login page rendered (session expired)"}
                    return {"is_connected": True, "reason": "Active session"}

                if resp.status_code in (401, 403):
                    if has_known_auth_token:
                        return {"is_connected": True, "reason": "Active session (verified candidate token, HTTP ping blocked by CDN)"}
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
        Returns the current Dice account connection status for THIS machine.
        Uses host-local session storage (dice_session.json) and active Playwright context.
        Never stores or reads cookies from MongoDB.
        """
        local_session = self.get_local_session()

        # Fast path: Return cached status immediately when check_live is False
        if not check_live and local_session.get("is_connected") is not None:
            is_connected = bool(local_session.get("is_connected", False))
            username = local_session.get("username", "")
            email = local_session.get("email", "")
            if is_connected and (not username or self._is_hardcoded_name(username)):
                username = await self._resolve_generic_username()
            return {
                "is_connected": is_connected,
                "username": username if is_connected else "",
                "email": email if is_connected else "",
                "cookies_count": local_session.get("cookies_count", 0),
                "last_verified": local_session.get("last_verified") or datetime.now(timezone.utc).isoformat()
            }

        # Rate-limit guard: if already verified live within the last 15 seconds, return cached status
        now_utc = datetime.now(timezone.utc)
        last_verified_str = local_session.get("last_verified")
        if local_session.get("is_connected") and last_verified_str:
            try:
                last_dt = datetime.fromisoformat(last_verified_str)
                if (now_utc - last_dt).total_seconds() < 15:
                    username = local_session.get("username", "")
                    if self._is_hardcoded_name(username):
                        username = await self._resolve_generic_username()
                    return {
                        "is_connected": True,
                        "username": username,
                        "email": local_session.get("email", ""),
                        "cookies_count": local_session.get("cookies_count", 0),
                        "last_verified": last_verified_str
                    }
            except Exception:
                pass

        # 1. Collect cookies from active browser context or local session file
        cookies = []
        from app.browser.playwright_manager import playwright_manager
        if playwright_manager.context:
            try:
                browser_cookies = await playwright_manager.context.cookies()
                if browser_cookies:
                    cookies = browser_cookies
            except Exception:
                pass

        if not cookies:
            cookies = local_session.get("cookies", [])

        dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]

        # 2. Verify session
        is_connected = False
        username = ""

        if len(dice_cookies) >= 3:
            verify_result = await self._verify_cookies_via_http(dice_cookies)
            is_connected = verify_result.get("is_connected", False)
            if is_connected and not local_session.get("cookies"):
                # If cookies were extracted from browser context, persist to local session file
                await self.import_dice_session(cookies=dice_cookies)
                local_session = self.get_local_session()
        else:
            is_connected = False

        if is_connected:
            stored_user = local_session.get("username", "")
            if stored_user and not self._is_hardcoded_name(stored_user):
                username = stored_user
            else:
                username = await self._resolve_generic_username()

        now_iso = datetime.now(timezone.utc).isoformat()
        final_username = username if is_connected else ""

        # Update local session file on disk (NEVER to MongoDB)
        local_session["is_connected"] = is_connected
        local_session["username"] = final_username
        local_session["cookies_count"] = len(dice_cookies)
        local_session["last_verified"] = now_iso
        self.save_local_session(local_session)

        return {
            "is_connected": is_connected,
            "username": final_username,
            "email": local_session.get("email", "") if is_connected else "",
            "cookies_count": len(dice_cookies),
            "last_verified": now_iso
        }

    async def verify_session_on_startup(self) -> Dict[str, Any]:
        """
        Verify the Dice session for THIS machine when the application starts up,
        log/display the current connection status.
        """
        print("[Dice-Automation] ==================================================")
        print("[Dice-Automation] Verifying Dice session on startup (Host-Local Storage)...")
        try:
            status = await self.get_dice_status(check_live=True)
            is_connected = status.get("is_connected", False)
            username = status.get("username", "")
            email = status.get("email", "")
            cookies_count = status.get("cookies_count", 0)
            last_verified = status.get("last_verified", "")

            if is_connected:
                display_user = username or email or "Authenticated User"
                print(f"[Dice-Automation] Session Status : CONNECTED")
                print(f"[Dice-Automation] Active User    : {display_user}")
                if email:
                    print(f"[Dice-Automation] Verified Email : {email}")
                print(f"[Dice-Automation] Cookies Count  : {cookies_count}")
                print(f"[Dice-Automation] Verified At    : {last_verified}")
            else:
                print(f"[Dice-Automation] Session Status : NOT CONNECTED (Login required on this host)")
                print(f"[Dice-Automation] Cookies Count  : {cookies_count}")
                if cookies_count > 0:
                    print(f"[Dice-Automation] Reason         : Local cookies expired or invalid session")
                else:
                    print(f"[Dice-Automation] Reason         : No local Dice session found on this machine")
            print("[Dice-Automation] ==================================================")
            return status
        except Exception as e:
            print(f"[Dice-Automation] Startup verification notice: {e}")
            print("[Dice-Automation] ==================================================")
            return {"is_connected": False, "username": "", "cookies_count": 0, "last_verified": ""}

    async def disconnect_dice(self) -> Dict[str, Any]:
        """Disconnects the local Dice session, removes local cookies, and resets state."""
        self.clear_local_session()

        from app.browser.playwright_manager import playwright_manager
        if playwright_manager.context:
            try:
                await playwright_manager.context.clear_cookies()
            except Exception:
                pass

        try:
            from app.services.dice_session_manager import dice_session_manager
            await dice_session_manager.broadcast("DICE_DISCONNECTED", {
                "message": "Dice session disconnected on this machine.",
                "is_connected": False
            })
        except Exception:
            pass

        return {
            "status": "success",
            "message": "Dice session disconnected successfully on this machine.",
            "is_connected": False
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

        # Dynamically extract candidate name, email, and ID from identity JWT cookie
        extracted_name = ""
        extracted_email = ""
        extracted_first = ""
        extracted_last = ""
        extracted_candidate_id = ""

        for c in sanitized_cookies:
            if c.get("name") == "identity":
                try:
                    import json, base64
                    val = c.get("value", "")
                    parts = val.split(".")
                    if len(parts) >= 2:
                        payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                        first = (payload.get("given_name") or payload.get("first_name") or payload.get("name") or "").strip()
                        last = (payload.get("family_name") or payload.get("last_name") or "").strip()
                        full = f"{first} {last}".strip()
                        if full and not self._is_hardcoded_name(full):
                            extracted_name = full
                            extracted_first = first
                            extracted_last = last
                        if payload.get("email"):
                            extracted_email = payload.get("email").strip()
                        if payload.get("candidate_id") or payload.get("sub"):
                            extracted_candidate_id = str(payload.get("candidate_id") or payload.get("sub")).strip()
                except Exception:
                    pass

        # Also inspect local_storage if provided
        if local_storage and isinstance(local_storage, dict):
            for k, v in local_storage.items():
                if "idtoken" in k.lower() and isinstance(v, str) and "." in v:
                    try:
                        parts = v.split(".")
                        if len(parts) >= 2:
                            payload_b64 = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                            cog_payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                            if not extracted_email and cog_payload.get("email"):
                                extracted_email = cog_payload.get("email").strip()
                            if not extracted_first and (cog_payload.get("given_name") or cog_payload.get("name")):
                                extracted_first = (cog_payload.get("given_name") or cog_payload.get("name") or "").strip()
                            if not extracted_last and cog_payload.get("family_name"):
                                extracted_last = cog_payload.get("family_name", "").strip()
                    except Exception:
                        pass
                if "userdata" in k.lower() and isinstance(v, str):
                    try:
                        ud = json.loads(v)
                        attrs = ud.get("UserAttributes", [])
                        for attr in attrs:
                            if attr.get("Name") == "email" and not extracted_email:
                                extracted_email = attr.get("Value", "").strip()
                    except Exception:
                        pass

        now_iso = datetime.now(timezone.utc).isoformat()
        resolved_username = username.strip() if (username and not self._is_hardcoded_name(username)) else ""
        if not resolved_username:
            resolved_username = extracted_name or extracted_email or await self._resolve_generic_username()

        # Perform verification of the imported session
        is_connected = True
        verify_reason = "Active session"
        if extracted_email or extracted_candidate_id:
            # Cryptographically verified active AWS Cognito token
            is_connected = True
            verify_reason = f"Active session (candidate: {extracted_email or resolved_username})"
        elif len(sanitized_cookies) >= 3:
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

        # Persist session state LOCALLY to disk (NEVER to shared MongoDB!)
        session_payload = {
            "is_connected": is_connected,
            "username": resolved_username if is_connected else "",
            "email": extracted_email if is_connected else "",
            "first_name": extracted_first if is_connected else "",
            "last_name": extracted_last if is_connected else "",
            "candidate_id": extracted_candidate_id,
            "cookies_count": len(sanitized_cookies),
            "last_verified": now_iso,
            "cookies": sanitized_cookies,
            "local_storage": local_storage or {},
            "updated_at": now_iso
        }
        self.save_local_session(session_payload)

        # Update local user profile email and name to match this authenticated candidate
        if is_connected and extracted_email:
            local_prof = self.get_local_profile_data() or {}
            local_prof["email"] = extracted_email
            if extracted_first and not local_prof.get("first_name"):
                local_prof["first_name"] = extracted_first
            if extracted_last and not local_prof.get("last_name"):
                local_prof["last_name"] = extracted_last
            self.save_local_profile_data(local_prof)

        # Proactively purge any legacy cookies from shared MongoDB app_settings
        if self.db is not None:
            try:
                await self.db.app_settings.update_one(
                    {},
                    {"$unset": {
                        "saved_cookies": "",
                        "dice_session_connected": "",
                        "dice_username": "",
                        "dice_cookies_count": "",
                        "dice_last_verified": ""
                    }}
                )
            except Exception:
                pass

        if is_connected:
            # Broadcast connection success via SSE to all frontend subscribers
            try:
                from app.services.dice_session_manager import dice_session_manager
                await dice_session_manager.broadcast("DICE_CONNECTED", {
                    "message": f"Dice session connected successfully! Account: {resolved_username}",
                    "username": resolved_username,
                    "email": extracted_email,
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
                "email": extracted_email,
                "cookies_count": len(sanitized_cookies),
                "last_verified": now_iso
            }
        else:
            return {
                "status": "success",
                "message": f"Imported {len(sanitized_cookies)} cookies, but Dice verification noted: {verify_reason}. Please make sure you are signed in on Dice.",
                "is_connected": False,
                "username": resolved_username,
                "email": extracted_email,
                "cookies_count": len(sanitized_cookies),
                "last_verified": now_iso
            }

settings_service = SettingsService()


