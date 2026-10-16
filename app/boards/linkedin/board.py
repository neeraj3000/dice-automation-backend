"""LinkedIn board integration descriptor.
Adheres strictly to LinkedIn Terms of Service: automated DOM scraping, cookie extraction,
and automated application bots are prohibited. This board operates under the safe,
user-confirmed application tracking workflow.
"""
from pathlib import Path
from playwright.async_api import BrowserContext

from app.boards.base import (
    JobBoard,
    SearchCriteria,
    CountProgress,
    Progress,
    ApplyResult,
    BoardError,
)


class LinkedInBoard(JobBoard):
    key = "linkedin"
    name = "LinkedIn"
    base_url = "https://www.linkedin.com"
    login_url = "https://www.linkedin.com/login"
    cookie_domain = "linkedin.com"
    auth_cookie_names = ()

    def is_login_url(self, url: str) -> bool:
        return "linkedin.com/login" in url or "linkedin.com/uas/login" in url

    async def verify_login(self, ctx: BrowserContext) -> bool:
        """Automated cookie checking is prohibited; user manages LinkedIn session in their browser."""
        return True

    async def search(
        self,
        ctx: BrowserContext,
        criteria: SearchCriteria,
        progress: CountProgress,
        interactive: bool
    ) -> list[dict]:
        raise BoardError(
            "Automated scraping of LinkedIn is disabled per platform policy. "
            "Please discover jobs on LinkedIn and record applications via Apply2Hire user confirmation."
        )

    async def fetch_description(self, ctx: BrowserContext, job: dict) -> str:
        raise BoardError(
            "Automated scraping of LinkedIn job descriptions is disabled per platform policy."
        )

    async def apply(
        self,
        ctx: BrowserContext,
        job: dict,
        resume_path: Path,
        profile: dict,
        answers: dict[str, str],
        mode: str,
        progress: Progress,
        interactive: bool,
    ) -> ApplyResult:
        raise BoardError(
            "Automated bot submission to LinkedIn is prohibited. "
            "Apply directly through LinkedIn and confirm application outcome in Apply2Hire."
        )
