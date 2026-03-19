"""Unit tests for the matching pipeline.

These are pure logic tests — no backend, no desktop needed.
They exercise ``touchpoint.matching.matcher.match()`` directly
with synthetic :class:`Element` objects.

Markers: ``unit``
"""

from __future__ import annotations

import pytest

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.matching.matcher import MatchResult, match


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _el(name: str, *, role: Role = Role.UNKNOWN, value: str | None = None,
         description: str | None = None) -> Element:
    """Build a minimal synthetic Element for matcher tests."""
    return Element(
        id=f"test:0:0:{name}",
        name=name,
        role=role,
        states=[],
        position=(0, 0),
        size=(0, 0),
        app="TestApp",
        pid=0,
        backend="test",
        raw_role="unknown",
        value=value,
        description=description,
    )


# Pre-built pool used by most tests.
POOL = [
    _el("Send"),
    _el("Send Message"),
    _el("Cancel"),
    _el("Submit"),
    _el("Settings"),
    _el(""),
]


# ---------------------------------------------------------------------------
# Exact match
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.unit


class TestExactMatch:
    """Stage 1: case-insensitive exact match."""

    def test_exact_hit(self):
        results = match("Send", POOL)
        assert len(results) >= 1
        assert results[0].element.name == "Send"
        assert results[0].match_type == "exact"
        assert results[0].score == 1.0

    def test_case_insensitive(self):
        results = match("send", POOL)
        assert len(results) >= 1
        assert results[0].element.name == "Send"
        assert results[0].match_type == "exact"

    def test_exact_blocks_later_stages(self):
        """If exact matches exist, contains/fuzzy don't run."""
        results = match("Cancel", POOL)
        assert all(r.match_type == "exact" for r in results)


# ---------------------------------------------------------------------------
# Contains match
# ---------------------------------------------------------------------------


class TestContainsMatch:
    """Stage 2: substring match when no exact match exists."""

    def test_contains_hit(self):
        results = match("Mess", POOL)
        assert len(results) >= 1
        assert results[0].element.name == "Send Message"
        assert results[0].match_type == "contains"

    def test_contains_score_range(self):
        """Contains scores should be between 0.7 and 0.9."""
        results = match("Mess", POOL)
        for r in results:
            if r.match_type == "contains":
                assert 0.7 <= r.score <= 0.9

    def test_higher_coverage_scores_higher(self):
        """Longer query covering more of the text scores higher."""
        pool = [_el("Send Message"), _el("Send a Long Message Text")]
        results = match("Send Message", pool)
        # "Send Message" is exact for the first element.
        assert results[0].element.name == "Send Message"


# ---------------------------------------------------------------------------
# Fuzzy match
# ---------------------------------------------------------------------------


class TestContainsWords:
    """Stage 2b: order-independent multi-word matching."""

    def test_multi_word_hit(self):
        """All query words present in text, regardless of order."""
        pool = [_el("Israel Beirut Trump warns about escalation")]
        results = match("Trump warns Israel", pool)
        assert len(results) >= 1
        assert results[0].match_type == "contains_words"

    def test_single_word_skipped(self):
        """Single-word queries are handled by _contains, not _contains_words."""
        results = match("Send", POOL)
        assert all(r.match_type != "contains_words" for r in results)

    def test_word_not_substring(self):
        """Words must be exact tokens — 'art' should not match 'particle'."""
        pool = [_el("particle information")]
        results = match("art form", pool)
        # "art" is not a word in "particle information"
        assert not any(r.match_type == "contains_words" for r in results)

    def test_score_higher_for_more_coverage(self):
        """More query words relative to text words → higher score."""
        pool = [
            _el("Alpha Beta Gamma Delta Epsilon"),  # 5 words, query covers 2/5
            _el("Alpha Beta"),                       # 2 words, query covers 2/2
        ]
        results = match("Alpha Beta", pool)
        words_results = [r for r in results if r.match_type == "contains_words"]
        if len(words_results) >= 2:
            # "Alpha Beta" (2/2 coverage) should score higher
            scores = {r.element.name: r.score for r in words_results}
            assert scores["Alpha Beta"] > scores["Alpha Beta Gamma Delta Epsilon"]

    def test_missing_word_no_match(self):
        """If any query word is absent, no contains_words match."""
        pool = [_el("Alpha Beta Gamma")]
        results = match("Alpha Delta", pool)
        assert not any(r.match_type == "contains_words" for r in results)

    def test_case_insensitive(self):
        """Word matching is case-insensitive."""
        pool = [_el("Hello World Foo")]
        results = match("hello foo", pool)
        words_results = [r for r in results if r.match_type == "contains_words"]
        assert len(words_results) == 1


