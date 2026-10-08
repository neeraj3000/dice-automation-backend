"""Job-board plugin contract. Add a new board by subclassing JobBoard and registering it."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from playwright.async_api import BrowserContext

Progress = Callable[[str], Awaitable[None]]
CountProgress = Callable[[int], Awaitable[None]]


class BoardError(Exception):
    """Expected, user-facing failure."""


class BoardAuthError(BoardError):
    """Session missing/expired - user must reconnect."""


class BoardChallengeError(BoardError):
    """Captcha / human verification."""


@dataclass
class SearchCriteria:
    keywords: str
    location: str = ""
    work_settings: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    easy_apply_only: bool = True
    posted_within: str = "3d"
    max_results: int = 20


@dataclass
class ApplyResult:
    status: str  # READY | REVIEW | APPLIED | FAILED
    message: str = ""
    unanswered: list[dict] = field(default_factory=list)
    external_url: str | None = None


class JobBoard(ABC):
    key: str
    name: str
    base_url: str
    login_url: str
    cookie_domain: str
    auth_cookie_names: tuple[str, ...] = ()

    @abstractmethod
    def is_login_url(self, url: str) -> bool: ...

    @abstractmethod
    async def search(self, ctx: BrowserContext, criteria: SearchCriteria, progress: CountProgress, interactive: bool) -> list[dict]: ...

    @abstractmethod
    async def fetch_description(self, ctx: BrowserContext, job: dict) -> str: ...

    @abstractmethod
    async def apply(
        self, ctx: BrowserContext, job: dict, resume_path: Path, profile: dict, answers: dict[str, str],
        mode: str, progress: Progress, interactive: bool,
    ) -> ApplyResult: ...

    @abstractmethod
    async def verify_login(self, ctx: BrowserContext) -> bool: ...
