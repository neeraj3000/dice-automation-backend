import re
import os
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from app.config import settings
from app.browser.playwright_manager import playwright_manager
from app.schemas.user_profile import UserProfileSchema

logger = logging.getLogger(__name__)

class ApplicationBrowser:
    async def prepare_application(
        self,
        application_url: str,
        resume_file_path: str,
        user_profile: UserProfileSchema,
        mode: str = "PREPARE"
    ) -> Dict[str, Any]:
        """
        Automates navigating to the Dice job application, filling personal info,
        selecting or uploading the target resume, answering standard screener questions,
        and either pausing at the final review step (PREPARE) or submitting (APPLY).
        """
        page = None
        progress_steps = ["Job application initialized"]
        unanswered_questions: List[Dict[str, Any]] = []

        try:
            page = await playwright_manager.get_new_page()
            target_url = (application_url or "").strip()
            if not target_url or not target_url.startswith("http"):
                logger.info(f"No valid HTTP URL provided: '{target_url}'. Skipping browser navigation.")
                progress_steps.append("No external job URL provided (direct matched JD). Application marked ready.")
                return {
                    "success": True,
                    "status": "READY" if mode == "PREPARE" else "APPLIED",
                    "progress_steps": progress_steps,
                    "unanswered_questions": [],
                    "failure_reason": None
                }

            logger.info(f"Navigating to Dice Application: {target_url}")
            progress_steps.append(f"Navigating to job page: {target_url}")

            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(3000)
            except Exception as e:
                logger.error(f"Failed to load URL {target_url}: {e}")
                raise e

            # Check for Dice login prompt first
            content = await page.content()
            if "sign in" in content.lower() and ("password" in content.lower() or "login" in content.lower()):
                login_btn = page.locator('button:has-text("Sign In"), input[type="password"]').first
                if await login_btn.count() and await login_btn.is_visible():
                    progress_steps.append("Dice login required")
                    return {
                        "success": False,
                        "status": "REVIEW",
                        "progress_steps": progress_steps,
                        "failure_reason": "Dice login required. Please log into your Dice account in the browser.",
                        "unanswered_questions": [{
                            "question_text": "Please sign in to Dice in the opened browser window to continue your application.",
                            "field_name": "login_required",
                            "options": ["Done, Continue"],
                            "is_login_prompt": True
                        }]
                    }

            # Check if we landed on a job detail page vs already on the wizard
            is_wizard = "/wizard" in page.url.lower()

            if not is_wizard:
                # Find active Apply button on job detail page
                apply_btn = page.locator(
                    'a[data-testid="apply-button"], button[data-testid="apply-button"], '
                    'button[data-cy="apply-button"], apply-button-wc button, '
                    'a:has-text("Apply Now"), button:has-text("Apply Now"), '
                    'a:has-text("Easy Apply"), button:has-text("Easy Apply"), '
                    'button:has-text("Apply with profile"), a:has-text("Apply"), button:has-text("Apply")'
                ).first

                has_apply_btn = await apply_btn.count() > 0 and await apply_btn.is_visible()

                # Check if job was truly already applied to (ONLY if NO apply button exists AND explicit applied badge exists)
                if not has_apply_btn:
                    applied_badge = page.locator(
                        '#applied-label, [data-testid="applied-button"], button:has-text("Applied")[disabled], '
                        'span[data-testid*="applied"], div[data-testid*="applied"], [data-testid*="applied-badge"]'
                    ).first
                    if await applied_badge.count() and await applied_badge.is_visible():
                        progress_steps.append("Job is verified as already applied on Dice")
                        return {
                            "success": True,
                            "status": "APPLIED",
                            "progress_steps": progress_steps,
                            "unanswered_questions": [],
                            "failure_reason": None
                        }

                if has_apply_btn:
                    btn_text = (await apply_btn.inner_text()).strip()
                    progress_steps.append(f"Clicked '{btn_text}' on Dice job page")

                    target_attr = await apply_btn.get_attribute("target") or ""
                    try:
                        if target_attr == "_blank":
                            async with page.context.expect_page(timeout=5000) as new_page_info:
                                await apply_btn.click()
                            page = await new_page_info.value
                            await page.wait_for_load_state("domcontentloaded", timeout=20000)
                            progress_steps.append("Switched to application wizard tab")
                        else:
                            await apply_btn.click()
                            try:
                                await page.wait_for_url("**/wizard**", timeout=10000)
                            except Exception:
                                await page.wait_for_timeout(3000)
                    except Exception as ex:
                        logger.warning(f"Apply button transition notice: {ex}")
                        await page.wait_for_timeout(3000)

                # Track active page if popup opened
                if len(page.context.pages) > 1:
                    page = page.context.pages[-1]

                # Check if application redirects to external company website
                curr_url = page.url.lower()
                if "dice.com" not in curr_url:
                    progress_steps.append(f"External employer application site detected: {page.url}")
                    return {
                        "success": True,
                        "status": "REVIEW",
                        "progress_steps": progress_steps,
                        "failure_reason": f"External application site ({page.url}). Please complete manually.",
                        "unanswered_questions": [{
                            "question_text": "This job requires completing the application on the employer's external website.",
                            "field_name": "external_site",
                            "options": ["Complete Manually"]
                        }]
                    }

            # Resolve full resume path
            full_resume_path = settings.RESUMES_DIR / Path(resume_file_path).name
            if not full_resume_path.exists():
                full_resume_path = settings.RESUMES_DIR.parent / resume_file_path

            # Multi-Step Wizard Loop (support up to 6 steps)
            max_steps = 6
            current_step = 0

            while current_step < max_steps:
                current_step += 1
                progress_steps.append(f"Processing wizard step {current_step}")
                await page.wait_for_timeout(1500)

                # A. Check for Final Review & Submit Button FIRST
                submit_btn = page.locator('button').filter(has_text=re.compile(r"^Submit$|^Submit Application$", re.I)).first

                if await submit_btn.count() and await submit_btn.is_visible():
                    progress_steps.append("Reached final review step on Dice")
                    if mode == "PREPARE":
                        progress_steps.append("PREPARE Mode: Automation paused at final review step for user verification")
                        return {
                            "success": True,
                            "status": "READY",
                            "progress_steps": progress_steps,
                            "unanswered_questions": [],
                            "failure_reason": None
                        }
                    else:
                        # APPLY Mode: Execute submission
                        progress_steps.append("Clicking 'Submit' on Dice...")
                        await submit_btn.click()
                        await page.wait_for_timeout(5000)

                        # Verify submission confirmation
                        is_confirmed = await self._verify_submission_confirmation(page)
                        if is_confirmed:
                            progress_steps.append("Verified application submission confirmation on Dice!")
                            return {
                                "success": True,
                                "status": "APPLIED",
                                "progress_steps": progress_steps,
                                "unanswered_questions": [],
                                "failure_reason": None
                            }
                        else:
                            # Check if submission was blocked by validation errors
                            blocking_errors = await self._scan_validation_errors(page)
                            if blocking_errors:
                                unanswered_questions.extend(blocking_errors)
                                progress_steps.append(f"Submission paused: {len(blocking_errors)} required field(s) need attention")
                                return {
                                    "success": True,
                                    "status": "REVIEW",
                                    "progress_steps": progress_steps,
                                    "unanswered_questions": unanswered_questions[:5],
                                    "failure_reason": None
                                }

                            # Double-check confirmation URL
                            curr_url = page.url.lower()
                            if any(k in curr_url for k in ["/applied", "/confirmation", "status=applied", "/success"]):
                                progress_steps.append("Verified application submission via confirmation URL on Dice!")
                                return {
                                    "success": True,
                                    "status": "APPLIED",
                                    "progress_steps": progress_steps,
                                    "unanswered_questions": [],
                                    "failure_reason": None
                                }

                            # If neither confirmed nor errored, return REVIEW rather than false APPLIED
                            progress_steps.append("Submit button was clicked; awaiting confirmation verification")
                            return {
                                "success": False,
                                "status": "REVIEW",
                                "progress_steps": progress_steps,
                                "unanswered_questions": [],
                                "failure_reason": "Application was submitted but Dice confirmation could not be verified. Please check your Dice account."
                            }

                # B. Fill Personal Info
                filled = await self._fill_personal_info(page, user_profile)
                if filled:
                    progress_steps.append(f"Populated contact fields: {', '.join(filled)}")

                # C. Select or Upload Resume
                if full_resume_path.exists():
                    uploaded = await self._handle_resume_selection(page, full_resume_path)
                    if uploaded:
                        progress_steps.append(f"Selected resume: {full_resume_path.name}")
                    await self._clear_cover_letter_field(page)

                # D. Answer Standard Screener Questions
                answered_count = await self._answer_screener_questions(page, user_profile)
                if answered_count > 0:
                    progress_steps.append(f"Auto-answered {answered_count} screener questions")

                # E. Advance to Next Step
                next_btn = page.locator('button').filter(has_text=re.compile(r"^Next$|^Continue$|^Next Step$|^Review Application$", re.I)).first

                if await next_btn.count() and await next_btn.is_visible() and await next_btn.is_enabled():
                    btn_text = (await next_btn.inner_text()).strip()
                    progress_steps.append(f"Advancing wizard: Clicked '{btn_text}'")
                    await next_btn.click()
                    await page.wait_for_timeout(2500)

                    # Check if clicking Next triggered validation errors on required fields
                    step_errors = await self._scan_validation_errors(page)
                    if step_errors:
                        unanswered_questions.extend(step_errors)
                        progress_steps.append(f"Found {len(step_errors)} required questions needing answers")
                        return {
                            "success": True,
                            "status": "REVIEW",
                            "progress_steps": progress_steps,
                            "unanswered_questions": unanswered_questions[:5],
                            "failure_reason": None
                        }
                else:
                    # No next button and no submit button found
                    break

            # If loop finished without errors
            progress_steps.append("Application form prepared and ready for review")
            return {
                "success": True,
                "status": "READY",
                "progress_steps": progress_steps,
                "unanswered_questions": [],
                "failure_reason": None
            }

        except Exception as e:
            logger.error(f"Application automation error: {e}", exc_info=True)
            progress_steps.append(f"Automation stopped: {str(e)}")
            return {
                "success": False,
                "status": "FAILED",
                "progress_steps": progress_steps,
                "failure_reason": str(e),
                "unanswered_questions": []
            }
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass

    async def submit_application_to_dice(
        self,
        application_url: str,
        resume_file_path: str,
        user_profile: UserProfileSchema
    ) -> Dict[str, Any]:
        """
        Directly executes the application submission on Dice in APPLY mode.
        """
        return await self.prepare_application(
            application_url=application_url,
            resume_file_path=resume_file_path,
            user_profile=user_profile,
            mode="APPLY"
        )

    async def _fill_personal_info(self, page, user_profile: UserProfileSchema) -> List[str]:
        filled = []
        field_mappings = [
            ("First Name", user_profile.first_name, ["first_name", "firstname", "first name", "given-name", "fname"]),
            ("Last Name", user_profile.last_name, ["last_name", "lastname", "last name", "family-name", "lname"]),
            ("Email", user_profile.email, ["email", "e-mail", "email address", "emailaddress"]),
            ("Phone", user_profile.phone, ["phone", "telephone", "mobile", "cell", "phonenumber"]),
            ("City", user_profile.city, ["city", "town"]),
            ("State", user_profile.state, ["state", "province", "region"]),
            ("Zip", user_profile.zip_code, ["zip", "postal", "postal code", "zipcode", "postalcode"]),
            ("LinkedIn", user_profile.linkedin_url, ["linkedin", "linkedin url", "profile url"]),
            ("GitHub", user_profile.github_url, ["github", "github profile"]),
            ("Portfolio", user_profile.portfolio_url, ["portfolio", "website", "personal website"]),
        ]

        for label, val, patterns in field_mappings:
            if not val:
                continue
            for pat in patterns:
                selector = f"input[name*='{pat}' i], input[id*='{pat}' i], input[placeholder*='{pat}' i], input[data-testid*='{pat}' i]"
                elem = page.locator(selector).first
                try:
                    if await elem.count() and await elem.is_visible():
                        curr = await elem.input_value()
                        if not curr.strip():
                            await elem.fill(val)
                            filled.append(label)
                            break
                except Exception:
                    pass
        return filled

    async def _handle_resume_selection(self, page, resume_path: Path) -> bool:
        """
        Uploads or selects the target resume file on Dice.
        - Checks if target resume is already selected.
        - Method 1: Clicks 'File options' button, clicks 'Replace', and sets files on file chooser.
        - Method 2: Direct file input (excluding cover letter).
        - Method 3: Upload dropzone.
        - Strictly isolates Resume area and avoids Cover Letter.
        """
        file_name = resume_path.name

        # 0. Check if currently attached file already matches our target
        try:
            opts_btn = page.locator('button[aria-label="File options"]').first
            if await opts_btn.count():
                card = opts_btn.locator("xpath=./ancestor::div[contains(@class, 'rounded') or contains(@class, 'card') or contains(@class, 'border')][1]")
                if await card.count():
                    card_text = (await card.inner_text()).lower()
                    if file_name.lower() in card_text:
                        logger.info(f"Target resume already attached on Dice: {file_name}")
                        return True
        except Exception as e:
            logger.debug(f"Error checking existing resume card: {e}")

        # 1. Native Dice: 'File options' button -> 'Replace'
        try:
            opts_btn = page.locator('button[aria-label="File options"]').first
            if await opts_btn.count() and await opts_btn.is_visible():
                logger.info("Found 'File options' on Resume card. Clicking...")
                await opts_btn.click()
                await page.wait_for_timeout(600)

                replace_item = page.locator('[role="menuitem"][data-key="replace"], [title="Replace"]').first
                if await replace_item.count() and await replace_item.is_visible():
                    logger.info("Found 'Replace' option. Intercepting file chooser...")
                    try:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await replace_item.click()
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(str(resume_path))
                        await page.wait_for_timeout(3000)

                        body_text = (await page.locator("body").inner_text()).lower()
                        if file_name.lower() in body_text:
                            logger.info(f"Successfully replaced resume with: {file_name}")
                            return True
                    except Exception as ex:
                        logger.debug(f"Replace file chooser attempt failed: {ex}")
        except Exception as e:
            logger.debug(f"Exception during Replace flow: {e}")

        # 2. Fallback: Direct file input (excluding cover letter)
        try:
            file_inputs = await page.locator("input[type='file']").all()
            for inp in file_inputs:
                name_attr = (await inp.get_attribute("name") or "").lower()
                id_attr = (await inp.get_attribute("id") or "").lower()
                aria_attr = (await inp.get_attribute("aria-label") or "").lower()

                # Strictly skip any cover letter input
                if "cover" in name_attr or "cover" in id_attr or "cover" in aria_attr:
                    continue

                await inp.set_input_files(str(resume_path))
                await page.wait_for_timeout(2500)
                new_body = (await page.locator("body").inner_text()).lower()
                if file_name.lower() in new_body:
                    logger.info(f"Direct file input attached resume: {file_name}")
                    return True
        except Exception as e:
            logger.debug(f"Direct file input attempt: {e}")

        # 3. Fallback: Dropzone
        try:
            dropzone = page.locator(
                'div:has-text("Upload your resume"), div:has-text("Upload resume"), '
                'button:has-text("Upload resume"), [data-testid*="dropzone"]'
            ).first
            if await dropzone.count() and await dropzone.is_visible():
                try:
                    async with page.expect_file_chooser(timeout=4000) as fc_info:
                        await dropzone.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(str(resume_path))
                    await page.wait_for_timeout(2500)
                    return True
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Dropzone upload attempt: {e}")

        return False

    async def _clear_cover_letter_field(self, page):
        """
        Ensures that the Cover Letter dropzone and input remain completely empty.
        If an accidental file is attached, clicks its delete/remove button.
        """
        try:
            cover_section = page.locator('div:has-text("Cover letter"), div:has-text("Cover Letter")').first
            if await cover_section.count():
                del_btn = cover_section.locator(
                    'button[aria-label*="delete" i], button[aria-label*="remove" i], '
                    'button:has-text("Delete"), button:has-text("Remove"), svg[data-icon="trash"]'
                ).first
                if await del_btn.count() and await del_btn.is_visible():
                    await del_btn.click()
                    await page.wait_for_timeout(500)
        except Exception as e:
            logger.debug(f"Cover letter cleanup notice: {e}")

    async def _answer_screener_questions(self, page, user_profile: UserProfileSchema) -> int:
        answered = 0
        work_auth = (user_profile.work_authorization or "Green Card").lower()
        is_authorized = any(k in work_auth for k in ["citizen", "green card", "authorized", "permanent"])

        qa_rules = [
            (r"(?:authorized|legally authorized|eligible to work|right to work)", "yes" if is_authorized else "no"),
            (r"(?:require sponsorship|need sponsorship|visa sponsorship|now or in the future)", "no" if is_authorized else "yes"),
            (r"(?:willing to relocate|relocate|relocation)", "yes" if user_profile.willing_to_relocate else "no"),
            (r"(?:commute|travel|on-site|remote|hybrid)", "yes"),
            (r"(?:18 years of age|at least 18|legal age)", "yes"),
            (r"(?:background check|drug test|drug screen)", "yes"),
        ]

        # Process Radio Groups / Yes-No Questions
        radio_groups = await page.locator("fieldset, [role='radiogroup'], div.form-group").all()
        for group in radio_groups:
            try:
                text = (await group.inner_text()).lower()
                for pattern, target_val in qa_rules:
                    if re.search(pattern, text):
                        radio = group.locator(f'input[type="radio"][value*="{target_val}" i], label:has-text("{target_val.capitalize()}")').first
                        if await radio.count() and await radio.is_visible():
                            await radio.click()
                            answered += 1
                            break
            except Exception:
                pass

        # Process Numeric inputs (years of experience)
        numeric_inputs = await page.locator('input[type="number"], input[name*="experience" i]').all()
        for num_inp in numeric_inputs:
            try:
                if await num_inp.is_visible():
                    val = await num_inp.input_value()
                    if not val.strip():
                        exp = re.sub(r"[^\d]", "", user_profile.years_of_experience or "10")
                        await num_inp.fill(exp or "10")
                        answered += 1
            except Exception:
                pass

        return answered

    async def _scan_validation_errors(self, page) -> List[Dict[str, Any]]:
        """
        Scans for real validation errors that prevented advancing the form.
        Only captures fields with aria-invalid='true' or visible error messages.
        """
        errors = []
        try:
            invalid_inputs = await page.locator("input[aria-invalid='true'], textarea[aria-invalid='true'], select[aria-invalid='true']").all()
            for inp in invalid_inputs:
                try:
                    if not await inp.is_visible():
                        continue
                    name_attr = await inp.get_attribute("name") or ""
                    id_attr = await inp.get_attribute("id") or ""
                    label_text = ""
                    if id_attr:
                        lbl = page.locator(f"label[for='{id_attr}']").first
                        if await lbl.count():
                            label_text = (await lbl.inner_text()).strip()
                    q_text = label_text or name_attr or "Required Application Field"
                    errors.append({
                        "question_text": q_text,
                        "field_name": name_attr or id_attr,
                        "options": []
                    })
                except Exception:
                    pass

            error_msgs = await page.locator('[class*="error-message"], [data-testid*="error"], span:has-text("This field is required"), p:has-text("required")').all()
            for em in error_msgs:
                try:
                    if await em.is_visible():
                        txt = (await em.inner_text()).strip()
                        parent = em.locator("xpath=./ancestor::div[contains(@class, 'form-group') or contains(@class, 'field') or contains(@class, 'container')][1]")
                        lbl = parent.locator("label").first if await parent.count() else None
                        q_title = (await lbl.inner_text()).strip() if (lbl and await lbl.count()) else txt
                        if q_title and not any(e["question_text"] == q_title for e in errors):
                            errors.append({
                                "question_text": q_title,
                                "field_name": "",
                                "options": []
                            })
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Error scanning validation errors: {e}")

        return errors

    async def _verify_submission_confirmation(self, page) -> bool:
        """
        Scans the page for confirmation indicators showing the application was submitted.
        """
        try:
            # 1. URL Check
            curr_url = page.url.lower()
            if any(k in curr_url for k in ["/applied", "/confirmation", "status=applied", "/success"]):
                return True

            # 2. Heading Check
            headings = await page.locator("h1, h2, h3, [role='heading']").all()
            for h in headings:
                try:
                    if await h.is_visible():
                        htext = (await h.inner_text()).lower()
                        if any(p in htext for p in [
                            "application submitted",
                            "application sent",
                            "thank you for applying",
                            "successfully applied",
                            "your application was sent",
                            "has been submitted",
                            "you applied for this job",
                        ]):
                            return True
                except Exception:
                    pass

            # 3. Confirmation elements / testids
            confirm_elems = page.locator(
                '[data-testid*="success" i], [data-testid*="confirmation" i], '
                '[class*="success-message" i], [data-testid*="applied-badge" i], '
                '[data-testid="applied-button"]'
            ).first
            if await confirm_elems.count() and await confirm_elems.is_visible():
                return True

            # 4. Strict explicit body confirmation phrases
            body_text = (await page.locator("body").inner_text()).lower()
            confirmation_phrases = [
                "your application has been sent",
                "your application was sent",
                "application submitted successfully",
                "application has been submitted",
                "thank you for applying to",
                "thank you for your application",
                "you've applied to this job",
                "you applied for this job",
                "application received",
            ]
            for phrase in confirmation_phrases:
                if phrase in body_text:
                    return True
        except Exception as e:
            logger.debug(f"Submission confirmation check error: {e}")
        return False

application_browser = ApplicationBrowser()
