"""Tests for tp.configure() — runtime configuration.

Mostly unit tests (pure logic, no desktop needed).  One integration
test verifies that fuzzy_threshold actually flows through to find().
"""

from __future__ import annotations

import pytest

import touchpoint as tp
from touchpoint.core.types import State
from tests.conftest import skip_without_backend


# -----------------------------------------------------------------------
# Setting individual keys
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestConfigureKeys:
    """Test setting each valid config key."""

    def test_fuzzy_threshold(self):
        tp.configure(fuzzy_threshold=0.8)
        assert tp._config["fuzzy_threshold"] == 0.8

    def test_fallback_input(self):
        tp.configure(fallback_input=False)
        assert tp._config["fallback_input"] is False

    def test_type_chunk_size(self):
        tp.configure(type_chunk_size=100)
        assert tp._config["type_chunk_size"] == 100

    def test_max_elements(self):
        tp.configure(max_elements=2000)
        assert tp._config["max_elements"] == 2000

    def test_max_depth(self):
        tp.configure(max_depth=5)
        assert tp._config["max_depth"] == 5


# -----------------------------------------------------------------------
# Multi-key and edge cases
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestConfigureBehaviour:
    """Behavioural tests for configure()."""

    def test_multiple_keys(self):
        tp.configure(fuzzy_threshold=0.8, fallback_input=False)
        assert tp._config["fuzzy_threshold"] == 0.8
        assert tp._config["fallback_input"] is False

    def test_invalid_key(self):
        with pytest.raises(ValueError, match="unknown config key"):
            tp.configure(nonexistent_key=42)

    def test_preserves_other_keys(self):
        original_fallback = tp._config["fallback_input"]
        tp.configure(fuzzy_threshold=0.9)
        assert tp._config["fallback_input"] == original_fallback


# -----------------------------------------------------------------------
# Boundary values
# -----------------------------------------------------------------------

