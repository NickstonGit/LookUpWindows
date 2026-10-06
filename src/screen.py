"""Pure screen geometry: is a rectangle really visible on a physical monitor?

The Win32 layer answers "where are the monitors", this module answers "does
this rectangle intersect one of them".  Keeping the predicate free of ctypes is
what makes it testable against synthetic multi-monitor layouts: the bounding box
of the virtual screen is *not* the set of visible pixels.  On an L-shaped desk
(top-left monitor plus a second monitor in the upper right corner) there is a gap
inside that bounding box, and a window parked in that gap is invisible to the
user while still "intersecting the virtual screen".

A window counts as accessible only when it covers at least ``min_visible`` pixels
in both dimensions on at least one real monitor.  One monitor is enough: a window
straddling the seam between two neighbours is perfectly usable.
"""

from __future__ import annotations

Rect = tuple[int, int, int, int]


def normalise(rect) -> Rect:
    """Return ``rect`` as ``(left, top, right, bottom)`` with left<=right."""
    left, top, right, bottom = (int(value) for value in rect)
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return (left, top, right, bottom)


def intersection(first, second) -> Rect | None:
    """The overlap of two rectangles, or ``None`` when they do not overlap."""
    a_left, a_top, a_right, a_bottom = normalise(first)
    b_left, b_top, b_right, b_bottom = normalise(second)
    left = max(a_left, b_left)
    top = max(a_top, b_top)
    right = min(a_right, b_right)
    bottom = min(a_bottom, b_bottom)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def visible_pixels(rect, monitors) -> tuple[int, int]:
    """Largest on-screen size of ``rect`` on any single monitor.

    Returns ``(width, height)``.  The virtual-screen bounding box is
    deliberately *not* consulted: it contains gaps on any layout that is not a
    plain grid, and a window inside such a gap is not visible at all.

    This is a statistic, not the accessibility predicate.  Picking the monitor
    with the largest *area* is the wrong question for "can the user use this
    window": a long but very thin overlap can have more area than a small square
    that satisfies both thresholds comfortably, so the maximum area would report
    a perfectly usable window as invisible.  See :func:`is_visible_on_monitors`.
    """
    best = (0, 0)
    for monitor in monitors or ():
        overlap = intersection(rect, monitor)
        if overlap is None:
            continue
        left, top, right, bottom = overlap
        size = (right - left, bottom - top)
        if size[0] * size[1] > best[0] * best[1]:
            best = size
    return best


def is_visible_on_monitors(rect, monitors, min_visible: int = 24) -> bool:
    """True when ``rect`` covers at least ``min_visible`` px on some real monitor.

    The test is per monitor and it is an *existence* test: one monitor meeting
    both thresholds is enough, and which of several qualifying monitors it is
    makes no difference.  Comparing the largest area instead would let the order
    of the monitor list and the shape of the overlaps decide the answer, which is
    how a window with a real 24x24 patch of screen used to be declared off-screen
    and its recovery record left behind forever.
    """
    minimum = max(1, int(min_visible))
    for monitor in monitors or ():
        overlap = intersection(rect, monitor)
        if overlap is None:
            continue
        left, top, right, bottom = overlap
        if (right - left) >= minimum and (bottom - top) >= minimum:
            return True
    return False


def bounding_rect(rects) -> Rect | None:
    """Bounding box of ``rects``, or ``None`` when there are none."""
    items = [normalise(rect) for rect in rects or ()]
    if not items:
        return None
    return (
        min(item[0] for item in items),
        min(item[1] for item in items),
        max(item[2] for item in items),
        max(item[3] for item in items),
    )


def inside_gap(rect, monitors, bounding) -> bool:
    """True when ``rect`` sits in ``bounding`` but on no monitor in it.

    This is the regression the virtual-desktop gap produced: the window is inside
    the virtual screen's bounding box and simultaneously invisible.
    """
    if bounding is not None and intersection(rect, bounding) is None:
        return False
    return intersection_any(rect, monitors) is None


def intersection_any(rect, monitors) -> Rect | None:
    for monitor in monitors or ():
        overlap = intersection(rect, monitor)
        if overlap is not None:
            return overlap
    return None
