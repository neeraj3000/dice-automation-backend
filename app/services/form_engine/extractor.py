"""
Layer B: Dynamic field extraction for application forms.
Extracts unknown or unfilled form fields, labels, options, and types
using Playwright locator and in-page JavaScript evaluation.
"""

import logging
from typing import List, Dict, Any, Optional
from app.services.form_engine.schemas import FormField

logger = logging.getLogger(__name__)

# In-page JS extractor that runs in a single CDP round-trip
FIELD_EXTRACTION_JS = r"""
() => {
  const isVisible = el => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  };

  const getLabelText = el => {
    if (!el) return '';
    let labelText = '';
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl && isVisible(lbl)) labelText = lbl.innerText;
    }
    if (!labelText) {
      const parentLbl = el.closest('label');
      if (parentLbl && isVisible(parentLbl)) {
        // Clone and remove input text to keep label clean
        const clone = parentLbl.cloneNode(true);
        clone.querySelectorAll('input, select, textarea').forEach(n => n.remove());
        labelText = clone.innerText;
      }
    }
    if (!labelText) {
      const fs = el.closest('fieldset');
      if (fs) {
        const lg = fs.querySelector('legend');
        if (lg && isVisible(lg)) labelText = lg.innerText;
      }
    }
    if (!labelText) {
      const container = el.closest('.form-group, .field, [class*="formField"], [class*="question"]');
      if (container) {
        const heading = container.querySelector('label, h3, h4, h5, p, span');
        if (heading && isVisible(heading)) labelText = heading.innerText;
      }
    }
    const aria = el.getAttribute('aria-label') || '';
    const placeholder = el.placeholder || '';
    const name = el.name || '';

    const text = (labelText || aria || placeholder || name).replace(/\s+/g, ' ').trim();
    // Clean trailing colons and asterisks
    return text.replace(/[\*:\s]+$/, '').trim();
  };

  const isFieldRequired = el => {
    if (!el) return false;
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    const lbl = getLabelText(el);
    if (/\*|required/i.test(lbl)) return true;
    const fs = el.closest('fieldset');
    if (fs && (fs.getAttribute('aria-required') === 'true' || /\*|required/i.test(fs.innerText))) return true;
    return false;
  };

  const results = [];
  const seenIds = new Set();

  // 1. Radio groups & Fieldsets
  document.querySelectorAll('fieldset, [role="radiogroup"]').forEach((fs, idx) => {
    if (!isVisible(fs)) return;
    const radios = [...fs.querySelectorAll('input[type="radio"]')].filter(isVisible);
    if (radios.length === 0) return;

    const legend = fs.querySelector('legend');
    const question = (legend ? legend.innerText : getLabelText(radios[0])).replace(/\s+/g, ' ').trim();
    if (!question) return;

    const options = radios.map(r => {
      let optText = '';
      if (r.id) {
        const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
        if (l) optText = l.innerText;
      }
      if (!optText) {
        const parentL = r.closest('label');
        if (parentL) optText = parentL.innerText;
      }
      return (optText || r.value || '').trim();
    }).filter(Boolean);

    const checked = fs.querySelector('input[type="radio"]:checked');
    const currVal = checked ? (checked.value || '') : null;
    const stableId = (radios[0].name || fs.id || `radio_group_${idx}`).trim();

    if (!seenIds.has(stableId)) {
      seenIds.add(stableId);
      results.push({
        field_id: stableId,
        question_text: question,
        control_type: 'radio',
        required: isFieldRequired(fs) || radios.some(isFieldRequired),
        options: options,
        current_value: currVal,
        field_name: radios[0].name || '',
        element_id: fs.id || radios[0].id || ''
      });
    }
  });

  // 2. Select dropdowns
  document.querySelectorAll('select').forEach((sel, idx) => {
    if (!isVisible(sel)) return;
    const qText = getLabelText(sel);
    if (!qText) return;

    const options = [...sel.options]
      .map(o => (o.text || o.value || '').trim())
      .filter(t => t && !/^select|choose|please select$/i.test(t));

    const selectedOpt = sel.options[sel.selectedIndex];
    const currVal = selectedOpt && selectedOpt.value && !/^select|choose$/i.test(selectedOpt.text)
      ? selectedOpt.text.trim()
      : null;

    const stableId = sel.id || sel.name || `select_${idx}`;
    if (!seenIds.has(stableId)) {
      seenIds.add(stableId);
      results.push({
        field_id: stableId,
        question_text: qText,
        control_type: 'select',
        required: isFieldRequired(sel),
        options: options,
        current_value: currVal,
        field_name: sel.name || '',
        element_id: sel.id || ''
      });
    }
  });

  // 3. Text inputs, Number inputs, Textareas
  const textInputs = document.querySelectorAll(
    'input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]), textarea'
  );

  textInputs.forEach((inp, idx) => {
    if (!isVisible(inp)) return;
    const name = (inp.name || '').toLowerCase();
    const id = (inp.id || '').toLowerCase();

    // Skip resume/cover inputs
    if (name.includes('cover') || id.includes('cover') || name.includes('resume') || id.includes('resume')) return;

    const qText = getLabelText(inp);
    if (!qText) return;

    const val = (inp.value || '').trim();
    const controlType = inp.tagName.toLowerCase() === 'textarea' ? 'textarea' : (inp.type === 'number' ? 'number' : 'text');
    const stableId = inp.id || inp.name || `input_${idx}`;

    if (!seenIds.has(stableId)) {
      seenIds.add(stableId);
      results.push({
        field_id: stableId,
        question_text: qText,
        control_type: controlType,
        required: isFieldRequired(inp),
        options: [],
        current_value: val || null,
        field_name: inp.name || '',
        element_id: inp.id || ''
      });
    }
  });

  // 4. Checkboxes (standalone confirmation/questions, excluding multi-select)
  document.querySelectorAll('input[type="checkbox"]').forEach((chk, idx) => {
    if (!isVisible(chk)) return;
    const qText = getLabelText(chk);
    if (!qText) return;

    const stableId = chk.id || chk.name || `checkbox_${idx}`;
    if (!seenIds.has(stableId)) {
      seenIds.add(stableId);
      results.push({
        field_id: stableId,
        question_text: qText,
        control_type: 'checkbox',
        required: isFieldRequired(chk),
        options: ['Yes', 'No'],
        current_value: chk.checked ? 'Yes' : 'No',
        field_name: chk.name || '',
        element_id: chk.id || ''
      });
    }
  });

  return results;
}
"""


class DynamicFieldExtractor:
    """Extracts dynamic and unfamiliar application questions from a Playwright page."""

    async def extract_fields(self, page: Any) -> List[FormField]:
        """
        Extracts visible, active form fields from the given Playwright page.
        Returns a list of structured FormField instances.
        """
        if not page:
            return []

        try:
            raw_data = await page.evaluate(FIELD_EXTRACTION_JS)
            fields = []
            for item in raw_data:
                try:
                    fields.append(FormField(**item))
                except Exception as parse_err:
                    logger.debug(f"Error parsing extracted field {item}: {parse_err}")
            return fields
        except Exception as e:
            logger.warning(f"Dynamic form field extraction failed: {e}")
            return []


dynamic_field_extractor = DynamicFieldExtractor()
