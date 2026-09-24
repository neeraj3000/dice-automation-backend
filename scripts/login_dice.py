"""
Dice.com One-Time Interactive Login Script.

This launches the Playwright persistent browser profile in headed (visible) mode
so you can manually log in to your Dice.com account (entering your email, password,
and any 2FA/CAPTCHA prompts).

Once logged in, all session cookies and tokens are automatically saved in:
data/browser_profile/

All subsequent automated searches and job applications will reuse this session!
"""
import asyncio
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from playwright.async_api import async_playwright
from app.config import settings

async def main():
    profile_dir = settings.DATA_DIR / "browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("DICE.COM ONE-TIME BROWSER LOGIN HELPER")
    print("=" * 70)
    print(f"Using persistent browser profile directory:\n  {profile_dir}")
    print("\nLaunching visible Chrome browser...")

    async with async_playwright() as p:
        chrome_path = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        channel = "chrome" if chrome_path.exists() else None

        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-infobars",
            "--start-maximized",
        ]

        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel=channel,
                headless=False,
                args=args,
                viewport=None,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )
        except Exception:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                args=args,
                viewport=None,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            )

        page = context.pages[0] if context.pages else await context.new_page()

        print("\nNavigating to https://www.dice.com/dashboard/login ...")
        await page.goto("https://www.dice.com/dashboard/login")

        print("\n[ACTION REQUIRED]")
        print("1. Log in to your Dice account in the opened browser window.")
        print("2. Complete any 2FA or CAPTCHA if prompted.")
        print("3. Once you see your Dice Dashboard / search page, return here.")
        print("=" * 70)

        # Wait for user input in terminal
        try:
            await asyncio.to_thread(input, "Press ENTER here once you have finished logging in to save session...")
        except EOFError:
            await asyncio.sleep(10)

        print("\nSaving session cookies to profile directory...")
        await context.close()
        print("[SUCCESS] Session saved! Your Dice account is now connected.")
        print("The automation will now run with your authenticated Dice session.")

if __name__ == "__main__":
    asyncio.run(main())
