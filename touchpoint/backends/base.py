"""Abstract base classes for Touchpoint backends.

Every platform must provide two implementations:

1. :class:`Backend` — structured accessibility tree queries and
   native a11y actions (click via AT-SPI2, set text via UIA, etc.).
2. :class:`InputProvider` — raw OS-level keyboard and mouse
   simulation (xdotool, SendInput, CGEvent, etc.).

The public ``tp.*`` API never calls OS-specific code directly — it
always goes through these interfaces.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from touchpoint.core.element import Element
from touchpoint.core.types import Role, State
from touchpoint.core.window import Window


class Backend(ABC):
    """Abstract interface that every Touchpoint backend must implement.

    A backend is responsible for:

    1. **Discovery** — listing applications, windows, and elements.
    2. **Actions** — performing clicks, key presses, etc. on elements.
    3. **Self-reporting** — indicating whether it can run on the
       current system.

    Touchpoint may hold multiple backends simultaneously and merge
    their results (e.g. AT-SPI2 for native apps + CDP for Electron).

    Subclasses must implement all ``@abstractmethod`` methods.  They
    should raise :class:`~touchpoint.core.exceptions.BackendUnavailableError`
    from ``__init__`` if the required system libraries are missing.

    Example::

        class AtSpiBackend(Backend):
            def is_available(self) -> bool:
                try:
                    import gi
                    gi.require_version("Atspi", "2.0")
                    return True
                except (ImportError, ValueError):
                    return False
            ...
    """

    # -- Discovery --------------------------------------------------------

    @abstractmethod
    def get_applications(self) -> list[str]:
        """List running applications that expose UI elements.

        Returns:
            Application names as reported by the accessibility tree
            (e.g. ``["Firefox", "Konsole", "Slack"]``).  Names are
            not guaranteed to be unique — two Firefox windows both
            report ``"Firefox"``.
        """

    @abstractmethod
    def get_windows(self) -> list[Window]:
        """List all visible windows.

        Returns:
            :class:`~touchpoint.core.window.Window` instances for
            every window the backend can see.
        """

    @abstractmethod
    def get_elements(
        self,
        app: str | None = None,
        window_id: str | None = None,
        tree: bool = False,
        max_depth: int | None = None,
        root_element: str | None = None,
        lightweight: bool = False,
        max_elements: int | None = None,
        role: Role | None = None,
        states: list[State] | None = None,
        named_only: bool = False,
        skip_subtree_roles: set[Role] | None = None,
    ) -> list[Element]:
        """Get UI elements, optionally scoped to an app or window.

        Args:
            app: If provided, only return elements belonging to this
                application name.
            window_id: If provided, only return elements within the
                window with this id.
            tree: If ``True``, populate each element's ``children``
                list to form a tree.  If ``False`` (default), return
                a flat list with empty ``children``.
            max_depth: Maximum depth to walk.  ``0`` returns only
                the immediate children of the root(s), ``1`` returns
                children and grandchildren, and so on.  ``None``
                (default) walks the entire subtree.
            root_element: If provided, start the walk from this
                element id instead of from the window roots.  Useful
                for drilling into a specific container discovered in
                a previous shallow call.
            lightweight: If ``True``, return elements with only
                cheap fields populated (name, role, states).  Use
                :meth:`inflate_element` to fill in the rest for
                specific elements.  Not all backends benefit from
                this — the default ignores it and does a full walk.
            max_elements: Maximum number of elements to collect.
                Prevents hanging on very large UI trees.  ``None``
                uses the backend's built-in default.
            role: If provided, only include elements with this role.
                Backends use this as an early-skip hint during the
                tree walk to avoid building elements that will be
                filtered out.  Children of non-matching elements are
                still visited (a non-matching parent may have
                matching children).  Ignored when ``tree=True``.
            states: If provided, only include elements that have
                **all** of these states.  Same early-skip semantics
                as *role*.  Ignored when ``tree=True``.
            named_only: If ``True``, skip elements with empty or
                whitespace-only names during the walk.  Same
                early-skip semantics as *role*.  Ignored when
                ``tree=True``.
            skip_subtree_roles: If provided, still emit elements
                with these roles but **do not descend** into their
                children.  Used to skip expensive subtrees (e.g.
                ``Role.DOCUMENT`` for web content in browsers) when
                another backend already covers that content.

        Returns:
            List of :class:`~touchpoint.core.element.Element` instances.
        """

    def inflate_element(self, element: Element) -> Element:
        """Inflate a lightweight element into a fully populated one.

        Some backends have high per-element IPC cost (e.g. AT-SPI2
        on Linux makes ~10 D-Bus round-trips per element for
        position, actions, value, etc.).  Those backends can offer a
        lightweight walk that only fetches the cheap fields (name,
        role, states), then use this method to resolve the rest for
        just the elements that matter.

        Not every backend needs this — CDP returns a full JSON blob
        in one call, UIA can batch-fetch via ``CacheRequest``.  The
        default implementation returns the element unchanged, which
        is correct for backends where the full walk is already fast.

        Args:
            element: A lightweight :class:`Element` (or any element).

        Returns:
            A fully populated :class:`Element`, or the same element
            if it was already complete.
        """
        return element

    @abstractmethod
    def get_element_at(self, x: int, y: int) -> Element | None:
        """Get the element at a screen coordinate.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.

        Returns:
            The deepest (most specific) element at ``(x, y)``, or
            ``None`` if nothing is found.
        """

    @abstractmethod
    def get_element_by_id(self, element_id: str) -> Element | None:
        """Retrieve a single element by its id.

        Navigates the accessibility tree to the node identified by
        *element_id* and returns a fresh :class:`Element` with
        current position, states, value, etc.

        This is used by the public API to re-resolve elements from
        their string id — for example, to obtain an up-to-date
        position for coordinate-based fallback input.

        Args:
            element_id: The ``id`` of the target element (as returned
                in :attr:`Element.id`).

        Returns:
            The :class:`Element` if found, ``None`` otherwise.

        Raises:
            ValueError: If *element_id* is structurally malformed
                (not a valid ID format for this backend).
        """

    # -- Actions ----------------------------------------------------------

    ACTION_ALIASES: dict[str, list[str]] = {
        "click": [],
        "double_click": [],
        "right_click": [],
    }
    """Maps convenience action names to backend-specific action strings.

    Each backend overrides this with its own vocabulary.  The
    frontend's ``_try_actions()`` iterates the list, calling
    :meth:`do_action` for each until one succeeds.  An empty
    list means "skip straight to coordinate fallback".
    """

    @abstractmethod
    def do_action(self, element_id: str, action: str) -> bool:
        """Perform an accessibility action on an element.

        Args:
            element_id: The ``id`` of the target element (as returned
                in :attr:`Element.id`).
            action: The action name to invoke (e.g. ``"click"``,
                ``"press"``, ``"activate"``).

        Returns:
            ``True`` if the action was dispatched successfully,
            ``False`` otherwise.

        Raises:
            ActionFailedError: If the action fails in a way that can
                be described (e.g. element is disabled).
        """

    @abstractmethod
    def set_value(self, element_id: str, value: str, replace: bool) -> bool:
        """Set the text value of an editable element.

        Args:
            element_id: The ``id`` of the target element.
            value: The text to write.
            replace: If ``True``, clear the field first and set it
                to *value*.  If ``False``, insert *value* at the
                current cursor position.

        Returns:
            ``True`` if the value was set successfully.

        Raises:
            ActionFailedError: If the element is not editable or
                the operation fails.
        """

    @abstractmethod
    def set_numeric_value(
        self, element_id: str, value: float,
    ) -> bool:
        """Set the numeric value of a range element (slider, spinbox).

        Args:
            element_id: The ``id`` of the target element.
            value: The numeric value to set.

        Returns:
            ``True`` if the value was set successfully.

        Raises:
            ActionFailedError: If the element does not support
                numeric values or the operation fails.
        """

    @abstractmethod
    def focus_element(self, element_id: str) -> bool:
        """Move keyboard focus to an element.

        Backends should use the most reliable mechanism available
        (e.g. ``Component.grab_focus()`` on AT-SPI2,
        ``IUIAutomationElement.SetFocus()`` on Windows UIA,
        ``DOM.focus()`` on CDP).

        Args:
            element_id: The ``id`` of the target element.

        Returns:
            ``True`` if focus was moved.

        Raises:
            ActionFailedError: If the element cannot be found or
                cannot receive focus.
        """

    # -- Text selection ---------------------------------------------------

    @abstractmethod
    def select_text(
        self, element_id: str, start: int, end: int,
    ) -> bool:
        """Select a range of text within an element.

        Backends should use the native text selection mechanism
        available on their platform.

        Args:
            element_id: The ``id`` of the target element.
            start: Start offset (0-based character index).
            end: End offset (exclusive).

        Returns:
            ``True`` if the selection was applied.

        Raises:
            ActionFailedError: If the element does not support text
                selection or the operation fails.
        """

    # -- Window management ------------------------------------------------

    def activate_window(self, window_id: str) -> bool:
        """Bring a window to the foreground.

        Uses the accessibility framework's native mechanism to
        activate (raise and focus) a window.

        Not every backend can do this — the default returns
        ``False``.  Subclasses that support it should override.

        Args:
            window_id: The id of the window to activate (as
                returned in :attr:`Window.id`).

        Returns:
            ``True`` if the window was activated, ``False`` if
            this backend does not support window activation.
        """
        return False

    # -- Ownership / routing ----------------------------------------------

    def get_owned_pids(self) -> set[int]:
        """Return the set of process IDs this backend owns.

        Used by the public API to decide which backend handles a
        given application without reaching into private attributes.

        The default returns an empty set (the backend owns nothing
        exclusively).  Backends that manage a specific set of
        processes (e.g. the CDP backend which attaches to browser
        PIDs) should override and return those PIDs.

        Returns:
            A snapshot set of PIDs, safe to iterate without locking.
        """
        return set()

    def owns_element(self, element_id: str) -> bool:
        """Return ``True`` if *element_id* belongs to this backend.

        Lets the public API route element operations without knowing
        the ID-format conventions of each backend.

        The default checks nothing and returns ``False``.

        Args:
            element_id: An element ID string (as returned by
                :attr:`~touchpoint.core.element.Element.id`).
        """
        return False

    def claims_app(self, app_name: str) -> bool:
        """Return ``True`` if this backend serves *app_name*.

        Used to route ``app``-scoped queries to the correct backend.
        The default returns ``False`` (the backend makes no claim).
        Backends that know they are responsible for a named
        application (e.g. CDP knowing all browser comm-names) should
        override.

        Args:
            app_name: Application name as visible to the user
                (e.g. ``"Google Chrome"``, ``"Slack"``).
        """
        return False

    def get_topmost_pid_at(self, x: int, y: int) -> int | None:
        """Return the PID of the topmost window at ``(x, y)``.

        Backends that can answer this question accurately (e.g. via
        compositor stacking APIs or OS window-manager queries) should
        override.  The default falls back to :meth:`get_element_at`
        as a best-effort heuristic.

        Args:
            x: Horizontal screen coordinate (physical pixels).
            y: Vertical screen coordinate (physical pixels).

        Returns:
            A PID, or ``None`` if unknown.
        """
        try:
            el = self.get_element_at(x, y)
            if el is not None and el.pid:
                return el.pid
        except Exception:
            pass
        return None

    def set_pid_display_names(self, mapping: dict[int, str]) -> None:
        """Inject platform PID-to-display-name mapping.

        In multi-backend setups the public API knows each PID's
        user-visible display name (e.g. ``"Google Chrome"``) from
        the platform backend.  By passing that mapping here, a
        secondary backend can match user-supplied display names to
        its own PIDs without calling back into the public API.

        The default does nothing.  Backends that need cross-backend
        name resolution (e.g. CDP) should override.

        Args:
            mapping: ``{pid: display_name}`` snapshot.
        """

    # -- Availability -----------------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this backend can run on the current system.

        Called before any other methods.  Should be fast — check for
        library imports and system capabilities, don't do heavy I/O.

        Returns:
            ``True`` if the backend's dependencies are present and
            it is ready to use, ``False`` otherwise.
        """


