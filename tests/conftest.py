"""Shared fixtures, auto-detection, and helpers for Touchpoint tests.

Automatically detects the platform, display server, backend
availability, and input provider availability.  All integration
fixtures skip gracefully when the required infrastructure is
missing — your partner on Windows sees different apps but the
same tests pass.

Markers
-------
- ``unit``         — pure logic, no OS or desktop needed
- ``integration``  — needs a live desktop + backend
- ``realistic``    — needs apps open, opt-in via ``-m realistic``
- ``slow``         — takes >5 s
- ``destructive``  — modifies UI state (types, clicks)

Run examples::

    pytest tests/                          # everything available
    pytest tests/ -m unit                  # logic-only, works in CI
    pytest tests/ -m "not destructive"     # read-only validation
    pytest tests/ -m realistic             # agent-style workflows
"""

from __future__ import annotations

import os
import sys

import pytest

import touchpoint as tp
from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window


# -----------------------------------------------------------------------
# Config isolation — shared across all test files
# -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _restore_config():
    """Save config before each test and restore it after."""
    original = tp._config.copy()
    original_cdp = tp._cdp_backend
    original_cdp_attempted = tp._cdp_attempted
    original_input = tp._input_provider
    yield
    tp._config.update(original)
    tp._cdp_backend = original_cdp
    tp._cdp_attempted = original_cdp_attempted
    tp._input_provider = original_input


# -----------------------------------------------------------------------
# Platform & session detection
# -----------------------------------------------------------------------

def _detect_platform() -> str:
    """Return ``'linux'``, ``'windows'``, ``'macos'``, or ``'unknown'``."""
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "unknown"


def _detect_session_type() -> str:
    """Return ``'x11'``, ``'wayland'``, or ``'unknown'``.

    Only meaningful on Linux.  On other platforms returns
    ``'unknown'``.
    """
    if not sys.platform.startswith("linux"):
        return "unknown"
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session in ("x11", "wayland"):
        return session
    # Fallback: WAYLAND_DISPLAY is set when a Wayland compositor
    # is running.  DISPLAY is set for X11.
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


PLATFORM: str = _detect_platform()
"""Current platform: ``'linux'``, ``'windows'``, ``'macos'``, ``'unknown'``."""

SESSION_TYPE: str = _detect_session_type()
"""Display server on Linux: ``'x11'``, ``'wayland'``, ``'unknown'``."""


def _check_backend() -> bool:
    """Return ``True`` if a backend can be initialised."""
    try:
        tp._get_backend()
        return True
    except Exception:
        return False


def _check_input() -> bool:
    """Return ``True`` if an input provider can be initialised."""
    try:
        tp._get_input()
        return True
    except Exception:
        return False


HAS_BACKEND: bool = _check_backend()
"""Whether an accessibility backend is available."""

HAS_INPUT: bool = _check_input()
"""Whether a raw input provider (xdotool, etc.) is available."""

TEST_APP: str | None = os.environ.get("TOUCHPOINT_TEST_APP") or None
"""App name for destructive tests.  Set ``TOUCHPOINT_TEST_APP=Mousepad`` etc."""

CDP_PORT: int | None = (
    int(os.environ["TOUCHPOINT_CDP_PORT"])
    if os.environ.get("TOUCHPOINT_CDP_PORT")
    else None
)
"""CDP debugging port.  Set ``TOUCHPOINT_CDP_PORT=9222`` to run CDP tests."""

CDP_APP: str | None = os.environ.get("TOUCHPOINT_CDP_APP") or None
"""CDP app name override.  Auto-detected from port if not set."""


# -----------------------------------------------------------------------
# Marker registration
# -----------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so ``pytest --strict-markers`` works."""
    config.addinivalue_line("markers", "unit: pure logic, no OS needed")
    config.addinivalue_line("markers", "integration: needs live desktop")
    config.addinivalue_line("markers", "realistic: agent-style workflows, opt-in")
    config.addinivalue_line("markers", "slow: takes >5 s")
    config.addinivalue_line("markers", "destructive: modifies UI state")
    config.addinivalue_line("markers", "cdp: requires a CDP app (set TOUCHPOINT_CDP_PORT)")


# -----------------------------------------------------------------------
# Skip helpers
# -----------------------------------------------------------------------

skip_without_backend = pytest.mark.skipif(
    not HAS_BACKEND, reason="no accessibility backend available",
)

skip_without_input = pytest.mark.skipif(
    not HAS_INPUT, reason="no input provider available",
)

