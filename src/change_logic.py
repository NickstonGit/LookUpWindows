from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChangeResult:
    status: str
    score: float | None = None
    changed_fraction: float = 0.0


class GridComparator:
    """Pure state machine for comparing luminance grids.

    Capture failures and near-black frames are not valid observations.  They
    invalidate the previous baseline so the first healthy frame after a capture
    problem becomes a fresh baseline rather than a false change notification.
    """

    def __init__(self, *, cell_delta_threshold: int = 24, blank_max_value: int = 2):
        self.cell_delta_threshold = max(0, min(255, int(cell_delta_threshold)))
        self.blank_max_value = max(0, min(255, int(blank_max_value)))
        self._last: dict[int, bytes] = {}

    def compare(self, hwnd: int, grid: bytes | None) -> ChangeResult:
        hwnd = int(hwnd)
        if grid is None:
            self.forget(hwnd)
            return ChangeResult(status="capture_failed")
        if grid and max(grid) <= self.blank_max_value:
            self.forget(hwnd)
            return ChangeResult(status="blank_frame")

        previous = self._last.get(hwnd)
        self._last[hwnd] = grid
        if previous is None or len(previous) != len(grid):
            return ChangeResult(status="baseline")
        if not grid:
            return ChangeResult(status="ok", score=0.0, changed_fraction=0.0)

        total = 0
        changed_cells = 0
        for before, after in zip(previous, grid, strict=True):
            delta = abs(before - after)
            total += delta
            if delta >= self.cell_delta_threshold:
                changed_cells += 1
        return ChangeResult(
            status="ok",
            score=total / (len(grid) * 255),
            changed_fraction=changed_cells / len(grid),
        )

    def forget(self, hwnd: int) -> None:
        self._last.pop(int(hwnd), None)

    def clear(self) -> None:
        self._last.clear()
