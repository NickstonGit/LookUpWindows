from __future__ import annotations


def matches_target(tracked, process_name: str, title: str) -> bool:
    """Strict user-defined match: process AND title substring.

    Kept free of Win32 calls so the rule that decides which windows a tracked
    entry accepts can be unit tested on its own.
    """
    process = (getattr(tracked, "process", "") or "").casefold()
    title_contains = (getattr(tracked, "title_contains", "") or "").casefold()
    if process and (process_name or "").casefold() != process:
        return False
    if title_contains and title_contains not in (title or "").casefold():
        return False
    return True


def preferred_candidate_index(
    candidates: list[tuple[str, str]],
    *,
    title_hint: str = "",
    class_hint: str = "",
) -> int | None:
    """Pick a stable candidate without turning remembered identity into a filter.

    ``candidates`` contains ``(title, class_name)`` pairs that have already
    passed the user's strict process/title filter.  Remembered hints only rank
    those candidates.  If hints are missing or stale, the historical first
    match is returned so old configs keep working.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return 0

    ranked = list(range(len(candidates)))
    normalized_class = (class_hint or "").strip().casefold()
    normalized_title = (title_hint or "").strip().casefold()

    if normalized_class:
        same_class = [
            index
            for index in ranked
            if (candidates[index][1] or "").casefold() == normalized_class
        ]
        if len(same_class) == 1:
            return same_class[0]
        if same_class:
            ranked = same_class

    if normalized_title:
        exact_title = [
            index
            for index in ranked
            if (candidates[index][0] or "").casefold() == normalized_title
        ]
        if exact_title:
            return exact_title[0]

        related_title = [
            index
            for index in ranked
            if normalized_title in (candidates[index][0] or "").casefold()
            or (candidates[index][0] or "").casefold() in normalized_title
        ]
        if len(related_title) == 1:
            return related_title[0]

    return ranked[0]
