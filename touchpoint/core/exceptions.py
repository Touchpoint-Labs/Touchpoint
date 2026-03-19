"""Custom exceptions for Touchpoint.

Each exception maps to one clear failure mode that a caller
(especially an LLM agent) can handle programmatically.
All exceptions inherit from :class:`TouchpointError`.
"""


class TouchpointError(Exception):
    """Base exception for all Touchpoint errors."""


class BackendUnavailableError(TouchpointError):
    """A required backend is not available on this system.

    Raised when a backend cannot initialise — e.g. AT-SPI2 is missing
    on a non-Linux system, or a CDP debugging port is not open.

    Args:
        backend: Name of the backend that failed (e.g. ``"atspi"``,
            ``"cdp"``).
        reason: Optional human-readable explanation of why the backend
            is unavailable.

    Attributes:
        backend: The backend name.
        reason: The failure reason (empty string if not provided).
    """

    def __init__(self, backend: str, reason: str = "") -> None:
        self.backend = backend
        self.reason = reason
        msg = f"Backend {backend!r} is not available"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)


class ActionFailedError(TouchpointError):
    """An action on an element did not succeed.

    Raised when an accessibility action (click, type, etc.) fails —
    e.g. clicking a disabled button, or typing into a read-only field.

    Args:
        action: The action that was attempted (e.g. ``"click"``,
            ``"type_text"``).
        element_id: The id of the element the action targeted.
        reason: Optional human-readable explanation of why the action
            failed.

    Attributes:
        action: The attempted action name.
        element_id: The target element's id.
        reason: The failure reason (empty string if not provided).
    """

    def __init__(self, action: str, element_id: str, reason: str = "") -> None:
        self.action = action
        self.element_id = element_id
        self.reason = reason
        msg = f"Action {action!r} failed on element {element_id!r}"
        if reason:
            msg += f": {reason}"
        super().__init__(msg)
