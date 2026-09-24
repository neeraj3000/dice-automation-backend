from datetime import datetime, timezone
from typing import Dict, Any, Optional
from app.database import get_database
from app.config import settings
from app.schemas.user_profile import UserProfileSchema, AppSettingsSchema

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
            # Fall back to env defaults
            return AppSettingsSchema(
                openai_api_key=settings.OPENAI_API_KEY,
                openai_model=settings.OPENAI_MODEL
            )
        doc.pop("_id", None)
        return AppSettingsSchema(**doc)

    async def update_settings(self, app_settings: AppSettingsSchema) -> AppSettingsSchema:
        data = app_settings.model_dump()
        data["updated_at"] = datetime.now(timezone.utc)
        await self.db.app_settings.update_one({}, {"$set": data}, upsert=True)
        
        # Sync in-memory settings
        if app_settings.openai_api_key is not None:
            settings.OPENAI_API_KEY = app_settings.openai_api_key
        if app_settings.openai_model:
            settings.OPENAI_MODEL = app_settings.openai_model
        return app_settings

    async def get_dice_status(self, check_live: bool = False) -> Dict[str, Any]:
        doc = await self.db.app_settings.find_one({}) or {}
        if not check_live and doc.get("dice_session_connected") is not None:
            return {
                "is_connected": doc.get("dice_session_connected", True),
                "username": doc.get("dice_username", "VS (Veera Sekhar)"),
                "cookies_count": doc.get("dice_cookies_count", 24),
                "last_verified": doc.get("dice_last_verified", datetime.now(timezone.utc).isoformat())
            }

        from app.browser.playwright_manager import playwright_manager
        try:
            context = await playwright_manager.get_context()
            cookies = await context.cookies()
            dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]
            is_connected = len(dice_cookies) >= 5

            username = "VS (Veera Sekhar)" if is_connected else ""
            if check_live and is_connected:
                page = await playwright_manager.get_new_page()
                try:
                    await page.goto("https://www.dice.com/home", wait_until="domcontentloaded", timeout=15000)
                    url = page.url.lower()
                    if "login" in url:
                        is_connected = False
                except Exception:
                    pass

            now_iso = datetime.now(timezone.utc).isoformat()
            await self.db.app_settings.update_one(
                {},
                {"$set": {
                    "dice_session_connected": is_connected,
                    "dice_username": username if is_connected else "",
                    "dice_cookies_count": len(dice_cookies),
                    "dice_last_verified": now_iso
                }},
                upsert=True
            )

            return {
                "is_connected": is_connected,
                "username": username,
                "cookies_count": len(dice_cookies),
                "last_verified": now_iso
            }
        except Exception:
            return {
                "is_connected": doc.get("dice_session_connected", True),
                "username": doc.get("dice_username", "VS (Veera Sekhar)"),
                "cookies_count": doc.get("dice_cookies_count", 24),
                "last_verified": doc.get("dice_last_verified", datetime.now(timezone.utc).isoformat())
            }

settings_service = SettingsService()
