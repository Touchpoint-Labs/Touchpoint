"""Fuzzy and exact matching pipeline for UI elements.

Given a query string and a list of elements, the matcher runs a
four-stage pipeline — exact → contains → contains-words → fuzzy —
and returns ranked results with confidence scores.

The fuzzy stage requires ``rapidfuzz``.  If it is not installed the
matcher still works but only performs exact, contains, and
contains-words matching.

Example::

    >>> from touchpoint.matching.matcher import match
    >>> results = match("Send", elements)
    >>> results[0].element.name
    'Send'
    >>> results[0].score
    1.0
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from touchpoint.core.element import Element

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:
    _fuzz = None

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class MatchResult:
    """A single match from the matching pipeline.

    Args:
        element: The matched element.
        score: Confidence score from ``0.0`` (no match) to ``1.0``
            (perfect match).
        match_type: Which stage produced this match —
            ``"exact"``, ``"contains"``, or ``"fuzzy"``.

    Example::

        >>> result.score
        0.85
        >>> result.match_type
        'contains'
    """

    element: Element
    score: float
    match_type: str


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def match(
    query: str,
    elements: list[Element],
    max_results: int | None = None,
    threshold: float = 0.6,
    text_fn: Callable[[Element], list[str]] | None = None,
) -> list[MatchResult]:
    """Run the matching pipeline against a list of elements.

    Stages run in order.  If an earlier stage produces results the
    later stages are skipped:

    1. **Exact** — text equals *query* (case-insensitive).
       Score is always ``1.0``.
    2. **Contains** — *query* appears inside the text
       (case-insensitive).  Score scales by how much of the text
       the query covers (``0.7`` – ``0.9``).
    3. **Fuzzy** — string similarity via ``rapidfuzz``.  Only runs
       if ``rapidfuzz`` is installed.  Results below *threshold*
       are discarded.

    Args:
        query: The search string (e.g. ``"Send"``, ``"submit"``).
        elements: Pre-filtered list of elements to search through.
            The public API handles ``role``/``states`` filtering
            before calling this function.
        max_results: Maximum number of matches to return.  ``None``
            (default) returns all matches.
        threshold: Minimum score for fuzzy matches (``0.0`` – ``1.0``).
            Default ``0.6``.
        text_fn: Optional callable ``(Element) → list[str]`` that
            extracts the text strings to match against.  When
            ``None`` (default), only ``element.name`` is used.
            When provided, the **best** score across all returned
            strings is kept for each element.

    Returns:
        Matches sorted by score (best first).  Empty list if nothing
        matched.

    Example::

        >>> match("Send", elements)
        [MatchResult(element=Element('Send', ...), score=1.0, match_type='exact')]
    """
    if not query or not elements:
        return []

    query_lower = query.lower()

    # Stage 1: exact match.
    exact = _exact(query_lower, elements, text_fn)
    if exact:
        return exact[:max_results]

    # Stage 2: contains match.
    contains = _contains(query_lower, elements, text_fn)
    if contains:
        return contains[:max_results]

    # Stage 2b: word-contains — all query words appear in the text,
    # but not necessarily as a contiguous substring.
    word_contains = _contains_words(query_lower, elements, text_fn)
    if word_contains:
        return word_contains[:max_results]

    # Stage 3: fuzzy match (requires rapidfuzz).
    if _fuzz is not None:
        return _fuzzy_match(query_lower, elements, threshold, text_fn)[:max_results]

    return []


# ---------------------------------------------------------------------------
# Text extraction helper
# ---------------------------------------------------------------------------


def _get_texts(el: Element, text_fn: Callable[[Element], list[str]] | None) -> list[str]:
    """Return the searchable text strings for an element."""
    if text_fn is not None:
        return text_fn(el)
    return [el.name] if el.name else []


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def _exact(
    query: str,
    elements: list[Element],
    text_fn: Callable[[Element], list[str]] | None,
) -> list[MatchResult]:
    """Case-insensitive exact match."""
    results: list[MatchResult] = []
    for el in elements:
        for text in _get_texts(el, text_fn):
            if text.lower() == query:
                results.append(
                    MatchResult(element=el, score=1.0, match_type="exact")
                )
                break  # one match per element is enough
    return results


def _contains(
    query: str,
    elements: list[Element],
    text_fn: Callable[[Element], list[str]] | None,
) -> list[MatchResult]:
    """Case-insensitive substring match, scored by coverage."""
    results: list[MatchResult] = []
    for el in elements:
        best_score = 0.0
        for text in _get_texts(el, text_fn):
            text_lower = text.lower()
            if query in text_lower and text_lower:
                coverage = len(query) / len(text_lower)
                score = 0.7 + 0.2 * coverage
                best_score = max(best_score, score)
        if best_score > 0:
            results.append(
                MatchResult(
                    element=el, score=round(best_score, 3), match_type="contains",
                )
            )
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _contains_words(
    query: str,
    elements: list[Element],
    text_fn: Callable[[Element], list[str]] | None,
) -> list[MatchResult]:
    """All query words appear in the text (order-independent).

    Catches multi-word queries like ``"Israel Beirut Trump warns"``
    against long element names where the words are present but not
    as a contiguous substring.  Score is based on word coverage:
    how many of the text's words are covered by the query.
    """
    query_words = set(query.split())
    if len(query_words) < 2:
        # Single-word queries are handled by _contains.
        return []
    results: list[MatchResult] = []
    for el in elements:
        best_score = 0.0
        for text in _get_texts(el, text_fn):
            text_words = set(text.lower().split())
            if query_words <= text_words:
                coverage = len(query_words) / max(len(text_words), 1)
                score = 0.65 + 0.2 * coverage
                best_score = max(best_score, score)
        if best_score > 0:
            results.append(
                MatchResult(
                    element=el,
                    score=round(best_score, 3),
                    match_type="contains_words",
                )
            )
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _fuzzy_match(
    query: str,
    elements: list[Element],
    threshold: float,
    text_fn: Callable[[Element], list[str]] | None,
) -> list[MatchResult]:
    """String similarity via rapidfuzz, filtered by threshold.

    Uses ``ratio`` which computes the normalised Levenshtein
    similarity between the query and each element's text.  This
    avoids inflated scores from partial-ratio algorithms that
    match very short substrings within long texts.
    """
    cutoff = threshold
    results: list[MatchResult] = []
    for el in elements:
        best_ratio = 0.0
        for text in _get_texts(el, text_fn):
            ratio = _fuzz.ratio(query, text.lower()) / 100.0
            best_ratio = max(best_ratio, ratio)
        if best_ratio >= cutoff:
            results.append(
                MatchResult(
                    element=el, score=round(best_ratio, 3), match_type="fuzzy",
                )
            )
    results.sort(key=lambda r: r.score, reverse=True)
    return results
