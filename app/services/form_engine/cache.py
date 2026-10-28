"""
User-scoped caching and normalization for form question interpretations.
Ensures cached mappings cannot leak between candidate accounts.
"""

import re
import hashlib
from typing import Dict, Optional, Tuple
from app.services.form_engine.schemas import ProposedFormAnswer


def normalize_question_key(question_text: str, options: list[str] = None) -> str:
    """Normalizes question text and options into a stable lookup key."""
    clean_q = re.sub(r"[^a-z0-9 ]+", "", question_text.lower()).strip()[:150]
    opts_key = ""
    if options:
        clean_opts = ",".join(sorted(re.sub(r"[^a-z0-9]+", "", o.lower()) for o in options))
        opts_key = f"|opts:{clean_opts}"
    return f"{clean_q}{opts_key}"


class FormEngineCache:
    """
    In-memory cache for question interpretations partitioned by user_id.
    Prevents cross-tenant answer leakage.
    """

    def __init__(self):
        # user_id -> { normalized_key -> ProposedFormAnswer }
        self._cache: Dict[str, Dict[str, ProposedFormAnswer]] = {}

    def get(self, user_id: str, question_text: str, options: list[str] = None) -> Optional[ProposedFormAnswer]:
        """Retrieves cached interpretation for a specific candidate."""
        uid = str(user_id or "default")
        user_bucket = self._cache.get(uid)
        if not user_bucket:
            return None
        key = normalize_question_key(question_text, options)
        return user_bucket.get(key)

    def set(self, user_id: str, question_text: str, answer_plan: ProposedFormAnswer, options: list[str] = None):
        """Stores interpretation in candidate's isolated cache."""
        uid = str(user_id or "default")
        if uid not in self._cache:
            self._cache[uid] = {}
        key = normalize_question_key(question_text, options)
        self._cache[uid][key] = answer_plan

    def clear(self, user_id: Optional[str] = None):
        """Clears cache for one or all users."""
        if user_id:
            self._cache.pop(str(user_id), None)
        else:
            self._cache.clear()


form_engine_cache = FormEngineCache()
