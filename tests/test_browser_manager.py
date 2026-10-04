import os
import sys
import shutil
import pytest
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.browser.playwright_manager import playwright_manager
from app.config import settings

@pytest.fixture(autouse=True)
async def cleanup_browser_contexts():
    """Ensure contexts are cleanly shut down after each test."""
    yield
    await playwright_manager.close()

@pytest.mark.anyio
async def test_headless_browser_startup():
    """Verify that Playwright launches headless Chromium and navigates without error."""
    page = await playwright_manager.get_new_page(profile_id="test_headless", headless=True)
    assert page is not None
    assert not page.is_closed()

    await page.goto("about:blank")
    content = await page.content()
    assert "<html" in content.lower()

    await page.close()
    await playwright_manager.close_context("test_headless")

@pytest.mark.anyio
async def test_browser_profile_creation(tmp_path):
    """Verify that isolated profile directories are created under the configured data dir."""
    test_profile_id = "user_test_profile_123"
    profile_dir = playwright_manager.get_profile_dir(test_profile_id)

    assert profile_dir.exists()
    assert profile_dir.is_dir()
    assert test_profile_id in str(profile_dir)
    assert playwright_manager.base_data_dir in profile_dir.parents

    # Launch context and verify it uses the isolated profile directory
    ctx = await playwright_manager.get_context(profile_id=test_profile_id, headless=True)
    assert ctx is not None
    assert test_profile_id in playwright_manager._contexts

    await playwright_manager.close_context(test_profile_id)
    assert test_profile_id not in playwright_manager._contexts

@pytest.mark.anyio
async def test_profile_isolation():
    """Verify that User A and User B have separate directories, contexts, and isolated cookie stores."""
    profile_a = "user_alpha"
    profile_b = "user_beta"

    dir_a = playwright_manager.get_profile_dir(profile_a)
    dir_b = playwright_manager.get_profile_dir(profile_b)
    assert dir_a != dir_b

    ctx_a = await playwright_manager.get_context(profile_id=profile_a, headless=True, restore_session=False)
    ctx_b = await playwright_manager.get_context(profile_id=profile_b, headless=True, restore_session=False)

    # Contexts must be distinct instances
    assert ctx_a is not ctx_b

    # Inject cookie into User A
    cookie_a = {
        "name": "alpha_token",
        "value": "super_secret_alpha_999",
        "domain": ".dice.com",
        "path": "/"
    }
    added = await playwright_manager.add_cookies_safely([cookie_a], profile_id=profile_a)
    assert added == 1

    # Verify User A has the cookie
    cookies_a = await ctx_a.cookies()
    cookie_names_a = [c["name"] for c in cookies_a]
    assert "alpha_token" in cookie_names_a

    # Verify User B does NOT have User A's cookie (strict isolation)
    cookies_b = await ctx_b.cookies()
    cookie_names_b = [c["name"] for c in cookies_b]
    assert "alpha_token" not in cookie_names_b

    # Cleanup both profiles
    await playwright_manager.close_context(profile_a)
    await playwright_manager.close_context(profile_b)

@pytest.mark.anyio
async def test_browser_cleanup():
    """Verify that closing contexts and calling close() safely tears down resources."""
    test_profile = "test_cleanup_user"
    page1 = await playwright_manager.get_new_page(profile_id=test_profile, headless=True)
    page2 = await playwright_manager.get_new_page(profile_id=test_profile, headless=True)

    assert not page1.is_closed()
    assert not page2.is_closed()

    # Close the specific profile
    await playwright_manager.close_context(test_profile)
    assert test_profile not in playwright_manager._contexts
    assert page1.is_closed()
    assert page2.is_closed()

    # Global close
    await playwright_manager.close()
    assert len(playwright_manager._contexts) == 0
    assert playwright_manager.playwright is None

@pytest.mark.anyio
async def test_browser_startup_failure_recovery():
    """Verify that PlaywrightManager recovers from stale lockfiles and gracefully handles errors."""
    test_profile = "test_lockfile_recovery"
    profile_dir = playwright_manager.get_profile_dir(test_profile)

    # Intentionally simulate a stale lockfile left by an abnormal process termination
    stale_lock = profile_dir / "SingletonLock"
    stale_lock.write_text("12345", encoding="utf-8")
    assert stale_lock.exists()

    # Launch context: clean_stale_locks should proactively remove the lock and launch cleanly
    ctx = await playwright_manager.get_context(profile_id=test_profile, headless=True)
    assert ctx is not None

    page = await ctx.new_page()
    await page.goto("about:blank")
    assert not page.is_closed()

    await playwright_manager.close_context(test_profile)