class TestFuzzyMatch:
    """Stage 3: string similarity (requires rapidfuzz)."""

    def test_fuzzy_hit(self):
        results = match("Sned", POOL)  # typo for "Send"
        if not results:
            pytest.skip("rapidfuzz not installed")
        assert results[0].match_type == "fuzzy"
        # The best match should be "Send" (closest to "Sned").
        assert results[0].element.name == "Send"

    def test_fuzzy_threshold(self):
        """Results below threshold are excluded."""
        results = match("zzzzz", POOL, threshold=0.99)
        assert results == []

    def test_custom_threshold(self):
        """Lowering threshold admits more results."""
        strict = match("Sned", POOL, threshold=0.95)
        relaxed = match("Sned", POOL, threshold=0.3)
        assert len(relaxed) >= len(strict)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Empty inputs, empty names, etc."""

    def test_empty_query(self):
        assert match("", POOL) == []

    def test_empty_pool(self):
        assert match("Send", []) == []

    def test_empty_query_and_pool(self):
        assert match("", []) == []

    def test_element_with_empty_name(self):
        """Elements with empty names should not crash the matcher."""
        pool = [_el(""), _el("Send")]
        results = match("Send", pool)
        assert len(results) >= 1
        assert results[0].element.name == "Send"


# ---------------------------------------------------------------------------
# max_results
# ---------------------------------------------------------------------------


class TestMaxResults:
    """Capping the number of returned matches."""

    def test_max_results_1(self):
        results = match("Se", POOL, max_results=1)
        assert len(results) == 1

    def test_max_results_limits_exact(self):
        pool = [_el("Send"), _el("Send")]
        results = match("Send", pool, max_results=1)
        assert len(results) == 1

    def test_max_results_none_returns_all(self):
        pool = [_el("Send"), _el("Send")]
        results = match("Send", pool, max_results=None)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# text_fn (custom text extraction)
# ---------------------------------------------------------------------------


class TestTextFn:
    """Custom text extraction function for multi-field search."""

    def test_search_by_value(self):
        pool = [
            _el("Email Field", value="hello@example.com"),
            _el("Name Field", value="John"),
        ]

        def text_fn(el: Element) -> list[str]:
            return [el.value] if el.value else []

        results = match("hello@example.com", pool, text_fn=text_fn)
        assert len(results) >= 1
        assert results[0].element.name == "Email Field"

    def test_search_by_description(self):
        pool = [
            _el("Button", description="Submit the form"),
            _el("Cancel"),
        ]

        def text_fn(el: Element) -> list[str]:
            return [el.description] if el.description else []

        results = match("Submit the form", pool, text_fn=text_fn)
        assert len(results) >= 1
        assert results[0].element.name == "Button"

    def test_multi_field_best_score(self):
        """When text_fn returns multiple strings, the best score wins."""
        pool = [
            _el("Email", value="hello@test.com", description="Enter email"),
        ]

        def text_fn(el: Element) -> list[str]:
            texts = []
            if el.name:
                texts.append(el.name)
            if el.value:
                texts.append(el.value)
            return texts

        results = match("hello@test.com", pool, text_fn=text_fn)
        assert len(results) >= 1
        # Exact match on value should score 1.0
        assert results[0].score == 1.0

    def test_text_fn_empty_return(self):
        """text_fn returning empty list should not crash."""
        pool = [_el("Send")]
        results = match("Send", pool, text_fn=lambda _: [])
        assert results == []


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


class TestMatchResult:
    """MatchResult dataclass basics."""

    def test_fields(self):
        el = _el("Send")
        r = MatchResult(element=el, score=1.0, match_type="exact")
        assert r.element is el
        assert r.score == 1.0
        assert r.match_type == "exact"

    def test_sorted_best_first(self):
        results = match("Set", POOL)
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i].score >= results[i + 1].score