class InputProvider(ABC):
    """Abstract interface for raw keyboard and mouse simulation.

    Unlike :class:`Backend`, an ``InputProvider`` has **no knowledge
    of elements or the accessibility tree**.  It sends low-level OS
    input events at screen coordinates or to the focused window.

    Touchpoint uses an ``InputProvider`` as a **fallback** when a
    native accessibility action is unavailable (e.g. the toolkit
    doesn't expose a ``click`` action, or a text field lacks an
    ``EditableText`` interface).  The public API bridges the gap:
    it reads the element's position from :attr:`Element.position`,
    then calls the appropriate ``InputProvider`` method.

    Whether fallback is attempted is controlled by the user via
    ``tp.configure(fallback_input=True)`` (the default).

    All methods are **fire-and-forget**: they inject the event into
    the OS input queue and return.  There is no feedback on whether
    the target application actually processed it.  If the underlying
    tool is missing or the call fails, methods raise
    :class:`RuntimeError`.
    """

    # -- Platform shortcuts ------------------------------------------------

    SELECT_ALL_KEYS: tuple[str, ...] = ("ctrl", "a")
    """Keys for select-all on this platform.

    Linux and Windows use ``("ctrl", "a")``.  A future macOS
    provider would override with ``("super", "a")``.
    """

    # -- Keyboard ---------------------------------------------------------

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type a string of characters as keyboard input.

        Sends each character as a key-press / key-release pair.
        Suitable for typing into the currently focused widget.

        Args:
            text: The text to type.  May contain any Unicode
                characters the underlying tool supports.

        Raises:
            RuntimeError: If the input tool is unavailable or
                the operation fails.
        """

    @abstractmethod
    def press_key(self, key: str) -> None:
        """Press and release a single key.

        Args:
            key: A canonical key name (lowercase, LLM-friendly).
                Common values::

                Letters / digits : ``"a"``, ``"A"``, ``"1"``
                Modifiers        : ``"ctrl"``, ``"alt"``, ``"shift"``, ``"super"``
                Navigation       : ``"enter"``, ``"tab"``, ``"escape"``
                                   ``"backspace"``, ``"delete"``
                                   ``"up"``, ``"down"``, ``"left"``, ``"right"``
                                   ``"home"``, ``"end"``, ``"pageup"``, ``"pagedown"``
                Function keys    : ``"f1"`` … ``"f12"``
                Whitespace       : ``"space"``

            Each ``InputProvider`` subclass normalises these to
            its platform's native keysyms via an internal key map.
            Unknown names are passed through as-is.

        Raises:
            RuntimeError: If the key name is unrecognised or the
                input tool is unavailable.
        """

    @abstractmethod
    def hotkey(self, *keys: str) -> None:
        """Press a keyboard combination (e.g. Ctrl+C).

        All *keys* are held down in order, then released in
        reverse order.

        Args:
            keys: Two or more canonical key names.
                Example: ``hotkey("ctrl", "shift", "s")``

        Raises:
            RuntimeError: If any key name is unrecognised or the
                input tool is unavailable.
        """

    # -- Mouse ------------------------------------------------------------

    @abstractmethod
    def click_at(self, x: int, y: int) -> None:
        """Left-click at screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
        """

    @abstractmethod
    def double_click_at(self, x: int, y: int) -> None:
        """Double-click (left button) at screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
        """

    @abstractmethod
    def right_click_at(self, x: int, y: int) -> None:
        """Right-click at screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
        """

    @abstractmethod
    def scroll(
        self,
        x: int | None,
        y: int | None,
        direction: str,
        amount: int = 3,
    ) -> None:
        """Scroll at a screen position.

        Args:
            x: Horizontal pixel coordinate to scroll at.
                ``None`` scrolls at the current cursor position.
            y: Vertical pixel coordinate to scroll at.
                ``None`` scrolls at the current cursor position.
            direction: One of ``"up"``, ``"down"``, ``"left"``,
                ``"right"``.
            amount: Number of scroll "ticks".  Defaults to ``3``.

        Raises:
            ValueError: If *direction* is not a valid value.
        """

    @abstractmethod
    def mouse_move(self, x: int, y: int) -> None:
        """Move the mouse pointer to screen coordinates.

        Args:
            x: Horizontal pixel coordinate.
            y: Vertical pixel coordinate.
        """

    # -- Window management ------------------------------------------------

    def activate_window(self, title: str, pid: int) -> bool:
        """Bring a window to the foreground using OS-level commands.

        Unlike :meth:`Backend.activate_window` which uses the
        accessibility tree, this uses system tools (e.g. ``xdotool``
        on Linux, ``SetForegroundWindow`` on Windows) to find and
        activate a window by its title and process id.

        Not every input provider can do this — the default returns
        ``False``.  Subclasses that support it should override.

        Args:
            title: The window's title bar text.
            pid: The process id of the application owning the window.

        Returns:
            ``True`` if the window was activated, ``False`` if
            this provider does not support window activation.
        """
        return False

    # -- Availability -----------------------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Check whether this input provider can run.

        Should verify that the underlying tool is installed and
        the display server is reachable.

        Returns:
            ``True`` if ready, ``False`` otherwise.
        """
