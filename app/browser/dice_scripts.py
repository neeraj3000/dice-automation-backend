"""In-page JavaScript evaluators for Dice (one CDP round-trip instead of repeated locator loops)."""

CARD_SELECTOR = '[data-testid="job-card"], div[data-job-guid], [role="article"]'

EXTRACT_JS = r"""
() => {
  const sel = '[data-testid="job-card"], div[data-job-guid], [role="article"]';
  const txt = e => e ? (e.innerText || e.textContent || '').trim() : '';
  return [...document.querySelectorAll(sel)].map(c => {
    const link = c.querySelector('a[href*="/job-detail/"]');
    const href = link ? link.href : '';
    const m = href.match(/job-detail\/([0-9a-zA-Z-]+)/);
    const id = c.getAttribute('data-job-guid') || (m ? m[1] : '');
    const title = txt(c.querySelector('[data-testid="job-search-job-detail-link"]')) || txt(link);
    const company = txt(c.querySelector('a[href*="/company-profile/"]'));
    const text = txt(c);
    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    const lower = text.toLowerCase();
    const appliedRe = /^(\u2714\s*)?applied$/i;
    const applied = !!c.querySelector('#applied-label')
      || [...c.querySelectorAll('[aria-label]')].some(e => appliedRe.test(e.getAttribute('aria-label') || ''))
      || lines.some(l => appliedRe.test(l));
    const easy = !!c.querySelector('#easyApply-label') || /easy apply/i.test(text);
    const wm = /remote/.test(lower) ? 'Remote' : /hybrid/.test(lower) ? 'Hybrid' : /on-?site/.test(lower) ? 'On-Site' : '';
    const salary = lines.find(l => /\$\s?\d/.test(l)) || '';
    const et = lines.find(l => /^(full[- ]?time|part[- ]?time|contract|third party|c2c)/i.test(l)) || '';
    const loc = txt(c.querySelector('[data-testid="location"]'))
      || lines.find(l => l !== title && l !== company && /,\s*[A-Z]{2}\b|remote/i.test(l)) || '';
    const posted = lines.find(l => /(ago|today|yesterday|posted)/i.test(l)) || '';
    return { external_job_id: id, title, company, location: loc, work_setting: wm, salary,
             employment_type: et, posted_date: posted, is_easy_apply: easy, is_applied: applied, url: href };
  });
}
"""

SCROLL_JS = r"""
() => {
  const cards = document.querySelectorAll('[data-testid="job-card"], div[data-job-guid], [role="article"]');
  const last = cards[cards.length - 1];
  let el = last ? last.parentElement : null;
  while (el && el !== document.body) {
    const s = getComputedStyle(el);
    if (/(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight) { el.scrollBy({ top: 900 }); break; }
    el = el.parentElement;
  }
  if (last) last.scrollIntoView({ block: 'end' });
}
"""

NEXT_PAGE_JS = r"""
() => {
  const b = document.querySelector('nav[aria-label="Pagination"] [aria-label="Next"]:not([aria-disabled="true"])');
  if (b) { b.click(); return true; }
  return false;
}
"""

DESCRIPTOR_JS = r"""
el => {
  let t = '';
  if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) t = l.innerText; }
  if (!t) { const p = el.closest('label'); if (p) t = p.innerText; }
  const fs = el.closest('fieldset');
  if (!t && fs) { const lg = fs.querySelector('legend'); if (lg) t = lg.innerText; }
  return [t, el.getAttribute('aria-label'), el.placeholder, el.name, el.id].filter(Boolean).join(' | ').trim();
}
"""

UNANSWERED_JS = r"""
() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const desc = el => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id + '"]'); if (l) t = l.innerText; }
    if (!t) { const p = el.closest('label'); if (p) t = p.innerText; }
    const fs = el.closest('fieldset');
    if (!t && fs) { const lg = fs.querySelector('legend'); if (lg) t = lg.innerText; }
    return (t || el.getAttribute('aria-label') || el.placeholder || el.name || '').trim();
  };
  const out = new Map();
  const opts = el => el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()).filter(Boolean) : undefined;
  document.querySelectorAll('[aria-invalid="true"]').forEach(el => { if (!vis(el)) return; const q = desc(el); if (q) out.set(q, { question: q, options: opts(el) }); });
  document.querySelectorAll('input[required]:not([type=file]):not([type=radio]):not([type=checkbox]), select[required], textarea[required]').forEach(el => {
    if (vis(el) && !el.value) { const q = desc(el); if (q) out.set(q, { question: q, options: opts(el) }); }
  });
  document.querySelectorAll('fieldset').forEach(fs => {
    const r = fs.querySelectorAll('input[type=radio]');
    if (r.length && !fs.querySelector('input[type=radio]:checked') && [...r].some(x => x.required || x.getAttribute('aria-required') === 'true')) {
      const lg = fs.querySelector('legend'); const q = (lg ? lg.innerText : '').trim();
      if (q) out.set(q, { question: q, options: [...fs.querySelectorAll('label')].map(l => l.innerText.trim()).filter(Boolean) });
    }
  });
  return [...out.values()];
}
"""

CLEAR_COVER_JS = r"""
() => {
  const heads = [...document.querySelectorAll('h1,h2,h3,h4,legend,label,span,p')].filter(e => /^cover letter$/i.test((e.innerText || '').trim()));
  for (const h of heads) {
    let e = h;
    for (let i = 0; i < 4 && e; i++) {
      e = e.parentElement;
      const b = e && [...e.querySelectorAll('button')].find(x => /delete|remove/i.test((x.getAttribute('aria-label') || '') + ' ' + (x.innerText || '')));
      if (b) { b.click(); return true; }
    }
  }
  return false;
}
"""

BUTTON_IN_COVER_JS = r"""
b => { let e = b; for (let i = 0; i < 3 && e.parentElement; i++) e = e.parentElement; return /cover letter/i.test(e.innerText || ''); }
"""
