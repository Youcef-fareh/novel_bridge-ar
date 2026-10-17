"""Deterministic glossary candidates for user review; never auto-applied."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from backend.models import Chapter

_CANDIDATE_RE = re.compile(
    r"\b[A-Z][a-z]+(?:['’][A-Z]?[a-z]+)?"
    r"(?:\s+(?:of|the|and|de|van|[A-Z][a-z]+(?:['’][A-Z]?[a-z]+)?)){0,3}\b"
)
_IGNORED_SINGLE_WORDS = {
    "After", "Before", "Chapter", "Finally", "However", "Meanwhile",
    "Perhaps", "Suddenly", "Then", "There", "This", "When", "Where",
    "While", "Yes",
}


def suggest_glossary_terms(
    chapters: Iterable[Chapter],
    existing_terms: Iterable[str] = (),
    limit: int = 50,
) -> list[dict[str, int | str | None]]:
    existing = {term.casefold() for term in existing_terms}
    occurrences: Counter[str] = Counter()
    chapter_counts: dict[str, set[int]] = defaultdict(set)
    for chapter in chapters:
        text = chapter.raw_text or ""
        for match in _CANDIDATE_RE.finditer(text):
            term = " ".join(match.group().split())
            word_count = len(term.split())
            if term.casefold() in existing:
                continue
            if word_count == 1 and term in _IGNORED_SINGLE_WORDS:
                continue
            occurrences[term] += 1
            chapter_counts[term].add(chapter.id or chapter.index)

    candidates = []
    for term, count in occurrences.items():
        chapter_count = len(chapter_counts[term])
        if len(term.split()) == 1:
            if count < 4 or chapter_count < 3:
                continue
        elif count < 2 or chapter_count < 2:
            continue
        candidates.append({
            "source_term": term,
            "target_term": None,
            "occurrences": count,
            "chapter_count": chapter_count,
            "status": "needs_review",
        })
    candidates.sort(
        key=lambda item: (-int(item["occurrences"]), -int(item["chapter_count"]), str(item["source_term"]).casefold())
    )
    return candidates[:max(1, limit)]
