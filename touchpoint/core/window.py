"""Core data model for windows.

Defines the :class:`Window` dataclass — returned by ``tp.windows()``
and used to scope element queries to a specific window.

Example::

    >>> windows = tp.windows()
    >>> windows[0]
    Window('Touchpoint — VS Code', app='Code')
    >>> windows[0].size
    (1920, 1040)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Window:
    """A top-level OS window.

    Backends discover windows via the accessibility tree (AT-SPI2) or
    the window manager, and return them as ``Window`` instances.  These
    are simpler than :class:`~touchpoint.core.element.Element` — they
    represent the container, not individual UI controls.

    Args:
        id: Session-unique identifier assigned by the backend.  Used
            to scope element queries (e.g. ``tp.elements(window_id=w.id)``).
        title: The window's title bar text (e.g. ``"untitled — Kate"``).
            May be empty for utility windows.
        app: Name of the application that owns this window.
        pid: OS process id of the owning application.
        position: ``(x, y)`` screen coordinates of the window's
            **top-left** corner, in pixels.  Note: this differs from
            :attr:`Element.position <touchpoint.core.element.Element.position>`,
            which is the **center** of the bounding box.
        size: ``(width, height)`` of the window, in pixels.
        is_active: Whether this is the currently focused / foreground
            window.
        is_visible: Whether the window is visible (not minimised or
            fully occluded).
        raw: Backend-specific extra data that doesn't fit the unified
            schema.  Contents vary by backend.  Empty dict when the
            backend has nothing extra.  Not included in ``__str__``
            output — only surfaced in JSON format.

    Attributes:
        All constructor args are stored as public attributes.

    Example::

        >>> w = tp.windows()[0]
        >>> w.title
        'Touchpoint — VS Code'
        >>> w.is_active
        True
    """

    id: str
    title: str
    app: str
    pid: int
    position: tuple[int, int]
    size: tuple[int, int]
    is_active: bool
    is_visible: bool
    raw: dict = field(default_factory=dict)

    # -- Display ----------------------------------------------------------

    def __repr__(self) -> str:
        """Concise repr for debugging and REPL use.

        Returns:
            String like ``Window('untitled — Kate', app='Kate')``.
        """
        return f"Window({self.title!r}, app={self.app!r})"

    def __str__(self) -> str:
        """Human-readable one-liner for quick inspection.

        Returns:
            String like
            ``[atspi:3:0] 'untitled — Kate' (1920x1040) app=Kate [active]``.
        """
        w, h = self.size
        flags = []
        if self.is_active:
            flags.append("active")
        if not self.is_visible:
            flags.append("hidden")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"[{self.id}] {self.title!r} ({w}x{h}) app={self.app}{suffix}"
