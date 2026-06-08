"""Tests for request serialization and health diagnostics."""

from __future__ import annotations

import threading
import time

import pytest

import touchpoint as tp
import touchpoint._state as _st


@pytest.mark.unit
class TestRequestSerialization:
    def test_serialized_is_reentrant(self):
        @_st._serialized
        def inner():
            return "ok"

        @_st._serialized
        def outer():
            return inner()

        assert outer() == "ok"

    def test_serialized_blocks_overlapping_operations(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        @_st._serialized
        def first():
            first_entered.set()
            assert release_first.wait(timeout=1)

        @_st._serialized
        def second():
            second_entered.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        assert first_entered.wait(timeout=1)
        second_thread.start()

        time.sleep(0.05)
        assert not second_entered.is_set()

        release_first.set()
        first_thread.join(timeout=1)
        second_thread.join(timeout=1)
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        assert second_entered.is_set()


@pytest.mark.unit
class TestDiagnostics:
    def test_probe_false_reports_state_without_initializing(self, monkeypatch):
        monkeypatch.setattr(_st, "_backend", None)
        monkeypatch.setattr(_st, "_input_provider", None)
        monkeypatch.setattr(_st, "_cdp_backend", None)
        monkeypatch.setattr(_st, "_cdp_attempted", False)

        report = tp.diagnostics(probe=False)

        assert report["request_serialization"] == "threading.RLock"
        assert report["backend"] == {
            "initialized": False,
            "name": None,
            "available": False,
        }
        assert report["input_provider"] == {
            "initialized": False,
            "name": None,
            "available": False,
        }
        assert report["cdp"] == {
            "attempted": False,
            "initialized": False,
            "owned_pids": [],
            "targets": [],
        }
        assert report["config"]["ax_messaging_timeout"] == 1.0
        assert report["errors"] == []

    def test_backend_health_is_included(self, monkeypatch):
        class Backend:
            def is_available(self):
                return True

            def get_diagnostics(self):
                return {
                    "messaging_timeout_seconds": 0.5,
                    "skipped_apps": [{"pid": 42}],
                }

        monkeypatch.setattr(_st, "_backend", Backend())
        monkeypatch.setattr(_st, "_input_provider", None)
        monkeypatch.setattr(_st, "_cdp_backend", None)

        report = tp.diagnostics(probe=False)

        assert report["backend"]["messaging_timeout_seconds"] == 0.5
        assert report["backend"]["skipped_apps"] == [{"pid": 42}]
