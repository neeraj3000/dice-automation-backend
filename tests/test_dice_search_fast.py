"""
Automation searches (fast=True) ask Dice for Easy Apply jobs only, skip the per-keyword
re-searches and the viewing window; an unticked "Easy Apply only" means every apply type.
The apply flow only follows popups its own job page opened.
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest

from app.browser.application_browser import ApplicationBrowser
from app.browser.dice_browser import dice_browser

KEYWORDS = ["AWS Solutions Architect", "EC2", "Terraform"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _search(found, **kw):
    """Runs search_jobs with Dice faked; returns (urls loaded, card modes used, page)."""
    urls, modes = [], []

    async def collect(page, base_url_fn, max_results, easy_apply_mode, card_selectors, existing_ids=None):
        urls.append(base_url_fn(1))
        modes.append(easy_apply_mode)
        return list(found)

    page = MagicMock()
    page.close = AsyncMock()
    page.evaluate = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    with patch("app.browser.dice_browser.playwright_manager.get_new_page", AsyncMock(return_value=page)), \
         patch("app.browser.dice_browser.playwright_manager.is_context_headless", return_value=False), \
         patch.object(dice_browser, "_collect_jobs_from_dice_url", side_effect=collect):
        await dice_browser.search_jobs(keywords=KEYWORDS, posted_within="THREE", max_results=20, **kw)
    return urls, modes, page


@pytest.mark.anyio
async def test_fast_search_filters_easy_apply_on_dice_and_skips_keyword_researches():
    one_job = [{"external_job_id": "j1"}]
    urls, modes, page = await _search(one_job, easy_apply_only=False, fast=True)
    assert len(urls) == 1  # no per-keyword expansion
    assert "filters.easyApply=true" in urls[0]
    assert " OR " in urls[0].replace("+", " ")
    assert modes == ["ALL"]  # Dice already filtered; card markup can't drop results
    page.wait_for_timeout.assert_not_awaited()  # no viewing window


@pytest.mark.anyio
async def test_manual_search_keeps_keyword_expansion():
    urls, modes, _ = await _search([], easy_apply_only=True)
    # Primary query, 7-day widening, then each keyword on its own
    assert len(urls) == 2 + len(KEYWORDS)
    assert modes[0] == "EASY_ONLY"


@pytest.mark.anyio
async def test_unticked_easy_apply_means_all_apply_types():
    urls, modes, _ = await _search([{"external_job_id": "j1"}] * 20, easy_apply_only=False)
    assert "filters.easyApply" not in urls[0]
    assert modes == ["ALL"]


def _page(opener=None):
    p = MagicMock()
    p.is_closed.return_value = False
    p.opener = AsyncMock(return_value=opener)
    return p


@pytest.mark.anyio
async def test_apply_flow_only_follows_its_own_popup():
    job_page = _page()
    search_tab = _page(opener=None)
    context = MagicMock()
    job_page.context = context

    context.pages = [job_page, search_tab]
    assert await ApplicationBrowser._own_popup(job_page) is None

    popup = _page(opener=job_page)
    context.pages = [job_page, popup, search_tab]
    assert await ApplicationBrowser._own_popup(job_page) is popup
