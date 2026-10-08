"""Screener-question answering rules (board-agnostic text rules; reusable by other boards)."""
import re

PERSONAL_FIELDS = [
    (r"first\s*name", "first_name"),
    (r"last\s*name|surname", "last_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile", "phone"),
    (r"\bcity\b", "city"),
    (r"\bstate\b|province", "state"),
    (r"zip|postal", "zip_code"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"portfolio|website", "portfolio"),
]


def normalize(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", q.lower()).strip()[:200]


def _yn(v: bool) -> str:
    return "Yes" if v else "No"


def _full_location(profile: dict) -> str:
    """Build 'City, State' or 'City' from profile fields."""
    parts = [profile.get("city"), profile.get("state")]
    return ", ".join(p for p in parts if p)


def resolve_answer(question: str, profile: dict, saved: dict[str, str]) -> str | None:
    if question in saved:
        return saved[question]
    key = normalize(question)
    if key in saved:
        return saved[key]
    primary_key = normalize(question.split("|")[0])
    if primary_key in saved:
        return saved[primary_key]
    for sk, ans in saved.items():
        sk_norm = normalize(sk)
        if sk and (sk in question or sk_norm == key or sk_norm in key or key in sk_norm):
            return ans

    q = question.lower()
    if re.search(r"city of residence|current city|current location|where .* (located|live|reside)|candidatelocation", q):
        loc = _full_location(profile)
        if loc:
            return loc

    if re.search(r"first\s*name", q):
        return profile.get("first_name")
    if re.search(r"last\s*name|surname", q):
        return profile.get("last_name")
    if re.search(r"full\s*name", q):
        first = profile.get("first_name", "")
        last = profile.get("last_name", "")
        name = f"{first} {last}".strip()
        return name or None
    if re.search(r"phone|mobile", q):
        return profile.get("phone")
    if re.search(r"e-?mail", q):
        return profile.get("email")
    if re.search(r"linkedin", q):
        return profile.get("linkedin")
    if re.search(r"github", q):
        return profile.get("github")
    if re.search(r"portfolio|website", q):
        return profile.get("portfolio")
    if re.search(r"zip|postal", q):
        return profile.get("zip_code")

    if re.search(r"sponsor|visa", q):
        return _yn(bool(profile.get("requires_sponsorship")))
    if re.search(r"authori[sz]ed to work|legally (authori[sz]ed|eligible)|work authori[sz]ation|eligible to work|right to work", q):
        return _yn(bool(profile.get("authorized_to_work", True)))
    if re.search(r"relocat", q):
        return _yn(bool(profile.get("willing_to_relocate", True)))
    if re.search(r"commut", q):
        return "Yes"
    if re.search(r"18 years|at least 18|over (the age of )?18", q):
        return "Yes"
    if re.search(r"background (check|screen)|drug (test|screen)", q):
        return "Yes"
    if re.search(r"years of (relevant |work |professional )?experience|years experience|how many years", q):
        yrs = profile.get("years_experience")
        return str(yrs) if yrs is not None else None
    return None
