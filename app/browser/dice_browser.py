import re
import hashlib
import logging
from typing import List, Dict, Any, Optional
from urllib.parse import quote_plus
from playwright.async_api import Page
from app.browser.playwright_manager import playwright_manager

logger = logging.getLogger(__name__)

class DiceBrowser:
    def _build_dice_url(
        self,
        query: str,
        location: str = "United States",
        is_remote: bool = True,
        work_settings: Optional[List[str]] = None,
        employment_types: Optional[List[str]] = None,
        easy_apply_only: Optional[bool] = True,
        posted_within: str = "ONE",
        radius: int = 30,
        experience_levels: Optional[List[str]] = None,
        page_size: int = 50,
        page: int = 1
    ) -> str:
        # Determine valid Dice pageSize: 20, 50, or 100
        if page_size <= 20:
            effective_page_size = 20
        elif page_size <= 50:
            effective_page_size = 50
        else:
            effective_page_size = 100

        dice_url = (
            f"https://www.dice.com/jobs?q={quote_plus(query)}&location={quote_plus(location)}"
            f"&radius={radius}&radiusUnit=mi&pageSize={effective_page_size}&page={page}"
        )
        
        # 1. Work settings (Dice exact parameter)
        if work_settings:
            dice_url += f"&filters.workplaceTypes={','.join(work_settings)}"
        elif is_remote:
            dice_url += "&filters.workplaceTypes=Remote"

        # 2. Employment type (Dice exact values: FULLTIME, CONTRACTS, THIRD_PARTY, PARTTIME)
        if employment_types:
            dice_url += f"&filters.employmentType={','.join(employment_types)}"

        # 3. Easy apply only (Dice Application Wizard)
        # Only add to URL if easy_apply_only is explicitly True
        if easy_apply_only is True:
            dice_url += "&filters.easyApply=true"

        # 4. Posted date (ONE = Today, THREE = 3 days, SEVEN = 7 days)
        if posted_within in ["ONE", "THREE", "SEVEN"]:
            dice_url += f"&filters.postedDate={posted_within}"
        elif posted_within == "24h":
            dice_url += "&filters.postedDate=ONE"
        elif posted_within == "3d":
            dice_url += "&filters.postedDate=THREE"
        elif posted_within == "7d":
            dice_url += "&filters.postedDate=SEVEN"

        # 5. Experience level
        if experience_levels:
            dice_url += f"&filters.experienceLevel={','.join(experience_levels)}"

        return dice_url

    def _build_query(self, keywords: List[str]) -> str:
        clean_kws = [k.strip() for k in keywords if k and k.strip()]
        if not clean_kws:
            return "Software Engineer"
        if len(clean_kws) == 1:
            return clean_kws[0]

        # Check if multiple multi-word titles/skills exist
        has_phrases = any(" " in k for k in clean_kws)
        if has_phrases:
            # If user provided multiple job titles (e.g. ["Azure Solutions Architect", "Cloud Architect"]),
            # joining with spaces forces Dice to search for all words together resulting in 0 jobs.
            # We format with OR or use primary keyword.
            quoted = [f'"{k}"' if " " in k and not (k.startswith('"') and k.endswith('"')) else k for k in clean_kws[:3]]
            return " OR ".join(quoted)
        else:
            return " ".join(clean_kws)

    async def _extract_cards_from_page(
        self,
        page: Page,
        max_results: int,
        easy_apply_mode: str,
        existing_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        # Mode can be 'EASY_ONLY', 'NON_EASY_ONLY', or 'ALL'
        return await page.evaluate(r'''({ maxRes, mode, prevIds }) => {
            const results = [];
            const seenIds = new Set(prevIds || []);

            // Modern Dice renders cards with data-testid="job-card" or role="article"
            let cardElements = Array.from(document.querySelectorAll('[data-testid="job-card"], div[data-job-guid], [role="article"]'));
            
            // Fallback: If container cards are not found, gather visible title links directly
            if (cardElements.length === 0) {
                const visibleTitleLinks = Array.from(document.querySelectorAll('a[data-testid="job-search-job-detail-link"], a[href*="/job-detail/"]:not([class*="opacity-0"])'));
                cardElements = visibleTitleLinks.map(l => l.closest('[role="article"]') || l.closest('div.flex.flex-col') || l.parentElement?.parentElement?.parentElement || l.parentElement).filter(Boolean);
            }

            for (const card of cardElements) {
                // 1. Determine Title & Link
                const titleEl = card.querySelector('[data-testid="job-search-job-detail-link"]') ||
                                card.querySelector('a[href*="/job-detail/"]:not([class*="opacity-0"])') ||
                                card.querySelector('a[href*="/job-detail/"]');

                let title = titleEl ? titleEl.innerText.trim() : '';
                let href = titleEl ? (titleEl.getAttribute('href') || '') : '';

                if (!href) {
                    const anyLink = card.querySelector('a[href*="/job-detail/"]');
                    if (anyLink) href = anyLink.getAttribute('href') || '';
                }
                if (!href) continue;
                if (href.startsWith('/')) href = 'https://www.dice.com' + href;

                // Extract job ID
                let extId = card.getAttribute('data-job-guid') || '';
                if (!extId) {
                    const idMatch = href.match(/\/job-detail\/([a-zA-Z0-9\-]+)/);
                    if (idMatch) extId = idMatch[1];
                }
                if (!extId || seenIds.has(extId)) continue;

                // If title is still empty, check aria-label on overlay or card
                if (!title) {
                    const ariaEl = card.querySelector('[aria-label*="View Details for"]');
                    const aria = ariaEl ? (ariaEl.getAttribute('aria-label') || '') : '';
                    const ariaMatch = aria.match(/View Details for (.+?)(?:\s*\([a-f0-9\-]+\))?$/i);
                    if (ariaMatch) title = ariaMatch[1].trim();
                }

                // Fallback to title text in card
                const cardText = card.innerText || '';
                const lines = cardText.split('\n').map(l => l.trim()).filter(Boolean);
                if (!title && lines.length > 0 && lines[0].length < 120 && !lines[0].includes('$') && !lines[0].includes('USD')) {
                    title = lines[0];
                }
                if (!title) continue; // Skip cards without a valid title

                // 2. Determine Company
                let company = '';
                const compEl = card.querySelector('[data-testid="job-card-company-name"]') ||
                               card.querySelector('a[href*="/company-profile/"]') ||
                               card.querySelector('a[href*="/company/"]');
                if (compEl && compEl.innerText.trim()) {
                    company = compEl.innerText.trim();
                }
                if (!company && lines.length > 1) {
                    const candidate = lines[1];
                    if (!candidate.includes('•') && !candidate.includes('$') && !candidate.includes('USD') && !candidate.includes('ago') && candidate !== title) {
                        company = candidate;
                    }
                }
                if (!company) company = 'Tech Company';

                // 3. Location & Posted Date
                let loc = 'Remote';
                let postedDate = 'Recently';
                const dateMatch = cardText.match(/(?:Today|Yesterday|\d+\s*d\s+ago|\d+\s+days?\s+ago)/i);
                if (dateMatch) postedDate = dateMatch[0];

                const locLine = lines.find(l => (l.includes('•') || l.includes('Remote') || /^[A-Z][a-zA-Z\s]+,\s*[A-Z]{2}/.test(l)) && !l.includes('$') && !l.includes('USD'));
                if (locLine) {
                    const parts = locLine.split('•').map(p => p.trim());
                    loc = parts[0] || 'Remote';
                    if (parts.length > 1 && !dateMatch) {
                        postedDate = parts[1];
                    }
                }

                // 4. Salary
                const salaryEl = card.querySelector('#salary-label');
                let salary = salaryEl ? salaryEl.innerText.trim() : '';
                if (!salary) {
                    const salaryMatch = cardText.match(/(?:USD\s*|\$)\s*[\d,]+(?:\.\d{2})?(?:\s*-\s*(?:USD\s*|\$)?\s*[\d,]+(?:\.\d{2})?)?\s*(?:per\s+(?:hour|year)|hr|yr)?/i);
                    salary = salaryMatch ? salaryMatch[0].trim() : (cardText.includes('Depends on Experience') ? 'Depends on Experience' : '');
                }

                // 5. Employment Type
                const empEl = card.querySelector('#employmentType-label');
                let empType = empEl ? empEl.innerText.trim() : '';
                if (!empType) {
                    if (cardText.toLowerCase().includes('contract')) empType = 'Contract';
                    else if (cardText.toLowerCase().includes('part-time')) empType = 'Part-time';
                    else empType = 'Full-time';
                }

                // 6. Work Setting
                let workSetting = 'On-Site';
                if (cardText.toLowerCase().includes('remote') || loc.toLowerCase().includes('remote')) workSetting = 'Remote';
                else if (cardText.toLowerCase().includes('hybrid')) workSetting = 'Hybrid';

                // 7. Easy Apply Detection
                const isEasyApply = !!card.querySelector('#easyApply-label') ||
                                    cardText.toLowerCase().includes('easy apply') ||
                                    !!card.querySelector('[data-cy*="easy-apply"], [aria-label*="Easy Apply"]');

                // RESPECT USER'S EASY APPLY PREFERENCE STRICTLY
                if (mode === 'EASY_ONLY' && !isEasyApply) {
                    continue; // Skip non-easy-apply jobs
                }
                if (mode === 'NON_EASY_ONLY' && isEasyApply) {
                    continue; // Skip easy-apply jobs (User explicitly unchecked Easy Apply)
                }

                // 8. Already Applied Detection & Skipping
                // Check if card has the Dice "Applied" tag (e.g. green pill badge with checkmark)
                let isAlreadyApplied = false;

                // A. Explicit attributes or testids
                if (card.querySelector('#applied-label, [data-testid*="applied" i], [data-cy*="applied" i], [aria-label*="Applied" i]')) {
                    isAlreadyApplied = true;
                }

                // B. Pill / badge / chip element containing exact text "Applied" or "✓ Applied"
                if (!isAlreadyApplied) {
                    const candidateBadges = card.querySelectorAll('span, div, button, p, a, [class*="badge"], [class*="pill"], [class*="tag"], [class*="chip"]');
                    for (const el of candidateBadges) {
                        if (el.children.length > 2) continue; // Leaf or near-leaf badge
                        const text = (el.innerText || el.textContent || '').trim();
                        if (/^(?:✓|✔)?\s*applied$/i.test(text)) {
                            isAlreadyApplied = true;
                            break;
                        }
                    }
                }

                // C. Check aria-labels across the card (e.g. "Applied", "You have applied")
                if (!isAlreadyApplied) {
                    const ariaEls = card.querySelectorAll('[aria-label]');
                    for (const el of ariaEls) {
                        const aria = el.getAttribute('aria-label') || '';
                        if (/\bapplied\b/i.test(aria) && !aria.toLowerCase().includes('easy apply') && !aria.toLowerCase().includes('apply now')) {
                            isAlreadyApplied = true;
                            break;
                        }
                    }
                }

                // D. Line-by-line card text check (Dice renders the badge as an isolated line: "Applied")
                if (!isAlreadyApplied) {
                    const cardLines = (card.innerText || '').split('\n').map(l => l.trim().toLowerCase());
                    if (cardLines.some(line => /^(?:✓|✔)?\s*applied$/i.test(line))) {
                        isAlreadyApplied = true;
                    }
                }

                // STRICTLY DO NOT SCRAPE JOBS THAT ARE ALREADY APPLIED IN DICE
                if (isAlreadyApplied) {
                    continue; // Skip already applied jobs completely!
                }

                const wizardUrl = `https://www.dice.com/job-applications/${extId}/wizard`;

                seenIds.add(extId);
                results.push({
                    external_job_id: extId,
                    title: title,
                    company: company,
                    location: loc,
                    work_setting: workSetting,
                    salary: salary,
                    employment_type: empType,
                    posted_date: postedDate,
                    is_easy_apply: isEasyApply,
                    is_applied: isAlreadyApplied,
                    job_url: href,
                    application_url: href,
                    application_wizard_url: wizardUrl,
                    description_raw: `Title: ${title} at ${company}. Location: ${loc} (${workSetting}). Employment Type: ${empType}. Salary: ${salary}. Posted: ${postedDate}. Summary: ${cardText.replace(/\s+/g, ' ').slice(0, 500)}`
                });

                if (results.length >= maxRes) break;
            }
            return results;
        }''', {"maxRes": max_results, "mode": easy_apply_mode, "prevIds": existing_ids or []})

    async def _collect_jobs_from_dice_url(
        self,
        page: Page,
        base_url_fn,
        max_results: int,
        easy_apply_mode: str,
        card_selectors: str,
    ) -> List[Dict[str, Any]]:
        jobs_collected: List[Dict[str, Any]] = []
        seen_ids = set()
        current_dice_page = 1
        # Allow sufficient pages (at least 5, or up to 20) so searches can reach max_results even if many cards are skipped
        max_pages = min(20, max(5, (max_results // 5) + 3))

        # Initial navigation to Page 1
        initial_url = base_url_fn(1)
        logger.info(f"Loading initial Dice search URL: {initial_url}")
        try:
            await page.goto(initial_url, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_selector(card_selectors, timeout=12000)
        except Exception as e:
            logger.info(f"Page 1 load/selector wait status: {e}")

        while len(jobs_collected) < max_results and current_dice_page <= max_pages:
            logger.info(f"Scraping Dice page {current_dice_page} (Collected so far: {len(jobs_collected)} / {max_results})")

            # Check for CAPTCHA or blocking
            content = await page.content()
            if "captcha" in content.lower() or "verify you are human" in content.lower():
                logger.warning("Dice CAPTCHA / Human verification screen detected.")
                break

            # Progressive scroll of the internal job cards container (Dice uses an overflow-y-auto inner element)
            for scroll_step in range(15):
                if len(jobs_collected) >= max_results:
                    break

                needed = max_results - len(jobs_collected)
                newly_extracted = await self._extract_cards_from_page(
                    page,
                    max_results=needed,
                    easy_apply_mode=easy_apply_mode,
                    existing_ids=list(seen_ids)
                )

                if newly_extracted:
                    for j in newly_extracted:
                        if j["external_job_id"] not in seen_ids:
                            seen_ids.add(j["external_job_id"])
                            jobs_collected.append(j)
                            if len(jobs_collected) >= max_results:
                                break

                # Scroll the actual scrollable cards container and bring the last card into view
                reached_bottom = await page.evaluate(r'''() => {
                    const firstCard = document.querySelector('[data-testid="job-card"], div[data-job-guid], [role="article"]');
                    let container = null;
                    let parent = firstCard ? firstCard.parentElement : null;
                    while (parent && parent !== document.body) {
                        if (parent.scrollHeight > parent.clientHeight && parent.clientHeight > 200) {
                            container = parent;
                            break;
                        }
                        parent = parent.parentElement;
                    }
                    if (!container) container = document.querySelector('.overflow-y-auto, [class*="overflow-y-auto"]');
                    if (container) {
                        container.scrollBy({ top: 900, behavior: 'smooth' });
                    }
                    const cards = document.querySelectorAll('[data-testid="job-card"], div[data-job-guid], [role="article"]');
                    if (cards.length > 0) {
                        cards[cards.length - 1].scrollIntoView({ behavior: 'smooth', block: 'end' });
                    }
                    if (container) {
                        return (container.scrollTop + container.clientHeight >= container.scrollHeight - 100);
                    }
                    return false;
                }''')

                await page.wait_for_timeout(800)

                # If reached bottom of current page and cards stopped mounting, proceed to next page
                if reached_bottom and scroll_step >= 3:
                    break

            if len(jobs_collected) >= max_results:
                break

            # Advance to next page via Dice's React-Aria pagination element
            nav_result = await page.evaluate(r'''() => {
                const nextBtn = document.querySelector('nav[aria-label="Pagination"] [aria-label="Next"]:not([aria-disabled="true"]):not([data-disabled="true"]), [data-testid="pagination-next"]');
                if (nextBtn) {
                    nextBtn.click();
                    return { clicked: true };
                }
                return { clicked: false };
            }''')

            if not nav_result.get("clicked"):
                logger.info(f"No further active Next button on Dice page {current_dice_page}. Stopping pagination.")
                break

            # Wait for the next page to mount and reset container scroll to top
            await page.wait_for_timeout(3000)
            await page.evaluate(r'''() => {
                const firstCard = document.querySelector('[data-testid="job-card"], div[data-job-guid], [role="article"]');
                let container = null;
                let parent = firstCard ? firstCard.parentElement : null;
                while (parent && parent !== document.body) {
                    if (parent.scrollHeight > parent.clientHeight && parent.clientHeight > 200) {
                        container = parent;
                        break;
                    }
                    parent = parent.parentElement;
                }
                if (!container) container = document.querySelector('.overflow-y-auto, [class*="overflow-y-auto"]');
                if (container) container.scrollTop = 0;
            }''')
            await page.wait_for_timeout(1000)

            current_dice_page += 1

        return jobs_collected

    async def search_jobs(
        self,
        keywords: List[str],
        location: str = "United States",
        is_remote: bool = True,
        work_settings: Optional[List[str]] = None,
        employment_types: Optional[List[str]] = None,
        easy_apply_only: Optional[bool] = True,
        posted_within: str = "ONE",
        radius: int = 30,
        experience_levels: Optional[List[str]] = None,
        max_results: int = 15
    ) -> List[Dict[str, Any]]:
        page = await playwright_manager.get_new_page()

        clean_kws = [k.strip() for k in keywords if k and k.strip()]
        primary_query = self._build_query(clean_kws)
        first_keyword = clean_kws[0] if clean_kws else "Software Engineer"

        # Determine mode string for JS extractor: 'EASY_ONLY', 'NON_EASY_ONLY', or 'ALL'
        if easy_apply_only is True:
            easy_apply_mode = 'EASY_ONLY'
        elif easy_apply_only is False:
            easy_apply_mode = 'NON_EASY_ONLY'
        else:
            easy_apply_mode = 'ALL'

        card_selectors = '[data-testid="job-card"], div[data-job-guid], [role="article"], a[data-testid="job-search-job-detail-link"], a[href*="/job-detail/"]:not([class*="opacity-0"]), [data-testid="no-jobs-found"], h2:has-text("No jobs found"), [data-testid="search-empty-state"]'

        try:
            logger.info(f"Initiating progressive search on Dice (Target: up to {max_results} jobs, Mode: {easy_apply_mode})")

            # Primary search
            jobs_collected = await self._collect_jobs_from_dice_url(
                page=page,
                base_url_fn=lambda p: self._build_dice_url(
                    query=primary_query,
                    location=location,
                    is_remote=is_remote,
                    work_settings=work_settings,
                    employment_types=employment_types,
                    easy_apply_only=easy_apply_only,
                    posted_within=posted_within,
                    radius=radius,
                    experience_levels=experience_levels,
                    page_size=max_results,
                    page=p
                ),
                max_results=max_results,
                easy_apply_mode=easy_apply_mode,
                card_selectors=card_selectors,
            )

            # --- INTELLIGENT SEARCH FALLBACKS ---
            # Fallback 1: If 0 jobs found and we used a combined OR query, try the primary keyword alone
            if len(jobs_collected) == 0 and primary_query != first_keyword:
                logger.info(f"0 jobs found with combined query '{primary_query}'. Falling back to primary keyword '{first_keyword}'...")
                jobs_collected = await self._collect_jobs_from_dice_url(
                    page=page,
                    base_url_fn=lambda p: self._build_dice_url(
                        query=first_keyword,
                        location=location,
                        is_remote=is_remote,
                        work_settings=work_settings,
                        employment_types=employment_types,
                        easy_apply_only=easy_apply_only,
                        posted_within=posted_within,
                        radius=radius,
                        experience_levels=experience_levels,
                        page_size=max_results,
                        page=p
                    ),
                    max_results=max_results,
                    easy_apply_mode=easy_apply_mode,
                    card_selectors=card_selectors,
                )

            # Fallback 2: If still 0 jobs and posted_within was 24 hours (ONE), widen to 3 days (THREE)
            if len(jobs_collected) == 0 and posted_within in ["ONE", "24h"]:
                logger.info("0 jobs found in last 24h. Widening date filter to 3 days (THREE)...")
                jobs_collected = await self._collect_jobs_from_dice_url(
                    page=page,
                    base_url_fn=lambda p: self._build_dice_url(
                        query=first_keyword,
                        location=location,
                        is_remote=is_remote,
                        work_settings=work_settings,
                        employment_types=employment_types,
                        easy_apply_only=easy_apply_only,
                        posted_within="THREE",
                        radius=radius,
                        experience_levels=experience_levels,
                        page_size=max_results,
                        page=p
                    ),
                    max_results=max_results,
                    easy_apply_mode=easy_apply_mode,
                    card_selectors=card_selectors,
                )

            # Fallback 3: If still 0 jobs, widen date filter to 7 days (SEVEN)
            if len(jobs_collected) == 0 and posted_within in ["ONE", "24h", "THREE", "3d"]:
                logger.info("0 jobs found in last 3 days. Widening date filter to 7 days (SEVEN)...")
                jobs_collected = await self._collect_jobs_from_dice_url(
                    page=page,
                    base_url_fn=lambda p: self._build_dice_url(
                        query=first_keyword,
                        location=location,
                        is_remote=is_remote,
                        work_settings=work_settings,
                        employment_types=employment_types,
                        easy_apply_only=easy_apply_only,
                        posted_within="SEVEN",
                        radius=radius,
                        experience_levels=experience_levels,
                        page_size=max_results,
                        page=p
                    ),
                    max_results=max_results,
                    easy_apply_mode=easy_apply_mode,
                    card_selectors=card_selectors,
                )

            logger.info(f"Successfully scraped {len(jobs_collected)} jobs from Dice (Filter: {easy_apply_mode})")

            # --- USER VIEWING WINDOW (PREVENT PREMATURE TAB CLOSING) ---
            # When the user is watching in non-headless mode, hold the browser open for ~7 seconds
            # so the user can inspect the scraped jobs, view the search results, and observe the automation.
            try:
                from app.services.settings_service import settings_service
                app_settings = await settings_service.get_settings()
                if not app_settings.headless_browser:
                    mode_label = "Easy Apply only" if easy_apply_mode == "EASY_ONLY" else ("Non-Easy Apply only" if easy_apply_mode == "NON_EASY_ONLY" else "All apply types")
                    await page.evaluate(r'''({ count, mode, kws }) => {
                        const existing = document.getElementById('dice-automation-hud');
                        if (existing) existing.remove();

                        const hud = document.createElement('div');
                        hud.id = 'dice-automation-hud';
                        hud.style = `
                            position: fixed;
                            top: 20px;
                            right: 20px;
                            z-index: 9999999;
                            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                            color: #f8fafc;
                            padding: 16px 20px;
                            border-radius: 12px;
                            box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5), 0 8px 10px -6px rgba(0,0,0,0.5);
                            border: 1px solid rgba(56, 189, 248, 0.4);
                            font-family: system-ui, -apple-system, sans-serif;
                            font-size: 13px;
                            min-width: 300px;
                            pointer-events: none;
                        `;
                        const statusColor = count > 0 ? '#22c55e' : '#f59e0b';
                        hud.innerHTML = `
                            <div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#38bdf8;font-size:14px;margin-bottom:6px;">
                                <span style="width:10px;height:10px;border-radius:50%;background:${statusColor};display:inline-block;"></span>
                                Dice Automation Active
                            </div>
                            <div style="color:#e2e8f0;margin-bottom:4px;">
                                Scraped <strong>${count}</strong> jobs (${mode})
                            </div>
                            <div style="font-size:12px;color:#94a3b8;margin-bottom:8px;">
                                Query: "${kws}"
                            </div>
                            <div id="dice-hud-countdown" style="font-size:12px;font-weight:600;color:#38bdf8;border-top:1px solid rgba(255,255,255,0.1);padding-top:6px;">
                                Retaining tab for review (closing in 7s)...
                            </div>
                        `;
                        document.body.appendChild(hud);

                        let remaining = 7;
                        const timer = setInterval(() => {
                            remaining--;
                            const timerEl = document.getElementById('dice-hud-countdown');
                            if (timerEl) {
                                if (remaining > 0) {
                                    timerEl.innerText = `Retaining tab for review (closing in ${remaining}s)...`;
                                } else {
                                    timerEl.innerText = 'Closing tab now...';
                                    clearInterval(timer);
                                }
                            }
                        }, 1000);
                    }''', {"count": len(jobs_collected), "mode": mode_label, "kws": primary_query})

                    # Smooth scroll down then up so user can see cards rendered on screen
                    await page.evaluate("window.scrollBy({ top: 500, behavior: 'smooth' })")
                    await page.wait_for_timeout(3500)
                    await page.evaluate("window.scrollBy({ top: -300, behavior: 'smooth' })")
                    await page.wait_for_timeout(3500)
            except Exception as e:
                logger.debug(f"Viewing window error: {e}")

            return jobs_collected
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def fetch_full_job_description(self, job_url: str) -> str:
        if not job_url or not job_url.startswith("http"):
            return ""
        page = await playwright_manager.get_new_page()
        try:
            await page.goto(job_url, wait_until="domcontentloaded", timeout=25000)
            await page.wait_for_timeout(2000)
            # Dice description container selector
            desc_elem = page.locator("#jobDescription, [data-cy='jobDescriptionText'], .job-description").first
            if await desc_elem.count():
                return (await desc_elem.inner_text()).strip()
            # Fallback to page text
            body_text = await page.locator("body").inner_text()
            return body_text[:4000]
        except Exception as e:
            logger.warning(f"Failed to fetch JD from {job_url}: {e}")
            return ""
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def debug_dom(self) -> Dict[str, Any]:
        page = await playwright_manager.get_new_page()
        test_url = "https://www.dice.com/jobs?q=Python&location=Remote&radius=30&radiusUnit=mi"
        try:
            try:
                await page.goto(test_url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(4000)
            except Exception as e:
                logger.error(f"Debug nav error: {e}")

            info = await page.evaluate(r'''() => {
                const rawAnchors = Array.from(document.querySelectorAll('a[href*="/job-detail/"]'));
                const titleLinks = rawAnchors.filter(a => a.innerText && a.innerText.trim().length > 0 && !a.className.includes('opacity-0'));
                
                const extracted = titleLinks.slice(0, 5).map(titleEl => {
                    const title = titleEl.innerText.trim();
                    const href = titleEl.getAttribute('href') || '';
                    const card = titleEl.closest('[class*="bg-surface"]') || titleEl.parentElement?.parentElement?.parentElement || titleEl.parentElement;
                    const cardText = card ? card.innerText : '';
                    const links = card ? Array.from(card.querySelectorAll('a')).map(l => ({ text: l.innerText.trim(), href: l.getAttribute('href') })) : [];
                    
                    return {
                        title: title,
                        href: href,
                        cardText: cardText,
                        links: links
                    };
                });

                return {
                    currentUrl: window.location.href,
                    pageTitle: document.title,
                    totalJobLinks: rawAnchors.length,
                    uniqueTitleLinks: titleLinks.length,
                    extractedSample: extracted
                };
            }''')
            return info
        finally:
            try:
                await page.close()
            except Exception:
                pass

dice_browser = DiceBrowser()

