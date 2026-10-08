import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.async_api import BrowserContext, Page
from playwright.async_api import TimeoutError as PWTimeout

from app.boards.base import (
    ApplyResult,
    BoardAuthError,
    BoardChallengeError,
    BoardError,
    CountProgress,
    JobBoard,
    Progress,
    SearchCriteria,
)
from app.boards.dice.rules import PERSONAL_FIELDS, resolve_answer
from app.browser import dice_scripts as S

log = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_PAGES = 30
POSTED = {"today": "ONE", "3d": "THREE", "7d": "SEVEN"}
WIDEN = {"today": ["today", "3d", "7d"], "3d": ["3d", "7d"], "7d": ["7d"], "any": ["any"]}
CONFIRM_RE = re.compile(
    r"application (has been )?(submitted|sent)|thank you for applying|successfully applied|you.ve applied",
    re.I,
)
CHALLENGE_RE = re.compile(r"verify (that )?you are (a )?human|are you a robot|captcha|unusual traffic", re.I)
FIELDS = "input:not([type=file]):not([type=radio]):not([type=checkbox]):not([type=hidden]):not([type=submit]):not([type=button]), textarea"


class DiceBoard(JobBoard):
    key = "dice"
    name = "Dice"
    base_url = "https://www.dice.com"
    login_url = "https://www.dice.com/dashboard/login"
    cookie_domain = "dice.com"
    auth_cookie_names = (
        "identity",
        "refreshtoken",
        "cms_cookie",
        "candidate_id",
        "peopleid",
        "dli",
        "dice_member_id",
        "dice_session",
        "dice-user-id",
        "_oauth2_proxy",
    )

    def is_login_url(self, url: str) -> bool:
        return "/login" in url or "/dashboard/login" in url

    async def _page(self, ctx: BrowserContext) -> Page:
        page = await ctx.new_page()
        page.set_default_timeout(15000)
        return page

    async def _guard_challenge(self, page: Page, interactive: bool) -> None:
        async def blocked() -> bool:
            try:
                return bool(CHALLENGE_RE.search(await page.inner_text("body", timeout=3000)))
            except Exception:
                return False

        if not await blocked():
            return
        if interactive:
            for _ in range(60):
                await asyncio.sleep(2)
                if not await blocked():
                    return
        raise BoardChallengeError("Dice asked for human verification. Turn off headless mode in Settings, solve it once, then retry.")

    @staticmethod
    def _is_dice(url: str) -> bool:
        host = urlparse(url).hostname or ""
        return host == "dice.com" or host.endswith(".dice.com")

    async def verify_login(self, ctx: BrowserContext) -> bool:
        page = await self._page(ctx)
        try:
            await page.goto(f"{self.base_url}/dashboard", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            if self.is_login_url(page.url):
                return False
            names = {c["name"] for c in await ctx.cookies(self.base_url)}
            return bool(names & set(self.auth_cookie_names))
        finally:
            await page.close()

    def _build_url(self, c: SearchCriteria, posted: str, keywords: str) -> str:
        page_size = 20 if c.max_results <= 20 else 50 if c.max_results <= 50 else 100
        params = {"q": keywords, "page": 1, "pageSize": page_size, "language": "en"}
        if c.location:
            params.update({"location": c.location, "radius": 30, "radiusUnit": "mi"})
        if c.work_settings:
            params["filters.workplaceTypes"] = "|".join(c.work_settings)
        if c.employment_types:
            params["filters.employmentType"] = "|".join(c.employment_types)
        if c.easy_apply_only:
            params["filters.easyApply"] = "true"
        if posted in POSTED:
            params["filters.postedDate"] = POSTED[posted]
        return f"{self.base_url}/jobs?{urlencode(params)}"

    async def search(self, ctx: BrowserContext, c: SearchCriteria, progress: CountProgress, interactive: bool) -> list[dict]:
        page = await self._page(ctx)
        try:
            keyword_variants = [c.keywords]
            first = re.split(r"[,/]| or ", c.keywords, flags=re.I)[0].strip()
            if first and first != c.keywords:
                keyword_variants.append(first)
            for kw in keyword_variants:
                for posted in WIDEN.get(c.posted_within, ["3d", "7d"]):
                    jobs = await self._run_query(page, c, posted, kw, progress, interactive)
                    if jobs:
                        return jobs
            return []
        finally:
            await page.close()

    async def _run_query(self, page: Page, c: SearchCriteria, posted: str, kw: str, progress: CountProgress, interactive: bool) -> list[dict]:
        await page.goto(self._build_url(c, posted, kw), wait_until="domcontentloaded", timeout=45000)
        await self._guard_challenge(page, interactive)
        found_cards = False
        for _ in range(16):
            has_cards = await page.evaluate(
                f"() => document.querySelectorAll('{S.CARD_SELECTOR}').length > 0"
            )
            if has_cards:
                found_cards = True
                break
            no_results = await page.evaluate(
                "() => /0 results found|no results found|weren't able to find any jobs/i.test(document.body ? document.body.innerText : '')"
            )
            if no_results:
                return []
            await asyncio.sleep(0.5)

        if not found_cards:
            return []
        collected: dict[str, dict] = {}
        for _ in range(MAX_PAGES):
            idle = 0
            for _ in range(8):
                before = len(collected)
                for j in await page.evaluate(S.EXTRACT_JS):
                    if j.get("is_applied") or not j.get("external_job_id") or not j.get("title"):
                        continue
                    collected.setdefault(j["external_job_id"], j)
                await progress(len(collected))
                if len(collected) >= c.max_results:
                    return self._finalize(list(collected.values())[: c.max_results])
                idle = idle + 1 if len(collected) == before else 0
                if idle >= 3:
                    break
                await page.evaluate(S.SCROLL_JS)
                await page.wait_for_timeout(500)
            if not await page.evaluate(S.NEXT_PAGE_JS):
                break
            await page.wait_for_timeout(1500)
        return self._finalize(list(collected.values()))

    def _finalize(self, jobs: list[dict]) -> list[dict]:
        for j in jobs:
            j.pop("is_applied", None)
            j["apply_url"] = f"{self.base_url}/job-applications/{j['external_job_id']}/wizard"
            if not j.get("url"):
                j["url"] = f"{self.base_url}/job-detail/{j['external_job_id']}"
        return jobs

    async def fetch_description(self, ctx: BrowserContext, job: dict) -> str:
        page = await self._page(ctx)
        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=40000)
            for sel in ("#jobDescription", '[data-testid="jobDescriptionHtml"]', "main"):
                try:
                    return (await page.inner_text(sel, timeout=8000))[:20000]
                except Exception:
                    continue
            return ""
        finally:
            await page.close()

    async def apply(self, ctx: BrowserContext, job: dict, resume_path: Path, profile: dict, answers: dict[str, str],
                    mode: str, progress: Progress, interactive: bool) -> ApplyResult:
        page = await self._page(ctx)
        try:
            await progress("Opening the application")
            await page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)
            if self.is_login_url(page.url):
                raise BoardAuthError("Your Dice session has expired. Reconnect your account and try again.")
            await self._guard_challenge(page, interactive)

            if "/wizard" not in page.url:
                page = await self._open_wizard_from_job_page(ctx, page, job)
            if not self._is_dice(page.url):
                return ApplyResult("REVIEW", "This job applies on the employer's website.", external_url=page.url)

            for step in range(1, MAX_STEPS + 1):
                await progress(f"Step {step}: filling your details")
                if await self._is_confirmation(page):
                    return ApplyResult("APPLIED", "Application submitted.")
                await self._fill_personal(page, profile)
                await self._attach_resume(page, resume_path)
                await self._clear_cover_letter(page)
                await self._answer_questions(page, profile, answers)

                button = await self._primary_button(page)
                if not button:
                    break
                locator, label = button
                if "submit" in label.lower():
                    missing = await page.evaluate(S.UNANSWERED_JS)
                    if missing:
                        return ApplyResult("REVIEW", "A few questions need your answer.", unanswered=missing)
                    if mode == "PREPARE":
                        return ApplyResult("READY", "Ready to submit. Everything is filled in.")
                    await progress("Submitting")
                    await locator.click()
                    return await self._await_confirmation(page)
                await locator.click()
                await page.wait_for_timeout(1500)
                if not self._is_dice(page.url):
                    return ApplyResult("REVIEW", "This job continues on the employer's website.", external_url=page.url)
                missing = await page.evaluate(S.UNANSWERED_JS)
                if missing:
                    return ApplyResult("REVIEW", "A few questions need your answer.", unanswered=missing)
            return ApplyResult("FAILED", "Couldn't reach the final step of the application.")
        finally:
            await page.close()

    async def _open_wizard_from_job_page(self, ctx: BrowserContext, page: Page, job: dict) -> Page:
        btn = page.get_by_role("button", name=re.compile(r"^(easy apply|apply now|apply)$", re.I)).first
        if not await btn.count():
            btn = page.get_by_role("link", name=re.compile(r"^(easy apply|apply now|apply)$", re.I)).first
        if not await btn.count():
            raise BoardError("No Apply button was found. The job may be closed or you may have already applied.")
        try:
            async with ctx.expect_page(timeout=4000) as popup:
                await btn.click()
            new_page = await popup.value
            await new_page.wait_for_load_state("domcontentloaded")
            return new_page
        except PWTimeout:
            await page.wait_for_timeout(1500)
            return page

    async def _is_confirmation(self, page: Page) -> bool:
        if re.search(r"success|confirmation", page.url, re.I):
            return True
        try:
            return bool(CONFIRM_RE.search(await page.inner_text("body", timeout=2500)))
        except Exception:
            return False

    async def _await_confirmation(self, page: Page) -> ApplyResult:
        for _ in range(10):
            await page.wait_for_timeout(1000)
            if await self._is_confirmation(page):
                return ApplyResult("APPLIED", "Application submitted.")
        missing = await page.evaluate(S.UNANSWERED_JS)
        if missing:
            return ApplyResult("REVIEW", "A few questions need your answer.", unanswered=missing)
        return ApplyResult("FAILED", "We clicked Submit but couldn't confirm it went through. Check Dice before retrying.")

    async def _primary_button(self, page: Page):
        for pattern in (r"^submit( application)?$", r"^(next|continue)$"):
            loc = page.get_by_role("button", name=re.compile(pattern, re.I))
            for i in range(await loc.count()):
                b = loc.nth(i)
                if await b.is_visible() and await b.is_enabled():
                    return b, (await b.inner_text()).strip()
        return None

    async def _fill_combobox_or_input(self, page: Page, el, value: str) -> None:
        role = await el.get_attribute("role") or ""
        name = await el.get_attribute("name") or ""
        autocomplete = await el.get_attribute("aria-autocomplete") or ""
        is_combo = role == "combobox" or name == "candidateLocation" or autocomplete == "list"

        if not is_combo:
            await el.fill(value)
            return

        try:
            await el.click()
            await el.fill("")
            await el.press_sequentially(value, delay=35)
            await page.wait_for_timeout(800)

            options = page.locator('[role="option"], [role="listbox"] li, .pac-item')
            count = await options.count()
            if count > 0:
                matched = False
                for j in range(min(count, 5)):
                    opt = options.nth(j)
                    text = (await opt.inner_text()).strip()
                    if value.lower() in text.lower():
                        await opt.click()
                        matched = True
                        break
                if not matched:
                    await options.first.click()
            else:
                await el.press("Enter")
            await page.wait_for_timeout(400)
        except Exception as e:
            log.warning("Combobox selection failed, fallback to fill: %s", e)
            await el.fill(value)

    async def _fill_personal(self, page: Page, profile: dict) -> None:
        inputs = page.locator(FIELDS)
        for i in range(min(await inputs.count(), 40)):
            el = inputs.nth(i)
            try:
                if not await el.is_visible():
                    continue
                val = await el.input_value()
                is_invalid = await el.get_attribute("aria-invalid") == "true"
                if val and not is_invalid:
                    continue
                desc = await el.evaluate(S.DESCRIPTOR_JS)
                for pattern, key in PERSONAL_FIELDS:
                    if re.search(pattern, desc, re.I) and profile.get(key):
                        await self._fill_combobox_or_input(page, el, str(profile[key]))
                        break
            except Exception:
                continue

    async def _attach_resume(self, page: Page, path: Path) -> None:
        if await page.get_by_text(path.name, exact=False).count():
            return
        options = page.locator('button[aria-label="File options"]')
        for i in range(await options.count()):
            btn = options.nth(i)
            try:
                if await btn.evaluate(S.BUTTON_IN_COVER_JS):
                    continue
                await btn.click()
                item = page.locator('[role="menuitem"][data-key="replace"]').first
                async with page.expect_file_chooser(timeout=5000) as fc:
                    await item.click()
                chooser = await fc.value
                await chooser.set_files(str(path))
                await page.wait_for_timeout(1500)
                return
            except Exception as e:
                log.info("File-options replace failed, trying input[type=file]: %s", e)
        inputs = page.locator('input[type="file"]')
        for i in range(await inputs.count()):
            el = inputs.nth(i)
            meta = await el.evaluate("e => [e.id, e.name, e.getAttribute('aria-label')].join(' ').toLowerCase()")
            if "cover" in meta:
                continue
            await el.set_input_files(str(path))
            await page.wait_for_timeout(1500)
            return

    async def _clear_cover_letter(self, page: Page) -> None:
        try:
            if await page.evaluate(S.CLEAR_COVER_JS):
                await page.wait_for_timeout(400)
                confirm = page.get_by_role("button", name=re.compile(r"^(delete|remove|confirm)$", re.I)).first
                if await confirm.count():
                    await confirm.click(timeout=1500)
        except Exception:
            pass

    async def _answer_questions(self, page: Page, profile: dict, saved: dict[str, str]) -> None:
        sets = page.locator("fieldset")
        for i in range(min(await sets.count(), 25)):
            fs = sets.nth(i)
            try:
                if await fs.locator("input[type=radio]:checked").count() or not await fs.locator("input[type=radio]").count():
                    continue
                q = await fs.evaluate("el => (el.querySelector('legend')?.innerText || el.getAttribute('aria-label') || el.innerText.split('\\n')[0] || '')")
                ans = resolve_answer(q, profile, saved)
                if ans:
                    await fs.locator("label", has_text=re.compile(rf"^\s*{re.escape(ans)}\s*$", re.I)).first.click(timeout=2000)
            except Exception:
                continue
        selects = page.locator("select")
        for i in range(min(await selects.count(), 15)):
            el = selects.nth(i)
            try:
                if not await el.is_visible():
                    continue
                desc = await el.evaluate(S.DESCRIPTOR_JS)
                ans = resolve_answer(desc, profile, saved)
                if ans:
                    try:
                        await el.select_option(label=ans, timeout=2000)
                    except Exception:
                        opts = await el.locator("option").all_inner_texts()
                        for opt_text in opts:
                            if ans.lower() in opt_text.lower():
                                await el.select_option(label=opt_text.strip(), timeout=2000)
                                break
            except Exception:
                continue
        inputs = page.locator(FIELDS)
        for i in range(min(await inputs.count(), 40)):
            el = inputs.nth(i)
            try:
                if not await el.is_visible():
                    continue
                val = await el.input_value()
                is_invalid = await el.get_attribute("aria-invalid") == "true"
                if val and not is_invalid:
                    continue
                ans = resolve_answer(await el.evaluate(S.DESCRIPTOR_JS), profile, saved)
                if ans:
                    await self._fill_combobox_or_input(page, el, ans)
            except Exception:
                continue
