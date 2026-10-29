"""Title similarity for spotting duplicate ideas. Standard library only.

Titles are reduced to a set of word stems (lowercase, filler words removed, simple suffixes stripped).
Two titles score by how many stems they share (Dice coefficient), where stems with a one-letter
spelling difference (organiser / organizer) still count as shared.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

THRESHOLD = 0.8  # tuned in tests/test_ideas_similar.py

STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "with", "my", "our", "your", "new",
    "app", "apps", "tool", "tools", "project", "idea", "system", "thing", "simple", "small", "little",
}
_SUFFIXES = ("ings", "ing", "ers", "er", "ies", "es", "ed", "s")


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    # planner -> plann -> plan: collapse a doubled final letter left by the suffix
    if len(word) >= 4 and word[-1] == word[-2]:
        word = word[:-1]
    return word


def tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    # Check filler words before and after stemming, so plurals like "ideas" and "apps" count as filler too.
    kept = [w for w in words if w not in STOPWORDS and _stem(w) not in STOPWORDS] or words
    return sorted({_stem(w) for w in kept})


def same(a: str, b: str) -> bool:
    if a == b:
        return True
    return min(len(a), len(b)) >= 5 and SequenceMatcher(None, a, b).ratio() >= 0.85


def score(a: str, b: str) -> float:
    """0 (unrelated) to 1 (same idea)."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    unmatched = list(tb)
    shared = 0
    for t in ta:
        hit = next((u for u in unmatched if same(t, u)), None)
        if hit is not None:
            unmatched.remove(hit)
            shared += 1
    return 2 * shared / (len(ta) + len(tb))


def is_duplicate(a: str, b: str) -> bool:
    return score(a, b) >= THRESHOLD