skip_unless_x11 = pytest.mark.skipif(
    SESSION_TYPE != "x11", reason="requires X11 session",
)

skip_unless_wayland = pytest.mark.skipif(
    SESSION_TYPE != "wayland", reason="requires Wayland session",
)

skip_unless_linux = pytest.mark.skipif(
    PLATFORM != "linux", reason="requires Linux",
)

skip_unless_windows = pytest.mark.skipif(
    PLATFORM != "windows", reason="requires Windows",
)

skip_without_test_app = pytest.mark.skipif(
    TEST_APP is None,
    reason=(
        "destructive test requires TOUCHPOINT_TEST_APP env var "
        "(e.g. TOUCHPOINT_TEST_APP=Mousepad)"
    ),
)

skip_without_cdp = pytest.mark.skipif(
    CDP_PORT is None,
    reason=(
        "CDP test requires TOUCHPOINT_CDP_PORT env var "
        "(e.g. TOUCHPOINT_CDP_PORT=9222)"
    ),
)


# -----------------------------------------------------------------------
# Assertion helpers
# -----------------------------------------------------------------------

def assert_valid_element(el: Element) -> None:
    """Assert that an Element has well-formed fields.

    Checks structural correctness — does not validate semantic
    content (e.g. whether the name makes sense).
    """
    assert el.id is not None and isinstance(el.id, str) and el.id != ""
    assert isinstance(el.name, str)  # may be empty, that's ok
    assert isinstance(el.role, Role)
    assert isinstance(el.states, list)
    assert all(isinstance(s, State) for s in el.states)
    assert isinstance(el.position, tuple) and len(el.position) == 2
    assert isinstance(el.size, tuple) and len(el.size) == 2
    assert isinstance(el.app, str)
    assert isinstance(el.pid, int)
    assert isinstance(el.backend, str) and el.backend != ""


def assert_valid_window(win: Window) -> None:
    """Assert that a Window has well-formed fields."""
    assert win.id is not None and isinstance(win.id, str) and win.id != ""
    assert isinstance(win.title, str)
    assert isinstance(win.app, str)
    assert isinstance(win.pid, int)
    assert isinstance(win.position, tuple) and len(win.position) == 2
    assert isinstance(win.size, tuple) and len(win.size) == 2
    assert isinstance(win.is_active, bool)
    assert isinstance(win.is_visible, bool)


# -----------------------------------------------------------------------
# Fixtures — backend / input
# -----------------------------------------------------------------------

@pytest.fixture
def backend():
    """Provide the ``tp`` module with an active backend.

    Skips the test if no backend is available.
    """
    if not HAS_BACKEND:
        pytest.skip("no accessibility backend available")
    return tp


@pytest.fixture
def input_provider():
    """Provide the ``tp`` module with an active input provider.

    Skips the test if no input provider is available.
    """
    if not HAS_INPUT:
        pytest.skip("no input provider available")
    return tp


# -----------------------------------------------------------------------
# Fixtures — destructive target app
# -----------------------------------------------------------------------

@pytest.fixture
def destructive_app(backend) -> str:
    """Return the app name set by ``TOUCHPOINT_TEST_APP``.

    Skips if the env var is not set or the app has no visible window.
    Usage::

        TOUCHPOINT_TEST_APP=Mousepad pytest -m destructive
    """
    if TEST_APP is None:
        pytest.skip(
            "set TOUCHPOINT_TEST_APP to run destructive tests "
            "(e.g. TOUCHPOINT_TEST_APP=Mousepad)"
        )
    wins = backend.windows()
    test_app_lower = TEST_APP.lower()
    matched_app = None
    for w in wins:
        if w.app.lower() == test_app_lower and w.is_visible:
            matched_app = w.app  # use the actual casing from AT-SPI
            break
    if matched_app is None:
        pytest.skip(f"{TEST_APP!r} has no visible window")
    return matched_app


# -----------------------------------------------------------------------
# Fixtures — "any" live targets
# -----------------------------------------------------------------------
# These pick real, visible, interactive elements from whatever apps
# are running.  No hardcoded app names — works on any desktop.
# -----------------------------------------------------------------------

@pytest.fixture
def any_app(backend) -> str:
    """Return the name of a running app that has a visible window.

    Skips if no app with a visible window is found.
    """
    wins = backend.windows()
    visible_apps = {
        w.app for w in wins
        if w.is_visible and w.size[0] > 0 and w.size[1] > 0
    }
    if not visible_apps:
        pytest.skip("no app with a visible window found")
    return next(iter(visible_apps))


