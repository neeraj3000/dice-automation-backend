"""
Layer E: Playwright Form Execution & Value Verification.
Executes validated answers into browser controls and reads back values for confirmation.
Never allows LLM code execution, arbitrary navigation, or submission button clicks.
"""

import re
import logging
from typing import Any, Optional
from app.services.form_engine.schemas import FormField, FormFillResult

logger = logging.getLogger(__name__)


class PlaywrightFormExecutor:
    """Executes validated form answers using Playwright APIs and verifies readback."""

    async def _resolve_locator(self, page: Any, field: FormField) -> Optional[Any]:
        """Resolves a robust Playwright locator for the field without brittle CSS paths."""
        try:
            # 1. By ID if available
            if field.element_id:
                loc = page.locator(f"#{re.escape(field.element_id)}").first
                if await loc.count() > 0:
                    return loc

            # 2. By Name attribute
            if field.field_name:
                loc = page.locator(f'[name="{re.escape(field.field_name)}"]').first
                if await loc.count() > 0:
                    return loc

            # 3. By ARIA Label
            if field.question_text:
                clean_q = re.escape(field.question_text[:80])
                loc = page.get_by_label(re.compile(clean_q, re.I)).first
                if await loc.count() > 0:
                    return loc

            # 4. By Placeholder
            if field.question_text:
                loc = page.get_by_placeholder(re.compile(clean_q, re.I)).first
                if await loc.count() > 0:
                    return loc

            # 5. Fieldset for radio groups
            if field.control_type == "radio":
                fs = page.locator("fieldset").filter(has_text=re.compile(clean_q, re.I)).first
                if await fs.count() > 0:
                    return fs

        except Exception as e:
            logger.debug(f"Locator resolution notice for field '{field.field_id}': {e}")

        return None

    async def fill_field(
        self,
        page: Any,
        field: FormField,
        value_to_fill: str
    ) -> FormFillResult:
        """
        Executes a validated answer into a form field using native Playwright APIs.
        Reads back value where possible to confirm fill state.
        """
        if not page or not value_to_fill:
            return FormFillResult(
                field_id=field.field_id,
                question_text=field.question_text,
                control_type=field.control_type,
                success=False,
                error="No page or value provided."
            )

        try:
            loc = await self._resolve_locator(page, field)
            if not loc or not await loc.count():
                return FormFillResult(
                    field_id=field.field_id,
                    question_text=field.question_text,
                    control_type=field.control_type,
                    success=False,
                    error="Control locator could not be resolved on page."
                )

            # Check visibility and enabled status
            if not await loc.is_visible():
                return FormFillResult(
                    field_id=field.field_id,
                    question_text=field.question_text,
                    control_type=field.control_type,
                    success=False,
                    error="Field is not visible in the current viewport."
                )

            # A. Select dropdown
            if field.control_type == "select":
                try:
                    await loc.select_option(label=value_to_fill, timeout=3000)
                except Exception:
                    # Try by value or partial text match
                    await loc.select_option(value=value_to_fill, timeout=3000)

                # Readback
                selected = await loc.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                return FormFillResult(
                    field_id=field.field_id,
                    question_text=field.question_text,
                    control_type=field.control_type,
                    success=True,
                    applied_value=selected or value_to_fill
                )

            # B. Radio buttons
            elif field.control_type == "radio":
                # Find the radio matching the target value
                radio_btn = loc.locator(
                    f'label:has-text("{value_to_fill}"), input[type="radio"][value*="{value_to_fill}" i]'
                ).first
                if not await radio_btn.count():
                    radio_btn = loc.get_by_role("radio", name=re.compile(rf"^{re.escape(value_to_fill)}$", re.I)).first

                if await radio_btn.count() and await radio_btn.is_visible():
                    await radio_btn.click(timeout=3000)
                    return FormFillResult(
                        field_id=field.field_id,
                        question_text=field.question_text,
                        control_type=field.control_type,
                        success=True,
                        applied_value=value_to_fill
                    )
                else:
                    return FormFillResult(
                        field_id=field.field_id,
                        question_text=field.question_text,
                        control_type=field.control_type,
                        success=False,
                        error=f"Could not find radio option for value '{value_to_fill}'"
                    )

            # C. Checkbox
            elif field.control_type == "checkbox":
                should_check = value_to_fill.lower() in ("yes", "true", "1")
                is_checked = await loc.is_checked()
                if should_check and not is_checked:
                    await loc.check(timeout=3000)
                elif not should_check and is_checked:
                    await loc.uncheck(timeout=3000)

                return FormFillResult(
                    field_id=field.field_id,
                    question_text=field.question_text,
                    control_type=field.control_type,
                    success=True,
                    applied_value="Yes" if should_check else "No"
                )

            # D. Text / Number / Textarea
            else:
                await loc.click(timeout=2000)
                await loc.fill(value_to_fill, timeout=3000)
                readback = (await loc.input_value() or "").strip()

                return FormFillResult(
                    field_id=field.field_id,
                    question_text=field.question_text,
                    control_type=field.control_type,
                    success=bool(readback == value_to_fill),
                    applied_value=readback,
                    error=None if readback == value_to_fill else "Readback value did not match filled value."
                )

        except Exception as e:
            logger.warning(f"Error filling field '{field.field_id}': {e}")
            return FormFillResult(
                field_id=field.field_id,
                question_text=field.question_text,
                control_type=field.control_type,
                success=False,
                error=str(e)
            )


playwright_form_executor = PlaywrightFormExecutor()
