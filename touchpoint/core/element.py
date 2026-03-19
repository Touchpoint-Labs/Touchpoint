"""Core data model for UI elements.

Defines the :class:`Element` dataclass — the central type returned by
almost every Touchpoint API call.  Every backend (AT-SPI2, CDP, Windows
UIA, macOS AX) constructs ``Element`` instances, translating their
native data into this unified shape.

Example::

    >>> element.name
    'Send'
    >>> element.role
    <Role.BUTTON: 'button'>
    >>> element.position
    (512, 340)
    >>> element.states
    [<State.VISIBLE: 'visible'>, <State.ENABLED: 'enabled'>]
"""

from __future__ import annotations

from dataclasses import dataclass, field

from touchpoint.core.types import Role, State


@dataclass
class Element:
    """A single UI element on screen.

    This is the primary data object in Touchpoint.  Backends populate
    these from the accessibility tree (or CDP DOM), and the public API
    returns them to the caller.

    Args:
        id: Session-unique identifier assigned by the backend.  Not
            stable across runs — used for action targeting within a
            single session.
        name: Human-readable label of the element (e.g. ``"Send"``,
            ``"File"``, ``"Close"``).  May be empty for decorative
            elements.
        role: Unified role from :class:`~touchpoint.core.types.Role`.
        states: List of active states from
            :class:`~touchpoint.core.types.State`.  An element
            typically has several simultaneously (e.g. ``VISIBLE``,
            ``ENABLED``, ``FOCUSED``).
        position: ``(x, y)`` screen coordinates of the element's
            **center point**, in pixels.  Note: this differs from
            :attr:`Window.position <touchpoint.core.window.Window.position>`,
            which is the **top-left** corner.
        size: ``(width, height)`` of the element's bounding box,
            in pixels.
        actions: List of accessibility actions the element supports
            (e.g. ``["click", "press", "activate"]``).  These are
            the raw action names from the backend.
        value: Current value for elements that hold one — text field
            contents, slider position, toggle state, etc.  ``None``
            when not applicable.
        description: Accessibility description / help text if the
            backend provides one.  ``None`` when absent.
        children: Child elements in the accessibility tree.  **Empty
            by default**.  Only populated when the user
            requests ``tree=True``.
        parent_id: The ``id`` of this element's parent in the tree.
            ``None`` for top-level elements.
        window_id: The id of the top-level window this element
            belongs to, e.g. ``"atspi:2269:1"``.  Useful for scoping
            subsequent queries to the same window via
            ``tp.find(..., window_id=el.window_id)``.
        app: Name of the application that owns this element
            (e.g. ``"Firefox"``, ``"Slack"``).
        pid: OS process id of the owning application.
        backend: Which backend produced this element — ``"atspi"``,
            ``"cdp"``, etc.  Useful for debugging and for routing
            actions back to the correct backend.
        raw_role: The original role string from the backend *before*
            mapping to :class:`Role`.  Preserved even when the role
            maps to :attr:`Role.UNKNOWN`, so no information is lost.
        raw: Backend-specific extra data that doesn't fit the unified
            schema.  Contents vary by backend — for AT-SPI2 this
            includes the accessible's ``attributes`` dict (toolkit
            name, widget class, etc.).  Empty dict when the backend
            has nothing extra.  Not included in ``__str__`` or flat/
            tree output — only surfaced in JSON format.

    Attributes:
        All constructor args are stored as public attributes.

    Example::

        >>> el = tp.find("Send", role=tp.Role.BUTTON)[0].element
        >>> el.name
        'Send'
        >>> el.position
        (512, 340)
        >>> State.ENABLED in el.states
        True
    """

    id: str
    name: str
    role: Role
    states: list[State]
    position: tuple[int, int]
    size: tuple[int, int]
    app: str
    pid: int
    backend: str
    raw_role: str
    actions: list[str] = field(default_factory=list)
    value: str | None = None
    description: str | None = None
    children: list[Element] = field(default_factory=list)
    parent_id: str | None = None
    window_id: str | None = None
    raw: dict = field(default_factory=dict)

    # -- Convenience properties -------------------------------------------

    @property
    def bounds(self) -> tuple[int, int, int, int]:
        """Bounding rectangle as ``(left, top, width, height)``.

        Computes the top-left corner from the center-based
        :attr:`position` and :attr:`size`.

        Returns:
            ``(left, top, width, height)`` in screen pixels.
        """
        cx, cy = self.position
        w, h = self.size
        return (cx - w // 2, cy - h // 2, w, h)

    @property
    def is_enabled(self) -> bool:
        """Whether the element is interactive.

        Returns:
            ``True`` if :attr:`State.ENABLED` is in :attr:`states`.
        """
        return State.ENABLED in self.states

    @property
    def is_focused(self) -> bool:
        """Whether the element currently has keyboard focus.

        Returns:
            ``True`` if :attr:`State.FOCUSED` is in :attr:`states`.
        """
        return State.FOCUSED in self.states

    @property
    def is_visible(self) -> bool:
        """Whether the element is visible on screen.

        Returns:
            ``True`` if :attr:`State.VISIBLE` is in :attr:`states`.
        """
        return State.VISIBLE in self.states

    # -- Display ----------------------------------------------------------

    def __repr__(self) -> str:
        """Concise repr for debugging and REPL use.

        Returns:
            String like ``Element('Send', role=button, app='Slack')``.
        """
        return (
            f"Element({self.name!r}, role={self.role.value}, "
            f"app={self.app!r})"
        )

    def __str__(self) -> str:
        """Human-readable one-liner for quick inspection.

        Includes ``id``, ``role``, ``name``, ``position``, ``size``,
        ``app``, and conditionally ``states``, ``value``, and
        ``actions`` when present.

        Returns:
            String like
            ``[atspi:2269:1:2.1] [button] 'Send' (512, 340) 80×32 app=Slack states=[visible, enabled] actions=[click]``.
        """
        w, h = self.size
        parts = [
            f"[{self.id}]",
            f"[{self.role.value}]",
            f"{self.name!r}",
            f"{self.position}",
            f"{w}\u00d7{h}",
            f"app={self.app}",
        ]
        if self.window_id is not None:
            parts.append(f"window={self.window_id}")
        if self.states:
            parts.append(f"states=[{', '.join(s.value for s in self.states)}]")
        if self.value is not None:
            parts.append(f"value={self.value!r}")
        if self.actions:
            parts.append(f"actions=[{', '.join(self.actions)}]")
        return " ".join(parts)
