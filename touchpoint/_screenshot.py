"""Screenshot capture — platform framebuffer and CDP paths."""

from __future__ import annotations

from typing import Any

from touchpoint.core.element import Element
from touchpoint.core.window import Window
from touchpoint._state import (
    _backend_for_id,
    _get_cdp,
    _is_cdp_app,
    _is_cdp_id,
    _png_bytes_to_image,
    _serialized,
)


@_serialized
def screenshot(
    *,
    app: str | None = None,
    window_id: str | None = None,
    element: Element | str | None = None,
    padding: int = 0,
    monitor: int | None = None,
) -> Any:
    """Capture screen pixels and return a ``PIL.Image.Image``.

    With no arguments, captures the entire virtual desktop (all
    monitors).  Use the optional parameters to crop to a specific
    region.

    Only one of *app*, *window_id*, *element*, or *monitor* may
    be specified.
    """
    from touchpoint.utils.screenshot import take_screenshot

    if padding < 0:
        raise ValueError(
            f"padding must be non-negative, got {padding}"
        )

    scope_params = {
        "app": app, "window_id": window_id,
        "element": element, "monitor": monitor,
    }
    provided = [k for k, v in scope_params.items() if v is not None]
    if len(provided) > 1:
        raise ValueError(
            f"screenshot() accepts at most one scope parameter, "
            f"got: {', '.join(provided)}"
        )

    region: tuple[int, int, int, int] | None = None

    # --- CDP-native screenshot path ---

    if element is not None:
        eid = element.id if isinstance(element, Element) else element
        if _is_cdp_id(eid):
            cdp = _get_cdp()
            if cdp is not None:
                png_bytes = cdp.take_screenshot(
                    element_id=eid, padding=padding,
                )
                return _png_bytes_to_image(png_bytes)

    if window_id is not None and _is_cdp_id(window_id):
        cdp = _get_cdp()
        if cdp is not None:
            parts = window_id.split(":", 3)
            port = int(parts[1])
            tid = parts[2]
            png_bytes = cdp.take_screenshot(port=port, target_id=tid)
            return _png_bytes_to_image(png_bytes)

    if app is not None and _is_cdp_app(app):
        cdp = _get_cdp()
        if cdp is not None:
            app_lower = app.lower()
            candidates = [
                w for w in cdp.get_windows()
                if w.app.lower() == app_lower
            ]
            best = None
            for w in candidates:
                if w.is_active:
                    best = w
                    break
            if best is None:
                visible = [
                    w for w in candidates
                    if w.is_visible and w.size[0] > 0 and w.size[1] > 0
                ]
                if visible:
                    best = max(visible,
                               key=lambda w: w.size[0] * w.size[1])
            if best is None and candidates:
                best = candidates[0]
            if best is not None:
                parts = best.id.split(":", 3)
                port = int(parts[1])
                tid = parts[2]
                png_bytes = cdp.take_screenshot(
                    port=port, target_id=tid,
                )
                return _png_bytes_to_image(png_bytes)

    # --- Platform screenshot path ---

    if element is not None:
        if isinstance(element, str):
            el = _backend_for_id(element).get_element_by_id(element)
            if el is None:
                raise ValueError(
                    f"element {element!r} not found"
                )
        else:
            el = element

        left, top, w, h = el.bounds
        if w <= 0 or h <= 0:
            raise ValueError(
                f"element {el.name!r} has zero size ({w}x{h}) "
                f"— cannot screenshot"
            )
        region = (left, top, left + w, top + h)

    elif window_id is not None:
        win = find_window(window_id=window_id)
        if win is None:
            raise ValueError(
                f"window {window_id!r} not found"
            )
        ww, wh = win.size
        if ww <= 0 or wh <= 0:
            raise ValueError(
                f"window {window_id!r} has zero size ({ww}x{wh}) "
                f"— cannot screenshot"
            )
        wx, wy = win.position
        region = (wx, wy, wx + ww, wy + wh)

    elif app is not None:
        win = find_window(app=app)
        if win is None:
            raise ValueError(
                f"no window found for app {app!r}"
            )
        ww, wh = win.size
        if ww <= 0 or wh <= 0:
            raise ValueError(
                f"window for app {app!r} has zero size "
                f"({ww}x{wh}) — cannot screenshot"
            )
        wx, wy = win.position
        region = (wx, wy, wx + ww, wy + wh)

    elif monitor is not None:
        from touchpoint.utils.screenshot import get_monitor_regions

        regions = get_monitor_regions()
        if monitor < 0 or monitor >= len(regions):
            raise ValueError(
                f"monitor {monitor} out of range "
                f"(0–{len(regions) - 1})"
            )
        region = regions[monitor]

    # --- Apply padding ---

    if region is not None and padding > 0:
        left, top, right, bottom = region
        region = (
            left - padding,
            top - padding,
            right + padding,
            bottom + padding,
        )

    return take_screenshot(region=region)


@_serialized
def monitor_count() -> int:
    """Return the number of physical monitors detected."""
    from touchpoint.utils.screenshot import get_monitor_regions

    return len(get_monitor_regions())


@_serialized
def find_window(
    *,
    app: str | None = None,
    window_id: str | None = None,
) -> Window | None:
    """Find the best matching window for a screenshot crop or UI action."""
    # Import here to avoid circular import — windows() lives in __init__.py
    import touchpoint as tp

    all_windows = tp.windows()

    if window_id is not None:
        for w in all_windows:
            if w.id == window_id:
                return w
        return None

    if app is not None:
        app_lower = app.lower()
        candidates = [
            w for w in all_windows if w.app.lower() == app_lower
        ]
        if not candidates:
            return None

        for w in candidates:
            if w.is_active:
                return w

        visible = [
            w for w in candidates
            if w.is_visible and w.size[0] > 0 and w.size[1] > 0
        ]
        if visible:
            return max(visible, key=lambda w: w.size[0] * w.size[1])

        return max(candidates, key=lambda w: w.size[0] * w.size[1])

    return None


# Backward compatibility for callers that used the old private re-export.
_find_window = find_window
