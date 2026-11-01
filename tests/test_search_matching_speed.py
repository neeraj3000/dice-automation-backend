"""
Search-time matching must not stall automations: new jobs are matched several at a time, a
hung LLM call falls back to the heuristic extractor, and the OpenAI client has a short timeout.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

import pytest

import app.services.jd_service as jd_module
from app.config import settings
from app.database import close_db, connect_db, get_database
from app.schemas.job import JDStructuredData
from app.services.jd_service import jd_service
from app.services.llm_service import LLMService
from app.services.search_service import search_service

USER = "test_search_speed_user"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db():
    await connect_db()
    database = get_database()
    for col in ("search_profiles", "jobs"):
        await database[col].delete_many({"user_id": USER})
    yield database
    for col in ("search_profiles", "jobs"):
        await database[col].delete_many({"user_id": USER})
    await close_db()


def _scraped(n):
    return [{
        "external_job_id": f"speed{i}", "title": f"Engineer {i}", "company": "Acme", "location": "Remote",
        "work_setting": "Remote", "salary": "", "employment_type": "", "posted_date": "", "is_easy_apply": True,
        "job_url": f"https://www.dice.com/job-detail/speed{i}", "application_url": f"https://www.dice.com/job-detail/speed{i}",
        "application_wizard_url": "", "description_raw": "Python", "is_applied": False,
    } for i in range(n)]


@pytest.mark.anyio
async def test_new_jobs_are_matched_concurrently_and_progress_is_reported(db):
    profile = await db.search_profiles.insert_one({"user_id": USER, "name": "Speed", "keywords": ["python"]})
    running, peak = 0, 0

    async def slow_analyze(text, title=""):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.2)
        running -= 1
        return JDStructuredData(role=title)

    progress = []

    async def on_progress(msg):
        progress.append(msg)

    with patch("app.services.search_service.dice_browser.search_jobs", AsyncMock(return_value=_scraped(10))), \
         patch("app.services.search_service.jd_service.analyze_job_description", side_effect=slow_analyze), \
         patch("app.services.search_service.matching_service.match_job_against_all_resumes", AsyncMock()) as match:
        res = await search_service.run_search(
            str(profile.inserted_id), auto_queue=False, user_id=USER, max_results=10, on_progress=on_progress,
        )

    assert res["new_jobs_added"] == 10 and match.await_count == 10
    # Several jobs in flight at once (not one at a time), never more than MATCH_CONCURRENCY
    assert 1 < peak <= 5
    assert progress[0] == "Dice returned 10 result(s). Saving new jobs…"
    assert "Matching new jobs" in progress[1]


@pytest.mark.anyio
async def test_hung_jd_analysis_falls_back_to_heuristics(monkeypatch):
    async def hang(**kw):
        await asyncio.sleep(30)

    client = MagicMock()
    client.beta.chat.completions.parse = hang
    monkeypatch.setattr(jd_module, "JD_ANALYSIS_TIMEOUT_SECONDS", 0.1)
    with patch("app.services.jd_service.llm_service.get_client", return_value=client):
        result = await asyncio.wait_for(
            jd_service.analyze_job_description("Need Python and AWS. 5 years experience", "Data Engineer"), 5
        )
    assert result.role == "Data Engineer"
    assert "Python" in result.required_skills


def test_openai_client_has_short_timeout(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    client = LLMService().get_client()
    assert client.timeout == settings.OPENAI_TIMEOUT_SECONDS == 30.0
    assert client.max_retries == settings.OPENAI_MAX_RETRIES == 1