@pytest.fixture
def any_window(backend) -> Window:
    """Return a visible, non-zero-size window.

    Skips if none found.
    """
    wins = backend.windows()
    for w in wins:
        if w.is_visible and w.size[0] > 0 and w.size[1] > 0:
            return w
    pytest.skip("no visible window found")


@pytest.fixture
def any_element(backend, any_app) -> Element:
    """Return a visible, named element from any running app.

    The element has VISIBLE + SHOWING states, a non-empty name,
    and a real on-screen size.  Searches all visible apps if
    the primary app has no match.  Skips if none found.
    """
    wins = backend.windows()
    apps_to_try = [any_app] + [
        w.app for w in wins
        if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        and w.app != any_app
    ]
    for app in dict.fromkeys(apps_to_try):  # dedupe, preserve order
        elems = backend.elements(
            app=app,
            named_only=True,
            states=[State.VISIBLE, State.SHOWING],
        )
        for el in elems:
            if el.name and el.name.strip() and el.size[0] > 0 and el.size[1] > 0:
                return el
    pytest.skip("no visible named element found in any app")


@pytest.fixture
def any_button(backend) -> Element:
    """Return a visible, enabled, clickable button.

    Skips if none found across all apps.
    """
    elems = backend.elements(
        role=Role.BUTTON,
        states=[State.VISIBLE, State.SHOWING, State.ENABLED, State.SENSITIVE],
        named_only=True,
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    pytest.skip("no visible enabled button found")


@pytest.fixture
def any_text_field(backend) -> Element:
    """Return a visible, editable text field.

    Looks for TEXT_FIELD with VISIBLE + SHOWING + EDITABLE states.
    Skips if none found across all apps.
    """
    elems = backend.elements(
        role=Role.TEXT_FIELD,
        states=[State.VISIBLE, State.SHOWING, State.EDITABLE],
    )
    for el in elems:
        if el.size[0] > 0 and el.size[1] > 0:
            return el
    pytest.skip("no visible editable text field found")


# -----------------------------------------------------------------------
# Fixtures — CDP backend
# -----------------------------------------------------------------------
# Activated by setting  TOUCHPOINT_CDP_PORT=<port>.
# Optionally  TOUCHPOINT_CDP_APP=<name>  to override the auto-detected
# process name.
# -----------------------------------------------------------------------

@pytest.fixture
def cdp_backend():
    """Provide the ``tp`` module configured for CDP testing.

    Configures a CDP port from ``TOUCHPOINT_CDP_PORT``, connects,
    and returns ``tp``.  Skips if the env var is missing or the
    connection fails.
    """
    if CDP_PORT is None:
        pytest.skip("set TOUCHPOINT_CDP_PORT to run CDP tests")

    tp.configure(cdp_ports={"cdp_test": CDP_PORT})

    cdp = tp._get_cdp()
    if cdp is None:
        pytest.skip("CDP backend unavailable (websocket-client missing?)")

    # Verify we can actually reach the port.
    if not cdp._connections:
        pytest.skip(f"CDP connection to port {CDP_PORT} failed")

    return tp


@pytest.fixture
def cdp_app(cdp_backend) -> str:
    """Return the CDP app name to scope queries.

    Uses ``TOUCHPOINT_CDP_APP`` if set, otherwise auto-detects from
    the CDP connection's process name.
    """
    if CDP_APP:
        return CDP_APP

    cdp = tp._get_cdp()
    if cdp is None:
        pytest.skip("CDP backend unavailable")

    # Auto-detect: grab the first app name from CDP connections.
    for pid, name in getattr(cdp, "_pid_names", {}).items():
        if name and name != "unknown":
            return name

    # Fallback: use "cdp_test" (the configured name).
    return "cdp_test"


@pytest.fixture
def cdp_window(cdp_backend) -> Window:
    """Return the first visible CDP window.

    Skips if no CDP page targets are available.
    """
    wins = tp.windows()
    for w in wins:
        if w.id.startswith("cdp:"):
            return w
    pytest.skip("no CDP window found")


@pytest.fixture
def cdp_element(cdp_backend, cdp_app) -> Element:
    """Return a named, visible CDP element.

    Ensures it has the ``cdp:`` ID prefix and non-zero size.
    """
    elems = tp.elements(
        app=cdp_app,
        named_only=True,
        states=[State.VISIBLE, State.SHOWING],
    )
    for el in elems:
        if (
            el.id.startswith("cdp:")
            and el.name
            and el.name.strip()
            and el.size[0] > 0
            and el.size[1] > 0
        ):
            return el
    pytest.skip("no visible named CDP element found")