@pytest.mark.unit
class TestConfigureBoundaries:
    """Boundary-value tests for config keys."""

    def test_fuzzy_threshold_zero(self):
        """0.0 is a valid threshold (loosest — any fuzzy score passes)."""
        tp.configure(fuzzy_threshold=0.0)
        assert tp._config["fuzzy_threshold"] == 0.0

    def test_fuzzy_threshold_one(self):
        """1.0 is a valid threshold (strictest — only perfect matches)."""
        tp.configure(fuzzy_threshold=1.0)
        assert tp._config["fuzzy_threshold"] == 1.0

    def test_type_chunk_size_zero(self):
        """0 disables chunking (no splitting of typed text)."""
        tp.configure(type_chunk_size=0)
        assert tp._config["type_chunk_size"] == 0

    def test_fuzzy_threshold_negative_raises(self):
        """Negative fuzzy_threshold raises ValueError."""
        with pytest.raises(ValueError, match="fuzzy_threshold"):
            tp.configure(fuzzy_threshold=-0.1)

    def test_fuzzy_threshold_above_one_raises(self):
        """fuzzy_threshold > 1.0 raises ValueError."""
        with pytest.raises(ValueError, match="fuzzy_threshold"):
            tp.configure(fuzzy_threshold=1.1)

    def test_fuzzy_threshold_wrong_type_raises(self):
        """Non-numeric fuzzy_threshold raises ValueError."""
        with pytest.raises(ValueError, match="fuzzy_threshold"):
            tp.configure(fuzzy_threshold="high")

    def test_fuzzy_threshold_bool_raises(self):
        """bool is not accepted for fuzzy_threshold."""
        with pytest.raises(ValueError, match="fuzzy_threshold"):
            tp.configure(fuzzy_threshold=True)

    def test_type_chunk_size_negative_raises(self):
        """Negative type_chunk_size raises ValueError."""
        with pytest.raises(ValueError, match="type_chunk_size"):
            tp.configure(type_chunk_size=-1)

    def test_type_chunk_size_float_raises(self):
        """Float type_chunk_size raises ValueError."""
        with pytest.raises(ValueError, match="type_chunk_size"):
            tp.configure(type_chunk_size=1.5)

    def test_type_chunk_size_bool_raises(self):
        """bool is not accepted for type_chunk_size."""
        with pytest.raises(ValueError, match="type_chunk_size"):
            tp.configure(type_chunk_size=True)

    def test_fallback_input_int_raises(self):
        """int is not accepted for fallback_input."""
        with pytest.raises(ValueError, match="fallback_input"):
            tp.configure(fallback_input=1)

    def test_fallback_input_string_raises(self):
        """String is not accepted for fallback_input."""
        with pytest.raises(ValueError, match="fallback_input"):
            tp.configure(fallback_input="yes")

    # -- max_elements boundaries --

    def test_max_elements_one(self):
        """1 is the minimum valid value."""
        tp.configure(max_elements=1)
        assert tp._config["max_elements"] == 1

    def test_max_elements_zero_raises(self):
        """0 is not a positive integer."""
        with pytest.raises(ValueError, match="max_elements"):
            tp.configure(max_elements=0)

    def test_max_elements_negative_raises(self):
        with pytest.raises(ValueError, match="max_elements"):
            tp.configure(max_elements=-10)

    def test_max_elements_float_raises(self):
        with pytest.raises(ValueError, match="max_elements"):
            tp.configure(max_elements=500.0)

    def test_max_elements_bool_raises(self):
        """bool is not accepted for max_elements."""
        with pytest.raises(ValueError, match="max_elements"):
            tp.configure(max_elements=True)

    def test_max_elements_string_raises(self):
        with pytest.raises(ValueError, match="max_elements"):
            tp.configure(max_elements="5000")

    # -- max_depth boundaries --

    def test_max_depth_one(self):
        """1 is the minimum valid value."""
        tp.configure(max_depth=1)
        assert tp._config["max_depth"] == 1

    def test_max_depth_zero_accepted(self):
        """0 is valid — returns only immediate children."""
        tp.configure(max_depth=0)
        assert tp._config["max_depth"] == 0

    def test_max_depth_negative_raises(self):
        with pytest.raises(ValueError, match="max_depth"):
            tp.configure(max_depth=-1)

    def test_max_depth_float_raises(self):
        with pytest.raises(ValueError, match="max_depth"):
            tp.configure(max_depth=5.5)

    def test_max_depth_bool_raises(self):
        """bool is not accepted for max_depth."""
        with pytest.raises(ValueError, match="max_depth"):
            tp.configure(max_depth=True)

    def test_max_depth_string_raises(self):
        with pytest.raises(ValueError, match="max_depth"):
            tp.configure(max_depth="10")

    def test_type_chunk_size_invalidates_input_provider(self):
        """Changing type_chunk_size clears _input_provider so the
        next call re-creates it with the new value."""
        # Force provider into existence if possible.
        saved = tp._input_provider
        try:
            tp._input_provider = object()  # sentinel
            tp.configure(type_chunk_size=80)
            assert tp._input_provider is None, (
                "configure(type_chunk_size=...) should invalidate "
                "_input_provider"
            )
        finally:
            tp._input_provider = saved


# -----------------------------------------------------------------------
# Integration — config flows through to behaviour
# -----------------------------------------------------------------------

@pytest.mark.integration
@skip_without_backend
class TestConfigureIntegration:
    """Verify config values actually affect runtime behaviour."""

    def test_fuzzy_threshold_affects_find(self, backend):
        """Strict threshold should return fewer fuzzy matches."""
        # Search all visible apps for a named element ≥ 3 chars.
        wins = backend.windows()
        apps = list(dict.fromkeys(
            w.app for w in wins
            if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        ))
        target = None
        target_app = None
        for app in apps:
            elems = tp.elements(
                app=app, named_only=True,
                states=[State.VISIBLE, State.SHOWING],
            )
            for el in elems:
                if el.name and len(el.name.strip()) >= 4:
                    target = el
                    target_app = app
                    break
            if target:
                break
        if target is None:
            pytest.skip("no named element with ≥4 chars in any app")

        name = target.name
        # Mangle the name to trigger fuzzy (not exact or contains).
        typo = name[0] + "zz" + name[3:] if len(name) > 3 else "zzz"

        # Lenient threshold.
        tp.configure(fuzzy_threshold=0.1)
        lenient = tp.find(typo, app=target_app)

        # Strict threshold.
        tp.configure(fuzzy_threshold=0.99)
        strict = tp.find(typo, app=target_app)

        assert len(strict) <= len(lenient), (
            f"strict threshold (0.99) returned {len(strict)} results "
            f"but lenient (0.1) returned {len(lenient)}"
        )
