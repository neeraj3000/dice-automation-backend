"""
Dice Session Exporter & Cloud Syncer.

Exports authenticated Dice session cookies from your local browser profile
and uploads them directly to your deployed Render backend (or outputs them as JSON).

Usage:
  # Upload directly to Render:
  python scripts/export_session.py --url https://dice-automation-backend.onrender.com

  # Just print JSON cookies to copy:
  python scripts/export_session.py
"""
import sys
import json
import argparse
import asyncio
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from playwright.async_api import async_playwright
import httpx
from app.config import settings

async def export_cookies(target_url: str = None):
    profile_dir = settings.DATA_DIR / "browser_profile"
    if not profile_dir.exists():
        print(f"[ERROR] Browser profile directory not found at: {profile_dir}")
        print("Please run 'python scripts/login_dice.py' first to log in locally!")
        return

    print(f"Reading session from: {profile_dir}")
    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            cookies = await context.cookies()
            await context.close()
        except Exception as e:
            print(f"[ERROR] Failed to read cookies from browser profile: {e}")
            return

    dice_cookies = [c for c in cookies if "dice.com" in c.get("domain", "")]
    if not dice_cookies:
        print("[WARNING] No Dice.com cookies found in local profile.")
        print("Please run 'python scripts/login_dice.py' first and log into your Dice account!")
        return

    print(f"[SUCCESS] Found {len(dice_cookies)} Dice.com cookies!")

    if target_url:
        target_url = target_url.rstrip("/")
        endpoint = f"{target_url}/settings/import-dice-session"
        print(f"Uploading session to Render backend: {endpoint} ...")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(endpoint, json={"cookies": dice_cookies})
                if resp.status_code == 200:
                    data = resp.json()
                    print("\n" + "=" * 60)
                    print(f"SUCCESS: {data.get('message')}")
                    print(f"Connected: {data.get('is_connected')}")
                    print(f"Cookies Count: {data.get('cookies_count')}")
                    print("=" * 60)
                    print("\nYour cloud backend on Render is now fully authenticated with Dice!")
                else:
                    print(f"[ERROR] Server returned HTTP {resp.status_code}: {resp.text}")
        except Exception as err:
            print(f"[ERROR] Could not connect to {target_url}: {err}")
    else:
        print("\nSession Cookies JSON (copy & paste into session importer if needed):")
        print(json.dumps(dice_cookies, indent=2))
        print("\nTip: Pass '--url https://<your-render-app>.onrender.com' to upload automatically!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export local Dice session to cloud backend")
    parser.add_argument("--url", help="Your Render backend URL (e.g. https://dice-automation-backend.onrender.com)")
    args = parser.parse_args()
    asyncio.run(export_cookies(target_url=args.url))
