"""Durable execution ownership for windows that LookUp parked off-screen.

``recovery.py`` is the *record* of an obligation; this module is its *executor*.
The record alone is not enough: a process that is killed, that loses the UI
thread, or that simply runs out of shutdown deadline stops executing, and a
half-restored foreign window is a hard failure for the user.  Therefore the
obligation to put a window back is handed to a small, separate guardian process
before LookUp exits, and the guardian keeps retrying until the restore is
*verified* or the window is provably gone.

The single invariant this module exists to keep is:

    a live park obligation always has a live executor

The guardian therefore has **no** hard lifetime.  It may only leave when

* the journal holds no obligation it may execute (nothing outstanding, or every
  remaining record belongs to a proven live owner), or
* every obligation it holds was atomically handed to another confirmed
  executor, or
* the journal cannot be read for long enough that its state is honestly unknown.

Retries are bounded and backed off (1s, 2s, 5s, 10s, 30s, 60s, then up to five
minutes) and the log is rate limited, so "keep trying forever" costs no CPU and
no disk.  A hung target therefore costs nothing until it comes back - and when it
does, somebody is still there to put it back.
"""

from __future__ import annotations

import ctypes
import inspect
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from recovery import (  # noqa: E402  (after sys.path bootstrap)
    CLAIM_LEASE_SEC,
    DAMAGED_JOURNAL_STATUSES,
    Claim,
    ExecutorIdentity,
    JournalReadError,
    ParkRecord,
    RecoveryJournal,
    _as_executor,
    journal_identity,
    new_executor_identity,
    quarantined_entries,
    salvage_record,
    state_from_record,
)

logger = logging.getLogger("lookupwindows")

GUARDIAN_ARG = "--recovery-guardian"
# The mutex is named after the *journal*, not after the session.  The journal
# depends on ``--config``, on portable mode and on where the application lives,
# so a session-wide mutex let a guardian for one document announce "delegated"
# for a completely different one - and the main process then exited, happy with
# a confirmation that named nobody who could restore its window.
GUARDIAN_MUTEX_NAME = r"Local\LookUpWindows-RecoveryGuardian"
# Announced on stdout once this process is confirmed as *an* executor: either it
# owns the singleton mutex of this journal itself, or it proved that another
# guardian already executes this very journal.
GUARDIAN_READY_TOKEN = b"ready"
# Announced earlier, by a successor that is still waiting for the mutex its
# predecessor is about to release.  It proves the successor process exists and is
# committed to take over, which is what makes the handover safe.
GUARDIAN_STARTED_TOKEN = b"started"
POLL_SEC = 0.25
RENEW_SEC = max(2.0, CLAIM_LEASE_SEC / 3.0)

# The process that handed the obligations over may still be writing its last
# record (a park worker blocked just before it registers ownership).  The
# guardian therefore stays alive while that process lives and for a short grace
# after it dies, instead of concluding from one empty read that there is
# nothing left to do.
HANDOFF_PARTNER_GRACE_SEC = 10.0
GUARDIAN_IDLE_EXIT_SEC = 0.75
# A guardian that guards a *live* owner (the session guardian every park depends
# on) has nothing to do between obligations for most of the session.  It then
# polls for a park instead of spinning: the journal is a small local file, but a
# busy loop for hours is not free, and nothing is decided faster by looking more
# often.
GUARDIAN_IDLE_POLL_SEC = 1.0

# A journal that cannot be read is an *unknown* state, not an empty one, and the
# executor never leaves because of it: the obligation outlives the read, and the
# only honest exits remain "no obligation" and "a confirmed successor".  What the
# limit below bounds is only the retry frequency, so an unreadable journal costs
# no CPU while it lasts.
JOURNAL_UNREADABLE_POLL_CEILING_SEC = 5.0
# Readiness may not be announced before the journal has been read once under
# control.  A process that confirms readiness and then crashes on its first read
# has confirmed nothing at all.
GUARDIAN_INITIAL_READ_SEC = 10.0

# A pending record is only an *intent*.  It may still turn into a real park
# after the process that issued the move is gone, so it is held until the window
# has been verifiably on screen for a long stretch or the park was undone.
PENDING_SETTLE_SEC = 60.0

# Bounded retry frequency.  The obligation survives every one of these pauses.
BACKOFF_SCHEDULE_SEC = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
BACKOFF_CEILING_SEC = 300.0
# Retry much faster than in production when a runtime gate asks for it: the point
# of those gates is to watch many attempts happen, not to wait hours for them.
TEST_HOOK_FAST_BACKOFF_SEC = (0.25, 0.5, 1.0)

# One repeated failure per record per interval; anything else would turn a target
# that is unresponsive for hours into an unreadable log.
LOG_THROTTLE_SEC = 60.0

STATUS_GONE = "gone"
STATUS_REUSED = "reused"
STATUS_UNVERIFIED = "unverified-identity"
STATUS_PARKED = "parked"
STATUS_VISIBLE = "visible"
STATUS_OFFSCREEN = "offscreen"

STALE_CLAIM = "stale-claim"

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5

# Creation of a successor executor when this one is asked to hand over.
HANDOVER_START_TIMEOUT_SEC = 20.0
HANDOVER_CONFIRM_TIMEOUT_SEC = 30.0
# How long a successor waits for the mutex its predecessor is releasing.
HANDOVER_MUTEX_WAIT_SEC = HANDOVER_START_TIMEOUT_SEC


def winapi():
    """Import the Win32 layer lazily so this module stays importable anywhere."""
    import winapi as module

    return module


def test_hooks() -> set[str]:
    raw = os.environ.get("LOOKUPWINDOWS_TEST_HOOKS", "")
    return {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}


def backoff_schedule() -> tuple[float, ...]:
    if "guardian_fast_backoff" in test_hooks():
        return TEST_HOOK_FAST_BACKOFF_SEC
    return BACKOFF_SCHEDULE_SEC


def backoff_delay(attempt: int) -> float:
    """Sleep length for retry number ``attempt`` (1-based), bounded."""
    schedule = backoff_schedule()
    index = min(max(1, int(attempt)), len(schedule)) - 1
    ceiling = schedule[-1] if "guardian_fast_backoff" in test_hooks() else BACKOFF_CEILING_SEC
    return min(float(ceiling), float(schedule[index]))


_throttled: dict[str, float] = {}
_throttle_lock = threading.Lock()


def throttled(key: str, message: str, *args, level: int = logging.INFO, interval: float = LOG_THROTTLE_SEC) -> None:
    """Log at most once per ``interval`` per ``key``."""
    now = time.monotonic()
    with _throttle_lock:
        previous = _throttled.get(key)
        if previous is not None and now - previous < interval:
            return
        _throttled[key] = now
    logger.log(level, message, *args)


@dataclass
class Outcome:
    hwnd: int
    reason: str
    resolved: bool


# The identity verdicts, spelled the way ``winapi`` spells them.  They are
# duplicated as literals on purpose: this module must stay importable without the
# Windows layer, and the executor has to reach the same verdict as the UI paths
# even when it is handed a stand-in for it.
IDENTITY_GONE = "gone"
IDENTITY_REUSED = "reused"
IDENTITY_UNKNOWN = "unknown"


def _assess_identity(module, record: ParkRecord) -> str:
    """The one identity verdict for this window, or ``""`` when nothing said so.

    The real Win32 layer answers through :func:`winapi.classify_window_identity`,
    which is the same function the application's own recovery paths use - two
    implementations of "is this still our window" is how one of them started
    retiring records the other still considered live.
    """
    hwnd = int(record.hwnd)
    classifier = getattr(module, "classify_window_identity", None)
    if callable(classifier):
        return str(
            classifier(
                hwnd,
                recorded_pid=int(record.pid or 0),
                recorded_class_name=record.class_name or "",
                recorded_created=record.process_created,
                recorded_process_name=record.process_name or "",
            )
        )
    # A stand-in without the shared classifier still has to fail closed: a zero PID
    # and an empty class name are missing answers, not proof of a mismatch.
    if not module.is_window(hwnd):
        return IDENTITY_GONE
    pid = module.get_pid(hwnd)
    class_name = module.get_class_name(hwnd)
    unanswered = bool((record.pid and not pid) or (record.class_name and not class_name))
    if record.pid and pid and int(pid) != int(record.pid):
        return IDENTITY_REUSED
    if record.class_name and class_name and class_name != record.class_name:
        return IDENTITY_REUSED
    if record.process_created is not None:
        identity = module._query_process_identity(pid or 0, query_name=False)
        if identity.created is None:
            return IDENTITY_UNKNOWN
        if int(identity.created) != int(record.process_created):
            return IDENTITY_REUSED
    return IDENTITY_UNKNOWN if unanswered else ""


def assess(module, record: ParkRecord) -> str:
    """Classify what a record refers to *right now*.

    The distinction that matters is between "provably not ours any more" and
    "cannot be judged right now".  An unverifiable identity (an access-denied
    process, a failed PID or class query, a window that is being created) must
    never be read as "obsolete": that is exactly how a real obligation gets lost.
    """
    hwnd = int(record.hwnd)
    verdict = _assess_identity(module, record)
    if verdict == IDENTITY_GONE:
        return STATUS_GONE
    if verdict == IDENTITY_REUSED:
        return STATUS_REUSED
    if verdict == IDENTITY_UNKNOWN:
        return STATUS_UNVERIFIED
    if parking_evidence(module, record):
        return STATUS_PARKED
    if module.is_effectively_onscreen(hwnd):
        return STATUS_VISIBLE
    return STATUS_OFFSCREEN


def parking_evidence(module, record: ParkRecord) -> bool:
    """Whether this record's window is provably one LookUp parked off-screen.

    Required before any destructive move when the record cannot prove *which*
    process it describes (no creation time, or a record whose geometry had to be
    sanitised).  The journal is local and therefore not trustworthy enough to
    move an arbitrary window on its word alone; LookUp's own mark on the window,
    the rectangle the record says it was parked at, or the characteristic 1x1
    corner of the current layout are all evidence that this window really is one
    LookUp put there - and the first two survive a change of monitor layout.
    """
    hwnd = int(record.hwnd)
    accepts_kwargs = getattr(module, "is_stranded_park", None)
    if callable(accepts_kwargs):
        try:
            return bool(
                accepts_kwargs(
                    hwnd,
                    operation_id=record.operation_id or "",
                    park_rect=tuple(record.park_rect),
                )
            )
        except TypeError:  # pragma: no cover - a stand-in with a narrower signature
            pass
    if not module.is_window(hwnd):
        return False
    if module.looks_like_lookup_parked(hwnd):
        return True
    recorded = tuple(record.park_rect or ())
    matches_rect = getattr(module, "looks_like_parked_at", None)
    if len(recorded) == 4 and callable(matches_rect):
        return bool(matches_rect(hwnd, recorded))
    labelled = getattr(module, "park_operation_id", None)
    return bool(record.operation_id and callable(labelled) and labelled(hwnd) == record.operation_id)


def try_restore(module, record: ParkRecord) -> bool:
    """Put one window back exactly, and report only a *verified* restore.

    Two paths, in order of preference:

    * the exact pre-park placement, but only when the record carries a target
      identity that still matches and the placement itself is trustworthy;
    * the conservative orphan path (keep the size, put the window on a real
      monitor), which is what a record without a usable placement or without a
      verified process identity is allowed to do.
    """
    hwnd = int(record.hwnd)
    identity_is_proven = record.process_created is not None
    if identity_is_proven and record.placement_usable:
        builder = getattr(module, "state_from_record", None)
        if callable(builder):
            # A layer that already knows how to rebuild a placement does it
            # itself, which is also what keeps this executor testable without
            # Windows.
            state = builder(record)
        else:
            state = state_from_record(record, module)
        ok = module.window_matches_parked_state(hwnd, state) and module.restore_parked_window_sync(hwnd, state)
        if ok:
            return True
    if not parking_evidence(module, record):
        # Without the parking signature there is no proof that this window is the
        # one LookUp moved: refuse to touch it rather than risk moving a stranger.
        throttled(
            f"no-evidence:{hwnd}",
            "Recovery %s: hwnd=%s is not in the LookUp parking position and its "
            "identity is not fully proven; refusing to move it",
            record.label or "window",
            hwnd,
            level=logging.WARNING,
        )
        return False
    # The exact placement was refused (or unusable): the safe orphan path keeps
    # the window on a real monitor even when the original placement cannot be
    # reused.
    if _recover_orphan(module, record):
        return True
    return module.is_effectively_onscreen(hwnd) and not parking_evidence(module, record)


def _recover_orphan(module, record: ParkRecord) -> bool:
    """Run the conservative orphan path, passing the identity the record carries.

    A stand-in for the Win32 layer may only take the handle.  The signature is
    inspected rather than a ``TypeError`` caught, so a genuine failure inside the
    restore is never mistaken for "this layer cannot be told about park
    operations" and retried.
    """
    hwnd = int(record.hwnd)
    recover = module.recover_orphaned_lookup_park
    try:
        parameters = inspect.signature(recover).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins and mocks
        parameters = {}
    accepts = "operation_id" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts:
        return bool(
            recover(
                hwnd,
                operation_id=record.operation_id or "",
                park_rect=tuple(record.park_rect or ()),
            )
        )
    return bool(recover(hwnd))


class _ClaimRenewer:
    """Keep a record leased for as long as the work actually takes.

    A lost renewal is not a bookkeeping detail: it means a newer executor took
    the record over, so every mutation this worker could still make would be
    rejected.  The worker is told to stop instead.
    """

    def __init__(self, journal: RecoveryJournal, claim: Claim):
        self._journal = journal
        self._claim = claim
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="LookUpWindows-ClaimRenewer", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def lost(self) -> bool:
        return self._lost.is_set()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(RENEW_SEC):
            try:
                if not self._journal.renew(self._claim):
                    # A failed write is not proof of takeover: the inter-process
                    # journal lock can time out while our fenced claim is still the
                    # current one.  Only a fresh readable record that no longer
                    # matches the token/generation proves that ownership was lost.
                    try:
                        current = self._journal.get(self._claim.hwnd)
                    except JournalReadError:
                        throttled(
                            f"renew-io:{self._claim.hwnd}",
                            "Recovery: claim renewal for hwnd=%s could not be committed or "
                            "re-read yet; keeping the worker alive until ownership can be proved",
                            self._claim.hwnd,
                            level=logging.WARNING,
                        )
                        continue
                    if current is not None and current.claim_matches(self._claim):
                        throttled(
                            f"renew-delayed:{self._claim.hwnd}",
                            "Recovery: claim renewal for hwnd=%s was delayed by journal I/O; "
                            "the fenced claim is still current",
                            self._claim.hwnd,
                            level=logging.WARNING,
                        )
                        continue
                    throttled(
                        f"lost:{self._claim.hwnd}",
                        "Recovery: claim on hwnd=%s was lost (generation %s, token %s); "
                        "another executor owns it now",
                        self._claim.hwnd,
                        self._claim.generation,
                        self._claim.short_token,
                        level=logging.WARNING,
                    )
                    self._lost.set()
                    return
            except Exception:  # pragma: no cover - defensive lease bookkeeping
                logger.debug("Recovery claim renewal failed for hwnd=%s", self._claim.hwnd, exc_info=True)


def resolve(
    journal: RecoveryJournal,
    record: ParkRecord,
    executor,
    *,
    context: str = "recovery",
    budget_sec: float | None = None,
    stop: threading.Event | None = None,
    deadline: float | None = None,
) -> Outcome:
    """Drive one record to a definitive outcome.

    There is deliberately **no** upper time limit here.  A committed record
    describes a window that is demonstrably parked off-screen, so the executor
    that owns it keeps trying (with bounded backoff) until the window is verifiably
    back, provably gone, or has become somebody else's window.  ``budget_sec`` and
    ``stop`` exist only for callers that must return - the application's startup
    pass, and a guardian that is handing over - and both of them *release* the
    claim while leaving the record, which keeps the obligation durable instead of
    discarding it.
    """
    module = winapi()
    hwnd = int(record.hwnd)
    identity = _identity(executor)
    claim = journal.claim(hwnd, identity)
    if claim is None:
        try:
            existing = journal.get(hwnd)
        except JournalReadError:
            return Outcome(hwnd, "claim-failed", False)
        if existing is not None and not existing.lease_is_live(identity):
            # The claim failed for an I/O reason, not because somebody else owns
            # it: leaving the record in place is the safe answer.
            return Outcome(hwnd, "claim-failed", False)
        return Outcome(hwnd, "claimed-by-another-executor", False)
    record = _fresh_record(journal, hwnd, record)
    renewer = _ClaimRenewer(journal, claim)
    renewer.start()
    started = time.monotonic()
    stop_at = None if budget_sec is None else started + max(0.0, float(budget_sec))
    if deadline is not None:
        stop_at = min(stop_at, float(deadline)) if stop_at is not None else float(deadline)

    def finished(now: float) -> bool:
        if stop is not None and stop.is_set():
            return True
        return _out_of_time(now, stop_at)

    visible_since: float | None = None
    attempt = 0
    resolved = False
    reason = "unknown"
    try:
        while True:
            now = time.monotonic()
            status = assess(module, record)
            if renewer.lost():
                reason, resolved = STALE_CLAIM, False
                break
            if status == STATUS_GONE:
                reason, resolved = "window no longer exists", True
                break
            if status == STATUS_REUSED:
                reason, resolved = "window handle was reused by another window", True
                break
            if status == STATUS_UNVERIFIED:
                # Defer instead of destroying evidence about a live window.
                reason = STATUS_UNVERIFIED
                if finished(now):
                    break
                time.sleep(backoff_delay(1))
                continue
            if status == STATUS_PARKED or (status == STATUS_OFFSCREEN and record.parked):
                visible_since = None
                if try_restore(module, record):
                    reason, resolved = "restored", True
                    break
                attempt += 1
                reason = "restore not verified yet"
                throttled(
                    f"restore:{context}:{hwnd}",
                    "Recovery %s: %s still parked (%r), retry %s in %.1fs; "
                    "claim gen=%s token=%s",
                    context,
                    record.describe(),
                    record.label,
                    attempt,
                    backoff_delay(attempt),
                    claim.generation,
                    claim.short_token,
                    level=logging.WARNING,
                )
                if finished(now):
                    break
                time.sleep(backoff_delay(attempt))
                continue
            if status == STATUS_OFFSCREEN:
                # Pending intent and the window is off every monitor without
                # being in our parking position.  Keep the record: only a
                # verified visible window may release a pending intent.
                reason = "pending intent, window off-screen"
                visible_since = None
                if finished(now):
                    break
                time.sleep(backoff_delay(1))
                continue
            # STATUS_VISIBLE
            if record.parked:
                reason, resolved = "window is back on a monitor", True
                break
            if visible_since is None:
                visible_since = now
            elif now - visible_since >= PENDING_SETTLE_SEC:
                # The intended move never reached the window during a long
                # stretch in which it was verifiably visible: the intent can be
                # released without losing a restore.
                reason, resolved = "parked intent was never applied", True
                break
            else:
                reason = "pending intent, watching for the move to land"
            if finished(now):
                break
            time.sleep(backoff_delay(1))
    except Exception as exc:  # pragma: no cover - the guardian must never die
        logger.exception("Restore executor failed for hwnd=%s (%s)", hwnd, context)
        reason = f"executor error: {exc!r}"
    finally:
        renewer.stop()
        if resolved:
            # The window is back, but the obligation only ends once the durable
            # record is gone: a journal that still advertises a parked window
            # would make the next start "recover" a window that is on screen.
            cleared = False
            for _attempt in range(3):
                if journal.clear_claimed(claim, recorded_at=record.recorded_at):
                    cleared = True
                    break
                time.sleep(0.2)
            if cleared:
                logger.info(
                    "Recovery %s released hwnd=%s (%r): %s [%s gen=%s token=%s]",
                    context,
                    hwnd,
                    record.label,
                    reason,
                    record.state,
                    claim.generation,
                    claim.short_token,
                )
            else:
                journal.release(claim)
                resolved = False
                reason = f"{reason}; the durable record could not be cleared"
        else:
            # The record stays.  Releasing only the lease lets another executor
            # pick the obligation up immediately instead of waiting for expiry.
            journal.release(claim)
    if not resolved:
        throttled(
            f"unresolved:{context}:{hwnd}",
            "Recovery %s could not discharge %s: %s (claim released, record kept)",
            context,
            record.describe(),
            reason,
            level=logging.WARNING,
        )
    return Outcome(hwnd, reason, resolved)


def _out_of_time(now: float, stop_at: float | None) -> bool:
    return stop_at is not None and now >= stop_at


def _identity(executor) -> ExecutorIdentity:
    return executor if isinstance(executor, ExecutorIdentity) else _as_executor(executor)


def _fresh_record(journal: RecoveryJournal, hwnd: int, record: ParkRecord) -> ParkRecord:
    """The committed copy of ``record``, so the claim cannot be one generation behind."""
    try:
        current = journal.get(int(hwnd))
    except (JournalReadError, OSError):
        return record
    return current if current is not None else record


def resolve_all(
    journal: RecoveryJournal,
    executor,
    *,
    context: str = "recovery",
    budget_sec: float | None = None,
    deadline: float | None = None,
) -> dict[str, int]:
    """Resolve every record the journal holds, one worker thread per record.

    A window that never answers must not hold up the restore of another one, so
    the records are worked in parallel and the caller is released when they are
    all finished - or when ``budget_sec``/``deadline`` runs out.  Running out of
    time releases the leases and keeps the records: the obligation outlives the
    caller.
    """
    stop_at = None
    if budget_sec is not None or deadline is not None:
        now = time.monotonic()
        stop_at = now + max(0.0, float(budget_sec)) if budget_sec is not None else float(deadline)
    summary = {"restored": 0, "released": 0, "outstanding": 0, "skipped": 0}
    while True:
        try:
            snapshot = journal.snapshot()
            break
        except JournalReadError:
            if _out_of_time(time.monotonic(), stop_at):
                # The caller cannot wait for an unknown journal.  Nothing is
                # claimed and nothing is discarded: the records stay on disk for the
                # next executor.
                logger.error(
                    "Recovery %s gave up: the recovery journal is unreadable and the "
                    "caller's budget is exhausted",
                    context,
                )
                summary["unreadable"] = 1
                return summary
            # Startup recovery must retry a transient unreadable journal, not
            # silently abandon the only recovery pass of this application.
            time.sleep(POLL_SEC)
    records = snapshot.records
    if snapshot.damaged:
        # An empty record list next to unreadable evidence is not "nothing to do";
        # say so in the summary the caller decides on.
        summary["damaged"] = 1
        summary["unresolved"] = len(snapshot.outstanding)
        logger.warning(
            "Recovery %s: the recovery journal is damaged (%s); %s salvaged obligation(s) "
            "are only executable through the conservative sweep",
            context,
            snapshot.damage or "unknown damage",
            len(snapshot.outstanding),
        )
    if not records:
        return summary
    results: list[Outcome] = []
    lock = threading.Lock()

    def worker(record: ParkRecord) -> None:
        try:
            outcome = resolve(
                journal,
                record,
                executor,
                context=context,
                budget_sec=budget_sec,
                deadline=deadline,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Recovery worker crashed for hwnd=%s", record.hwnd)
            outcome = Outcome(int(record.hwnd), "worker error", False)
        with lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=worker, args=(record,), name=f"Recovery-{record.hwnd}", daemon=True)
        for record in records
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(timeout=POLL_SEC)
            if _out_of_time(time.monotonic(), stop_at):
                break
    for outcome in results:
        if outcome.resolved:
            if outcome.reason == "restored":
                summary["restored"] += 1
            else:
                summary["released"] += 1
        elif outcome.reason == "claimed-by-another-executor":
            summary["skipped"] += 1
        else:
            summary["outstanding"] += 1
    if any(results):
        logger.info(
            "Recovery %s finished: %s restored, %s released, %s outstanding, %s left to another executor",
            context,
            summary["restored"],
            summary["released"],
            summary["outstanding"],
            summary["skipped"],
        )
    return summary


# --------------------------------------------------------------------------- #
# Process identity
# --------------------------------------------------------------------------- #

def process_creation_time(pid: int) -> int | None:
    """Creation time of ``pid``, or ``None`` when it cannot be established."""
    try:
        return winapi().get_process_creation_time(int(pid))
    except Exception:  # pragma: no cover - defensive
        logger.debug("Process creation time for pid=%s unavailable", pid, exc_info=True)
        return None


def owner_is_alive(record: ParkRecord, *, creation_probe=None) -> bool:
    """Whether the LookUp run that registered ``record`` is still running.

    A PID is a locator, not an identity: Windows recycles them, and a recycled
    PID would otherwise convince every executor that the owner of the obligation
    is still around, so nobody would ever restore the user's window.  The proof is
    therefore *PID exists **and** creation time equals the recorded one*.

    A record whose owner identity is missing or unverifiable is treated as
    **not** trusted: an old journal must not be able to freeze recovery, and the
    operations involved (restoring a window out of LookUp's own parking corner)
    are idempotent.
    """
    pid = int(record.owner_pid)
    if not pid or not process_is_alive(pid):
        return False
    if record.owner_created is None:
        return False
    probe = creation_probe or process_creation_time
    try:
        actual = probe(pid)
    except Exception:  # pragma: no cover - defensive
        return False
    if actual is None:
        return False
    return int(actual) == int(record.owner_created)


# --------------------------------------------------------------------------- #
# The guardian process
# --------------------------------------------------------------------------- #

def guardian_command(
    journal_path: str,
    owner_pid: int = 0,
    owner_created: int = 0,
    owner_run_id: str = "",
    handover_from: str = "",
) -> list[str]:
    """The command line that starts a guardian for ``journal_path``.

    A frozen build re-executes its own executable, a source run executes this
    module directly: the guardian then imports nothing but the journal model and
    (lazily) the Win32 layer, so it stays cheap and independent of the UI.

    The handover partner is passed as a full identity (PID *and* creation time
    *and* run id) for the same reason the records carry one: a recycled PID must
    not be able to keep the guardian alive for an owner that is long gone.
    """
    arguments = [
        str(journal_path),
        str(int(owner_pid or 0)),
        str(int(owner_created or 0)),
        str(owner_run_id or ""),
    ]
    if handover_from:
        arguments.extend(["--handover-from", str(handover_from)])
    if getattr(sys, "frozen", False):
        return [sys.executable, GUARDIAN_ARG, *arguments]
    return [sys.executable, str(BASE_DIR / "restoreguard.py"), GUARDIAN_ARG, *arguments]


class _HandshakePipe:
    """One reader for the whole handshake of one guardian launch.

    The handover has *two* stages: the successor announces ``started`` while it
    waits for the mutex, and ``ready`` once it owns it.  A reader that was built
    for one token and thrown away after it threw away the pipe with it - the
    second stage then wrote into a closed pipe and the confirmation the protocol
    requires could never arrive.  This object therefore owns the stream and the
    buffer for the entire handshake: partial lines survive between stages, both
    tokens may even arrive in a single write, and the stream is closed exactly
    once, when the handshake is over.
    """

    #: A line longer than this is not a handshake token; refusing it early keeps
    #: a chatty child from growing the buffer without bound.
    MAX_LINE_BYTES = 4096

    def __init__(self, process: subprocess.Popen):
        self._process = process
        self._stream = getattr(process, "stdout", None)
        self._buffer = bytearray()
        self._eof = False
        self._closed = False
        self._pipe = None
        self._peek = None
        if self._stream is not None:
            try:
                fd = self._stream.fileno()
            except (OSError, ValueError):
                self._stream = None
                return
            if os.name == "nt":
                try:
                    import msvcrt

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    self._peek = kernel32.PeekNamedPipe
                    self._peek.argtypes = [
                        ctypes.c_void_p,
                        ctypes.c_void_p,
                        ctypes.c_ulong,
                        ctypes.c_void_p,
                        ctypes.POINTER(ctypes.c_ulong),
                        ctypes.c_void_p,
                    ]
                    self._peek.restype = ctypes.c_int
                    self._pipe = msvcrt.get_osfhandle(fd)
                except (OSError, AttributeError, ImportError):  # pragma: no cover
                    self._peek = None
            else:  # pragma: no cover - non-Windows
                self._pipe = fd

    @property
    def usable(self) -> bool:
        return self._stream is not None and not self._closed

    def take_line(self) -> bytes | None:
        """The next complete line, or ``None`` when none has arrived yet."""
        if b"\n" not in self._buffer:
            return None
        raw = bytes(self._buffer).split(b"\n", 1)[0]
        del self._buffer[: len(raw) + 1]
        return raw.strip()

    def wait_line(self, timeout: float) -> bytes | None:
        """One line, polling the pipe without ever blocking on a buffered read.

        Returns ``None`` on EOF, on a closed stream or when ``timeout`` expired -
        the caller decides what that means.  The buffer is deliberately kept so a
        token that arrived partly during this call is completed by the next one.
        """
        line = self.take_line()
        if line is not None:
            return line
        if not self.usable:
            return None
        deadline = time.monotonic() + max(0.05, float(timeout))
        while True:
            count = self._available()
            if count:
                try:
                    chunk = os.read(self._stream.fileno(), min(count, 512))
                except (OSError, ValueError):
                    self._eof = True
                    break
                if not chunk:
                    self._eof = True
                    break
                self._buffer.extend(chunk)
                if len(self._buffer) > self.MAX_LINE_BYTES:
                    self._eof = True
                    break
                line = self.take_line()
                if line is not None:
                    return line
                continue
            if self._process_exited():
                # Drain whatever is still buffered before declaring EOF: a
                # process that wrote its token and exited must still be heard.
                try:
                    remaining = os.read(self._stream.fileno(), 512)
                except (OSError, ValueError):
                    remaining = b""
                if remaining:
                    self._buffer.extend(remaining)
                    line = self.take_line()
                    if line is not None:
                        return line
                self._eof = True
                break
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

        line = self.take_line()
        return line

    def wait_token(self, token: bytes, timeout: float) -> bool:
        """Wait for ``token``, ignoring lines that are not it.

        A token we did not ask for is not a failure: the successor may announce
        readiness before this stage even starts looking.
        """
        deadline = time.monotonic() + max(0.05, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            line = self.wait_line(min(remaining, 0.5) if remaining > 0 else 0.0)
            if line is None:
                if self._eof or not self.usable:
                    return False
                if time.monotonic() >= deadline:
                    return False
                continue
            if _line_is(line, token):
                return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffer.clear()
        if self._stream is None:
            return
        try:
            self._stream.close()
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass

    def _process_exited(self) -> bool:
        try:
            return self._process.poll() is not None
        except Exception:  # pragma: no cover - a stand-in process object
            return False

    def _available(self) -> int:
        if self._stream is None or self._eof:
            return 0
        if self._peek is not None:
            available = ctypes.c_ulong()
            try:
                if not self._peek(self._pipe, None, 0, None, ctypes.byref(available), None):
                    # A closed pipe end means the child is done with it.
                    return 0
            except (OSError, ValueError):  # pragma: no cover - defensive
                return 0
            return int(available.value)
        import select

        try:
            ready = select.select([self._stream.fileno()], [], [], 0)[0]
        except (OSError, ValueError):  # pragma: no cover - defensive
            return 0
        return 512 if ready else 0


def _link_for(process: subprocess.Popen) -> _HandshakePipe:
    """The one handshake reader of ``process``, created on first use."""
    link = getattr(process, "_lookup_handshake", None)
    if link is None:
        link = _HandshakePipe(process)
        try:
            process._lookup_handshake = link  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - a stand-in process object
            pass
    return link


def _line_is(line: bytes, token: bytes) -> bool:
    return line == token or line.startswith(token + b" ")


def _line_detail(line: bytes, token: bytes) -> dict[str, str]:
    """Parse ``key=value`` pairs out of an announced token's detail."""
    detail: dict[str, str] = {}
    for part in line.split(b" ")[1:]:
        key, _, value = part.partition(b"=")
        if value:
            detail[key.decode("ascii", "replace")] = value.decode("ascii", "replace")
    return detail


def _wait_for_token(process: subprocess.Popen, timeout: float, prefix: bytes = GUARDIAN_READY_TOKEN) -> bool:
    """Wait for a single token on the launch's handshake pipe.

    Kept as the single-token form used by the first stage and by callers that do
    not need a second token; the stream stays open afterwards, because the same
    handshake may still have a ``ready`` coming.
    """
    return _link_for(process).wait_token(prefix, timeout)


# --------------------------------------------------------------------------- #
# Ownership of an unconfirmed guardian launch
# --------------------------------------------------------------------------- #

# The limit flags of JOBOBJECT_BASIC_LIMIT_INFORMATION, as WinNT.h defines them.
# They are written out rather than imported because a wrong bit here is silent:
# 0x00004000 is JOB_OBJECT_LIMIT_SUBSET_AFFINITY, and 0x08000000 is no limit at
# all, so a swapped-in value makes a kill-on-close job with real breakaway look
# like a job that cannot be left - and that refuses parking for no reason.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS = 1
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_JOB_OBJECT_ALL_ACCESS = 0x1F001F


class _LargeInteger(ctypes.Structure):
    _fields_ = [("QuadPart", ctypes.c_longlong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LargeInteger),
        ("PerJobUserTimeLimit", _LargeInteger),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _LargeInteger),
        ("TotalKernelTime", _LargeInteger),
        ("ThisPeriodTotalUserTime", _LargeInteger),
        ("ThisPeriodTotalKernelTime", _LargeInteger),
        ("TotalPageFaultCount", ctypes.c_ulong),
        ("TotalProcesses", ctypes.c_ulong),
        ("ActiveProcesses", ctypes.c_ulong),
        ("TotalTerminatedProcesses", ctypes.c_ulong),
    ]


def _job_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.OpenJobObjectW.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.OpenJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    kernel32.SetInformationJobObject.restype = ctypes.c_int
    kernel32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = ctypes.c_int
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.restype = ctypes.c_int
    kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    kernel32.IsProcessInJob.restype = ctypes.c_int
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


def process_in_any_job(process) -> bool | None:
    """Whether ``process`` is still attached to any pre-existing Windows job.

    ``True`` is deliberately *not* a verdict about safety: a child created with
    ``CREATE_BREAKAWAY_FROM_JOB`` can still be reported inside a different job
    whose limits are harmless, so membership alone says nothing about who would
    kill it.  What decides that is :func:`enclosing_job`, read from the limits
    of the job *this* process is running in.
    """
    if os.name != "nt":
        return False
    pid = int(getattr(process, "pid", 0) or 0)
    if not pid:
        return None
    try:
        kernel32 = _job_kernel32()
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            logger.error(
                "Recovery guardian pid=%s could not be opened to verify job independence (error %s)",
                pid,
                ctypes.get_last_error(),
            )
            return None
        try:
            in_job = ctypes.c_int()
            if not kernel32.IsProcessInJob(handle, None, ctypes.byref(in_job)):
                logger.error(
                    "Recovery guardian pid=%s job membership could not be verified (error %s)",
                    pid,
                    ctypes.get_last_error(),
                )
                return None
            return bool(in_job.value)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):  # pragma: no cover - non-Windows/defensive
        logger.exception("Recovery guardian pid=%s job membership check failed", pid)
        return None


# What the Job Object around *this* process allows a recovery guardian to do.
GUARDIAN_CONTEXT_NONE = "no_enclosing_job"
GUARDIAN_CONTEXT_ORDINARY = "ordinary_job"
GUARDIAN_CONTEXT_HOSTILE = "kill_on_close_job"
GUARDIAN_CONTEXT_UNREADABLE = "unreadable_job"


class EnclosingJob(NamedTuple):
    """The limits of the job this process runs in, and what they allow.

    ``limit_flags`` is ``None`` whenever membership itself could not be
    established, which is why the kind is a separate value: an unknown job is
    not an ordinary one, and must never be treated as one.
    """

    kind: str
    limit_flags: int | None

    @property
    def explicit_breakaway(self) -> bool:
        """Whether the job grants ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``.

        This is the only limit that makes ``CREATE_BREAKAWAY_FROM_JOB`` legal -
        the flag is rejected with ``ERROR_ACCESS_DENIED`` without it, so this
        property, not "can a guardian leave", decides what is passed to
        ``CreateProcess``.
        """
        return bool((self.limit_flags or 0) & _JOB_OBJECT_LIMIT_BREAKAWAY_OK)

    @property
    def silent_breakaway(self) -> bool:
        """Whether the job grants ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK``.

        Such a job already removes children from itself.  Adding
        ``CREATE_BREAKAWAY_FROM_JOB`` on top does not make that more likely and
        is not required - and where ``BREAKAWAY_OK`` is absent it makes the
        launch fail outright.
        """
        return bool((self.limit_flags or 0) & _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)

    @property
    def can_escape(self) -> bool:
        """Whether a child of this process can end up outside this job.

        True outside any job (there is nothing to leave) and inside a job that
        permits breakaway by either route.  Forbidding it is exactly the case
        that makes a guardian a prisoner of its launcher.
        """
        return (
            self.kind == GUARDIAN_CONTEXT_NONE
            or self.explicit_breakaway
            or self.silent_breakaway
        )

    @property
    def permits_guardian(self) -> bool:
        """Whether a guardian may run under this job context at all.

        Only the confirmed kill-on-close job - and a job whose limits could not
        be read - forbid it.  An ordinary job, or none at all, does not: being
        in *some* job is an ordinary way to run a desktop application.
        """
        return self.kind in (GUARDIAN_CONTEXT_NONE, GUARDIAN_CONTEXT_ORDINARY)


def _classify_job_flags(flags: int) -> str:
    """The context a job with exactly these limit flags imposes on a guardian.

    Only the combination "kill-on-close and no way out" is hostile: being in a
    job at all, or even being killed with it, is survivable as long as the child
    can be created outside that job.
    """
    if flags & _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and not (
        EnclosingJob(GUARDIAN_CONTEXT_ORDINARY, flags).can_escape
    ):
        # A launcher that closes this job takes every process in it with it,
        # and this application cannot leave.  There is no guardian to start.
        return GUARDIAN_CONTEXT_HOSTILE
    return GUARDIAN_CONTEXT_ORDINARY


def enclosing_job() -> EnclosingJob:
    """Classify the Job Object this process itself runs in.

    The limits of the enclosing job are readable without owning a job handle:
    ``QueryInformationJobObject`` accepts ``NULL`` and then answers for the job
    the *calling* process is associated with.  That is the only handle-free way
    to tell a launcher-owned kill-on-close job from an ordinary one, so it is
    what decides whether a guardian is allowed to exist here.
    """
    if os.name != "nt":
        return EnclosingJob(GUARDIAN_CONTEXT_NONE, None)
    try:
        kernel32 = _job_kernel32()
        handle = kernel32.GetCurrentProcess()
        in_job = ctypes.c_int()
        if not kernel32.IsProcessInJob(handle, None, ctypes.byref(in_job)):
            logger.error(
                "Enclosing job membership could not be established (error %s)",
                ctypes.get_last_error(),
            )
            return EnclosingJob(GUARDIAN_CONTEXT_UNREADABLE, None)
        if not in_job.value:
            return EnclosingJob(GUARDIAN_CONTEXT_NONE, None)
        info = _JobExtendedLimitInformation()
        if not kernel32.QueryInformationJobObject(
            None,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            logger.error(
                "Enclosing job limits could not be read (error %s)",
                ctypes.get_last_error(),
            )
            return EnclosingJob(GUARDIAN_CONTEXT_UNREADABLE, None)
        flags = int(info.BasicLimitInformation.LimitFlags)
        return EnclosingJob(_classify_job_flags(flags), flags)
    except (OSError, AttributeError):  # pragma: no cover - non-Windows/defensive
        logger.exception("Enclosing job classification failed")
        return EnclosingJob(GUARDIAN_CONTEXT_UNREADABLE, None)


def job_active_processes(name: str) -> int | None:
    """How many processes the named job still holds, or ``None`` if it is gone.

    A named job disappears together with its last handle, so "the job no longer
    exists" is exactly the proof that nothing this launch started survived it.
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = _job_kernel32()
        handle = kernel32.OpenJobObjectW(_JOB_OBJECT_ALL_ACCESS, False, name)
        if not handle:
            return None
        try:
            info = _JobBasicAccountingInformation()
            if not kernel32.QueryInformationJobObject(
                handle,
                _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            ):
                return None
            return int(info.ActiveProcesses)
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        return None


class _GuardianLaunchJob:
    """Kill-on-close ownership of one guardian launch, until it is confirmed.

    ``taskkill /T`` is the fallback, not the mechanism: it needs privileges the
    application does not have, its return code was never checked, and killing a
    onefile launcher alone leaves its Python child alive - holding the executor
    mutex and keeping a journal nobody is executing.  So the launch is bound to a
    job *at creation*, before it can spawn anything of its own, and the
    kill-on-close limit is removed again the moment the guardian has confirmed
    readiness - because from that point it must survive this process.
    """

    def __init__(self):
        self.name = ""
        self._handle = 0
        self._kernel32 = None
        self._adopted = False
        if os.name != "nt":
            return
        try:
            kernel32 = _job_kernel32()
            self.name = "Local\\LookUpWindows-GuardianLaunch-%s" % os.urandom(8).hex()
            handle = kernel32.CreateJobObjectW(None, self.name)
            if not handle:
                logger.warning("Guardian launch job could not be created; tree cleanup is degraded")
                return
            self._kernel32 = kernel32
            if not self._set_limits(int(handle), kill_on_close=True):
                kernel32.CloseHandle(handle)
                self.name = ""
                return
            self._handle = int(handle)
        except (OSError, AttributeError):  # pragma: no cover - non-Windows
            self.name = ""

    @property
    def available(self) -> bool:
        return bool(self._handle)

    @property
    def owns_tree(self) -> bool:
        return bool(self._handle and self._adopted)

    def adopt(self, process) -> bool:
        """Bind a freshly created guardian to this job."""
        if not self._handle:
            return False
        pid = int(getattr(process, "pid", 0) or 0)
        if not pid:
            return False
        kernel32 = self._kernel32
        handle = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            logger.warning("Guardian launcher pid=%s could not be opened for job assignment", pid)
            return False
        try:
            if kernel32.AssignProcessToJobObject(self._handle, handle):
                self._adopted = True
                return True
            # An enclosing job that forbids nesting makes this fail; the launcher
            # then owns no tree, which is said out loud instead of hidden.
            logger.warning(
                "Guardian launcher pid=%s could not be bound to its launch job "
                "(error %s); unready-startup cleanup will rely on the fallback",
                pid,
                ctypes.get_last_error(),
            )
            return False
        finally:
            kernel32.CloseHandle(handle)

    def release(self) -> None:
        """Give up ownership without killing: the guardian is confirmed working.

        Removing the kill-on-close limit first is what keeps a *successful*
        handover working - otherwise the parent's exit would take the very
        process it just confirmed.
        """
        if self._handle:
            self._set_limits(self._handle, kill_on_close=False)
        self.close()

    def close(self) -> None:
        """Drop the handle; a job with kill-on-close takes its processes with it."""
        if not self._handle:
            return
        try:
            self._kernel32.CloseHandle(self._handle)
        except (OSError, AttributeError):  # pragma: no cover - defensive
            pass
        self._handle = 0
        self._adopted = False

    def _set_limits(self, handle: int, *, kill_on_close: bool) -> bool:
        info = _JobExtendedLimitInformation()
        if kill_on_close:
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            ok = self._kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
        except (OSError, AttributeError):  # pragma: no cover - defensive
            return False
        if not ok:
            logger.error(
                "Guardian launch job limits could not be set (error %s)", ctypes.get_last_error()
            )
        return bool(ok)


def _tree_taskkill(pid: int) -> bool:
    """``taskkill /T``, with its result actually checked.

    Returns True only when the tool reported success.  A refused or failing
    ``taskkill`` used to be indistinguishable from a successful one, which is
    exactly how an unready onefile child kept holding the executor mutex.
    """
    if os.name != "nt":
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=10.0,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("taskkill could not be used for the guardian tree of pid=%s: %s", pid, exc)
        return False
    if completed.returncode != 0:
        logger.warning(
            "taskkill failed for the guardian tree of pid=%s (rc=%s): %s",
            pid,
            completed.returncode,
            (completed.stdout or completed.stderr or "").strip(),
        )
        return False
    return True


def _stop_unready_guardian(process: subprocess.Popen, job: "_GuardianLaunchJob | None" = None) -> None:
    """Bounded cleanup of this launch, including its onefile Python child.

    Every step is verified rather than assumed: the job that owns the tree is
    closed, the tool fallback is used only if the job could not own the launcher,
    and the result is checked against the job's own process list so a surviving
    child is *reported* rather than silently inherited by the next guardian.
    """
    _link_for(process).close()
    name = job.name if job is not None else ""
    # Whether this launch actually owns its tree decides if the tool fallback is
    # still needed at all: if the job did, closing it has already removed the
    # processes, and asking taskkill to do it again only produces noise.
    owned_tree = bool(job is not None and job.owns_tree)
    if job is not None:
        job.close()
    if process.poll() is None:
        if not owned_tree:
            _tree_taskkill(int(process.pid))
        try:
            process.kill()
        except (OSError, ValueError):  # pragma: no cover - defensive
            pass
        try:
            process.wait(timeout=5.0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            logger.warning("Unready guardian pid=%s did not exit", process.pid)
    if not name:
        return
    deadline = time.monotonic() + 5.0
    survivors = job_active_processes(name)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.05)
        survivors = job_active_processes(name)
    if survivors:
        # The job outlived our handle because something still holds it open;
        # address the tree directly and make the failure visible either way.
        _tree_taskkill(int(process.pid))
        time.sleep(0.1)
        survivors = job_active_processes(name)
    if survivors:
        logger.error(
            "The unready guardian launch of pid=%s still owns %s process(es); the "
            "executor mutex may stay taken by a process this launch created",
            process.pid,
            survivors,
        )


def guardian_environment() -> dict[str, str] | None:
    """Environment for a process that has to outlive this one.

    A onefile build points a frozen child at the *parent's* extracted directory
    (``_PYI_PARENT_PROCESS_LEVEL``) so the child does not have to extract the
    bundle a second time.  That is a sensible optimisation for a short-lived
    child and a trap for the guardian: the guardian deliberately keeps working
    after the application has exited, so it still holds that directory open when
    the application's launcher tries to delete it - and the launcher then reports
    a modal "failed to remove temporary directory" failure instead of exiting
    quietly, which leaves a visible dialog and a process that never exits.

    The guardian therefore starts as an independent frozen instance that
    extracts its own copy.  ``None`` means "inherit unchanged", which is what a
    source run (and any build without those variables) needs.
    """
    if not getattr(sys, "frozen", False):
        return None
    inherited = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("_PYI_") or key.startswith("_MEIPASS"))
    }
    logger.debug(
        "Starting the guardian as an independent frozen instance (%s)",
        "own extraction" if inherited != dict(os.environ) else "shared extraction",
    )
    return inherited


# The capture helpers are bound to a kill-on-close Job Object so they cannot
# outlive LookUp.  The recovery executor is the exact opposite case: it *must*
# outlive the application, so it leaves the job LookUp runs in whenever that job
# lets it - a shell, a service or another application may have put LookUp into a
# job of its own.  Leaving is requested, never assumed, and never traded away to
# make a launch succeed: whether the enclosing job tolerates it is read from its
# limits by ``enclosing_job`` and decided before anything is started.  The two
# breakaway limits are not interchangeable - see ``EnclosingJob.explicit_breakaway``
# and ``EnclosingJob.silent_breakaway``.
_CREATE_BREAKAWAY_FROM_JOB = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _guardian_creation_flags(enclosing: EnclosingJob | None = None) -> int:
    """Creation flags for one guardian launch.

    ``CREATE_BREAKAWAY_FROM_JOB`` is requested exactly where it is both legal
    and needed: inside a job that granted ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``.
    The two other cases must not carry it.

    Outside any job the flag is inert, so it is not passed at all.  Inside a
    job that only granted ``JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`` the children
    are already detached, and the flag is not merely redundant there - without
    ``BREAKAWAY_OK`` the launch fails with ``ERROR_ACCESS_DENIED``.  So the
    silent mode is honoured by *not* asking for anything.
    """
    if os.name != "nt":
        return 0
    if enclosing is not None and enclosing.explicit_breakaway:
        return _CREATE_NO_WINDOW | _CREATE_BREAKAWAY_FROM_JOB
    return _CREATE_NO_WINDOW


def _spawn(
    command: list[str],
    *,
    timeout: float,
    expect: bytes,
) -> subprocess.Popen | None:
    """Start a guardian and return it only after independent readiness.

    The decision is made once, from the enclosing job's own limits, before
    anything is launched:

    * confirmed kill-on-close job that forbids breakaway -> no guardian at all,
      and therefore no park: that job's owner can kill LookUp and its executor
      in one step, and there is no way out of it;
    * enclosing job whose limits cannot be read -> fail closed the same way,
      because "unknown" is not "harmless";
    * ordinary job, or none -> the guardian is allowed, and the child leaves the
      enclosing job by whichever breakaway mode that job actually granted.

    What a guardian's *own* ``IsProcessInJob`` answer cannot do is prove
    danger: membership in some job is how a supervised desktop application
    normally runs, and a breakaway child can still be reported inside an
    unrelated one.  So membership is only rejected where nothing in the parent
    could have put the child there.
    """
    env = guardian_environment()
    enclosing = enclosing_job()
    if not enclosing.permits_guardian:
        logger.error(
            "Recovery guardian refused: this process runs in a %s (limits %s); "
            "there is no way to start an executor that could outlive it",
            enclosing.kind,
            hex(enclosing.limit_flags) if enclosing.limit_flags is not None else "unreadable",
        )
        return None
    creationflags = _guardian_creation_flags(enclosing) if os.name == "nt" else 0
    job = _GuardianLaunchJob()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            env=env,
            close_fds=True,
        )
    except OSError as exc:
        job.close()
        if os.name == "nt" and exc.winerror == _ERROR_ACCESS_DENIED:
            logger.error(
                "Recovery guardian breakaway was denied; refusing to park without "
                "an independent recovery executor"
            )
        else:
            logger.error("Recovery guardian could not be started: %s", exc)
        return None

    inherited_job = process_in_any_job(process)
    if inherited_job is None:
        logger.error(
            "Recovery guardian pid=%s job membership could not be verified; "
            "refusing the handover",
            process.pid,
        )
        _stop_unready_guardian(process, job)
        return None
    if enclosing.kind == GUARDIAN_CONTEXT_NONE and inherited_job:
        # Nothing in this process could have produced a job around the child,
        # so this one was placed by somebody else and is not ours to judge.
        logger.error(
            "Recovery guardian pid=%s was placed into a Windows Job Object although "
            "this process runs in none; refusing the handover",
            process.pid,
        )
        _stop_unready_guardian(process, job)
        return None

    # Bind before the launcher can create a child of its own.  If this ownership
    # cannot be established, readiness is not accepted: otherwise a failed launch
    # cleanup could leave the onefile child behind holding the guardian mutex.
    if not job.adopt(process):
        logger.error(
            "Recovery guardian pid=%s could not be bound to the temporary launch "
            "job; refusing the handover",
            process.pid,
        )
        _stop_unready_guardian(process, job)
        return None

    if _wait_for_token(process, timeout, expect):
        job.release()
        return process
    logger.error(
        "Recovery guardian (pid %s) did not report %r within %.1fs",
        process.pid,
        expect.decode("ascii", "replace"),
        timeout,
    )
    _stop_unready_guardian(process, job)
    return None


def spawn_guardian(
    journal_path,
    *,
    owner_pid: int = 0,
    owner_created: int = 0,
    owner_run_id: str = "",
    handover_from: str = "",
    timeout: float = 5.0,
) -> subprocess.Popen | None:
    """Start a guardian and return it only once it reports that it is working."""
    command = guardian_command(
        str(journal_path), owner_pid, owner_created, owner_run_id, handover_from
    )
    if handover_from:
        # A successor is only "alive and committed"; it announces ownership
        # itself, after it has inherited the mutex, and its predecessor waits for
        # that second token on this very pipe.
        return _spawn(command, timeout=timeout, expect=GUARDIAN_STARTED_TOKEN)
    process = _spawn(command, timeout=timeout, expect=GUARDIAN_READY_TOKEN)
    if process is not None:
        # Readiness is all this caller waits for; the pipe has served its purpose
        # and the guardian redirects its stdout away from it.
        _link_for(process).close()
    return process


class _SingletonLock:
    """A named-mutex handle that releases itself when it is closed."""

    def __init__(self, handle):
        self._handle = handle
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle(self._handle)
        except (OSError, AttributeError):  # pragma: no cover - non-Windows
            pass

    def __del__(self):  # pragma: no cover - safety net for an early exception
        try:
            self.close()
        except Exception:
            pass


_NULL_SINGLETON = _SingletonLock(None)


def guardian_mutex_name(journal_path) -> str:
    """The executor mutex of *one journal document*.

    Naming it after the document is what makes "a guardian already exists" a
    statement about the right obligations.  With a single session-wide name a
    guardian serving configuration A answered for a guardian serving
    configuration B, and the process that asked for the handover exited on that
    answer.
    """
    return f"{GUARDIAN_MUTEX_NAME}-{journal_identity(journal_path)}"


def _acquire_singleton(name: str = GUARDIAN_MUTEX_NAME) -> "_SingletonLock | None":
    """Own the guardian mutex of one journal, or None when it is already taken.

    Without it two guardians spawned from two shutdowns would both retry the same
    record; the journal lease would keep them from corrupting it, but the extra
    restore attempts on a possibly stalled foreign window are pure noise.

    The mutex is a kernel object, so a crashed guardian cannot leave a stale
    owner behind: the name disappears with its last handle.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):  # pragma: no cover - non-Windows
        # Ownership cannot be expressed; a duplicate guardian is still safe
        # because the journal lease is what protects the record.
        return _SingletonLock(None)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, str(name))
    if not handle:
        return _SingletonLock(None)
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return _SingletonLock(handle)


_STILL_ACTIVE = 259


def _posix_process_is_alive(pid: int) -> bool:
    """Liveness outside Windows, so the guardian's decisions stay testable.

    A signal-less probe is the portable equivalent of GetExitCodeProcess: the
    process is gone when the kernel no longer knows the PID, and it is still
    running (or owned by somebody else) when the probe is refused.
    """
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_is_alive(pid: int) -> bool:
    """True only while ``pid`` is still running (a dead PID can be reused)."""
    if not pid or int(pid) <= 0:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (OSError, AttributeError):
        # Not Windows: the decision still has to be honest, so probe the PID
        # instead of assuming every process is alive forever.
        return _posix_process_is_alive(pid)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, int(pid))  # QUERY_LIMITED|SYNCHRONIZE
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _signal_ready(stream=None, token: bytes = GUARDIAN_READY_TOKEN, detail: str = "", redirect: bool = True) -> None:
    """Tell the handing-off process that this guardian is now executing."""
    target = stream if stream is not None else sys.stdout.buffer
    payload = token + (b" " + detail.encode("ascii", "replace") if detail else b"") + b"\n"
    try:
        target.write(payload)
        target.flush()
    except (OSError, ValueError):  # pragma: no cover - handshake is best effort
        pass
    if not redirect:
        return
    try:
        # The pipe has served its purpose; keep nothing else on stdout so a
        # late write can never raise inside the guardian.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    except OSError:  # pragma: no cover - defensive
        pass


def _announce_handover_successor(executor: ExecutorIdentity, journal_id: str) -> None:
    """Tell the predecessor that a successor exists and is committed to take over.

    The pipe is deliberately left open: this process announces ownership itself a
    moment later, and the predecessor waits for that second announcement before it
    lets go of anything.
    """
    _signal_ready(
        token=GUARDIAN_STARTED_TOKEN,
        detail=f"successor={executor.short_id} journal={journal_id}",
        redirect=False,
    )


def _await_mutex_for_handover(executor: ExecutorIdentity, mutex_name: str) -> "_SingletonLock | None":
    """Wait for the predecessor to release the singleton, then take it."""
    deadline = time.monotonic() + HANDOVER_MUTEX_WAIT_SEC
    while True:
        singleton = _acquire_singleton(mutex_name)
        if singleton is not None:
            return singleton
        if time.monotonic() >= deadline:
            logger.error(
                "Recovery guardian successor %s could not inherit the executor mutex "
                "%s within %.1fs",
                executor.describe(),
                mutex_name,
                HANDOVER_MUTEX_WAIT_SEC,
            )
            return None
        time.sleep(POLL_SEC)


def await_initial_state(journal: RecoveryJournal, executor: ExecutorIdentity) -> bool:
    """Read the journal once under control, before readiness may be announced.

    Announcing readiness on an executor that has not yet read anything is how a
    guardian confirmed a takeover and then crashed on its first parse.  A journal
    that cannot be read is not a reason to confirm anything: the caller keeps its
    obligation and this process keeps waiting.
    """
    deadline = time.monotonic() + GUARDIAN_INITIAL_READ_SEC
    attempt = 0
    while True:
        try:
            journal.reload()
            return True
        except JournalReadError:
            attempt += 1
            throttled(
                "initial-read",
                "Recovery guardian %s cannot read the recovery journal yet; "
                "readiness is not being announced",
                executor.describe(),
                level=logging.ERROR,
            )
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(POLL_SEC * 2, backoff_delay(attempt), JOURNAL_UNREADABLE_POLL_CEILING_SEC))


@dataclass(frozen=True)
class DamageResolution:
    """What resolving a damaged journal actually established.

    ``decided`` is the only thing that may lift the damage, and it means *all* of:
    the sweep ran, the desktop could be enumerated, and every window it found came
    back.  A restore that did not work and a desktop that could not be listed are
    both unfinished work, so both keep the journal damaged and the executor alive.
    """

    recovered: tuple[int, ...] = ()
    pending: tuple[int, ...] = ()
    unknown: tuple[int, ...] = ()
    decided: bool = False
    reason: str = ""

    @property
    def outstanding_hwnds(self) -> tuple[int, ...]:
        return tuple(sorted({*self.pending, *self.unknown}))


def _sweep_result(module, exclude=()) -> DamageResolution:
    """Ask the Win32 layer to sweep, and read its answer as a result.

    A layer that cannot sweep (or a sweep that raised) decides nothing: the
    journal stays damaged and the caller keeps retrying.
    """
    sweep = getattr(module, "recover_all_orphaned_parks", None)
    if not callable(sweep):
        # A layer that cannot enumerate the desktop decides nothing.
        return DamageResolution(reason="the Win32 layer cannot sweep orphaned parks")
    try:
        raw = sweep(exclude=exclude)
    except Exception:  # pragma: no cover - the sweep must never kill the guardian
        logger.exception("Orphan recovery sweep failed")
        return DamageResolution(reason="the orphan recovery sweep failed")
    # A stand-in may still answer with the plain list of recovered handles.
    if isinstance(raw, (list, tuple, set, frozenset)):
        recovered = tuple(int(hwnd) for hwnd in raw or ())
        return DamageResolution(recovered=recovered, reason="sweep did not report enumeration completeness")
    return DamageResolution(
        recovered=tuple(int(hwnd) for hwnd in getattr(raw, "recovered", ()) or ()),
        pending=tuple(int(hwnd) for hwnd in getattr(raw, "pending", ()) or ()),
        unknown=tuple(int(hwnd) for hwnd in getattr(raw, "unknown", ()) or ()),
        decided=bool(getattr(raw, "decided", False)),
        reason=str(getattr(raw, "describe", lambda: "")()),
    )


def resolve_journal_damage(journal: RecoveryJournal, *, module=None) -> DamageResolution:
    """Resolve a journal that could not be read, and report it honestly.

    Quarantine keeps the damaged document, but keeping a file is not keeping an
    *executable* obligation: a window whose only record was inside it would have
    nobody left to restore it, and a journal that then reports "no obligations"
    turns that loss into a silent one.  So damage is resolved explicitly - the
    salvaged entries are attempted, then every window still sitting where LookUp
    parked it is brought back - and only afterwards is the journal allowed to
    report itself as having nothing outstanding.

    The acknowledgement is *earned*: it happens when the sweep decided, and a sweep
    that could not decide, or that left a window where it was, keeps the journal
    damaged.  An unreadable journal is not a reason to claim that nothing is parked.
    """
    try:
        initial = journal.snapshot()
    except (JournalReadError, OSError) as exc:
        logger.error(
            "The damaged recovery journal %s could not be re-read while resolving damage: %s",
            journal.path,
            exc,
        )
        return DamageResolution(reason="the recovery journal could not be re-read")
    if not initial.damaged:
        return DamageResolution(decided=True, reason="the journal is not damaged")
    reason = journal.damage_reason or "unknown damage"
    logger.error(
        "The recovery journal %s is unreadable (%s); recovering the windows LookUp "
        "left off-screen before anything is reported as empty",
        journal.path,
        reason,
    )
    layer = module if module is not None else winapi()
    salvaged = list(initial.unresolved)
    known = {int(record.hwnd) for record in salvaged}
    # The quarantined document is kept precisely so its obligations stay
    # executable: reading it back gives the sweep the exact windows - and their own
    # parking rectangles - instead of a guess from the current monitor layout.
    try:
        quarantined = tuple(quarantined_entries(journal.path))
    except (OSError, JournalReadError):
        logger.exception("Recovery journal quarantine enumeration failed for %s", journal.path)
        quarantined = ()
    for item in quarantined:
        salvage = salvage_record(item)
        if salvage is not None and int(salvage.hwnd) not in known:
            known.add(int(salvage.hwnd))
            salvaged.append(salvage)
    if salvaged:
        logger.warning(
            "The damaged recovery journal %s still names %s window(s) in its quarantined "
            "document; they are recovered by record, not by position",
            journal.path,
            len(salvaged),
        )
    try:
        pending = {int(record.hwnd) for record in journal.reload()}
    except JournalReadError:
        pending = set()
    recovered: list[int] = []
    still_parked: list[int] = []
    for record in salvaged:
        if int(record.hwnd) in pending:
            continue
        try:
            status = assess(layer, record)
            if status in (STATUS_GONE, STATUS_REUSED, STATUS_VISIBLE):
                continue
            restored = status != STATUS_UNVERIFIED and try_restore(layer, record)
        except Exception:
            logger.exception(
                "Damage recovery failed while assessing/restoring %s; the obligation stays open",
                record.describe(),
            )
            restored = False
        if restored:
            recovered.append(int(record.hwnd))
        else:
            still_parked.append(int(record.hwnd))
            logger.error(
                "A window from the damaged journal (%s) is still not back; it may be "
                "off-screen and nobody else knows about it",
                record.describe(),
            )
    sweep = _sweep_result(layer, exclude=pending)
    recovered.extend(sweep.recovered)
    still_parked.extend(hwnd for hwnd in sweep.outstanding_hwnds if hwnd not in still_parked)
    if recovered:
        logger.warning(
            "Recovered %s window(s) that only the damaged journal knew about: %s",
            len(recovered),
            sorted(recovered),
        )
    outstanding = tuple(sorted(set(still_parked)))
    decided = sweep.decided and not outstanding
    if decided:
        if journal.acknowledge_damage(expected_token=initial.damage_token):
            return DamageResolution(recovered=tuple(recovered), decided=True)
        logger.error(
            "The damaged recovery journal %s could not record the acknowledgement; "
            "its obligations stay unresolved and this executor keeps trying",
            journal.path,
        )
        return DamageResolution(
            recovered=tuple(recovered),
            pending=outstanding,
            reason="the acknowledgement could not be committed",
        )
    logger.error(
        "The damaged recovery journal %s could not be swept (%s); its obligations stay "
        "unresolved and this executor keeps trying",
        journal.path,
        sweep.reason or "a window did not come back",
    )
    return DamageResolution(
        recovered=tuple(recovered),
        pending=outstanding or sweep.pending or sweep.unknown,
        reason=sweep.reason or "a window did not come back",
    )


class _Guardian:
    """The recovery executor: claim, restore, verify, and never walk away.

    One instance owns at most one worker thread per outstanding record, so a
    target that never answers can neither stall another restore nor keep the
    process busy-spinning.
    """

    def __init__(
        self,
        journal: RecoveryJournal,
        *,
        executor: ExecutorIdentity,
        partner_pid: int = 0,
        partner_created: int = 0,
        partner_run_id: str = "",
        singleton=None,
        mutex_name: str = GUARDIAN_MUTEX_NAME,
    ):
        self.journal = journal
        self.executor = executor
        self.partner_pid = int(partner_pid or 0)
        self.partner_created = int(partner_created or 0) or None
        self.partner_run_id = partner_run_id or ""
        self.singleton = singleton
        self.mutex_name = mutex_name
        self.stop = threading.Event()
        self._workers: dict[int, threading.Thread] = {}
        self._idle_since: float | None = None
        self._partner_dead_at: float | None = None
        self._unreadable_attempts = 0
        self._damage_attempts = 0
        self._next_damage_attempt = 0.0

    # ------------------------------------------------------------------ #
    def partner_finished(self, now: float) -> bool:
        """Whether the process that handed the obligations over is really done.

        The partner is matched by identity, not by PID: a recycled PID must not
        keep this guardian alive for an owner that is long gone.
        """
        if not self.partner_pid or self.partner_pid == self.executor.pid:
            return True
        if process_is_alive(self.partner_pid):
            if self.partner_created and process_creation_time(self.partner_pid) != self.partner_created:
                throttled(
                    "partner-reused",
                    "Recovery guardian: handover partner pid=%s was replaced by another "
                    "process; continuing without it",
                    self.partner_pid,
                    level=logging.WARNING,
                )
                if self._partner_dead_at is None:
                    self._partner_dead_at = now
                return now - self._partner_dead_at >= HANDOFF_PARTNER_GRACE_SEC
            self._partner_dead_at = None
            return False
        if self._partner_dead_at is None:
            self._partner_dead_at = now
        return now - self._partner_dead_at >= HANDOFF_PARTNER_GRACE_SEC

    def _claimable(self, record: ParkRecord) -> bool:
        """Whether this guardian may work on ``record`` right now."""
        return not owner_is_alive(record)

    def _leased_elsewhere(self, record: ParkRecord) -> bool:
        """Whether another executor is working on ``record`` at this moment.

        Such a record is not claimable *and* not finished: the worker that owns
        the lease may still be blocked inside a native call, and this guardian
        has just been started next to it.  Counting it as "nothing outstanding"
        is how a late-finishing startup worker left its window behind.
        """
        return record.lease_is_live(self.executor)

    def run(self) -> int:
        while True:
            outcome = self._pass()
            if outcome is not None:
                return outcome
            # The only way out of the loop is a stop request.  It may only end
            # this process once the obligations are somebody else's.
            if self.stop.is_set():
                handed = self._hand_off_on_stop()
                if handed:
                    return 0
                # The handover failed: this process is still the only executor,
                # so it keeps working instead of walking away from a window.
                self.stop.clear()

    def _pass(self) -> int | None:
        """One polling pass.  ``None`` means "keep going"."""
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                snapshot = self.journal.snapshot()
            except JournalReadError:
                # An unknown journal state is not an exit condition.  The
                # obligation outlives this read, and the only honest exits are a
                # proven empty journal and a confirmed successor, so the executor
                # stays, backing off and reporting the state it cannot see.
                self._unreadable_attempts += 1
                throttled(
                    "unreadable",
                    "Recovery guardian cannot read the recovery journal; the state of "
                    "outstanding obligations is unknown and this executor stays",
                    level=logging.ERROR,
                )
                self.stop.wait(
                    min(POLL_SEC * self._unreadable_attempts, JOURNAL_UNREADABLE_POLL_CEILING_SEC)
                )
                continue
            self._unreadable_attempts = 0
            self._handle_damage()
            records = snapshot.records
            mine = [record for record in records if self._claimable(record)]
            outstanding = bool(mine) or any(
                self._leased_elsewhere(record) for record in records
            )
            # Unresolved damage is unfinished work of its own.  An empty record list
            # next to a document nobody can read means "nobody knows what was
            # parked", and that is the opposite of "there is nothing to restore".
            outstanding = outstanding or snapshot.damaged
            if not outstanding:
                if self.partner_finished(now):
                    if self._idle_since is None:
                        self._idle_since = now
                    elif now - self._idle_since >= GUARDIAN_IDLE_EXIT_SEC:
                        logger.info(
                            "Recovery guardian %s finished: no outstanding obligations",
                            self.executor.describe(),
                        )
                        return 0
                else:
                    self._idle_since = None
                # The partner is still alive, so a park may still be registered: wait
                # for one instead of re-reading the journal as fast as possible.
                self.stop.wait(GUARDIAN_IDLE_POLL_SEC)
                continue
            self._idle_since = None
            for record in mine:
                self._ensure_worker(record)
            time.sleep(POLL_SEC)
        return None

    def _handle_damage(self) -> None:
        """Deal with a journal that could not be read completely.

        Until this has happened the journal keeps reporting that its obligations
        are unresolved, which is what stops any executor from concluding that
        there is nothing left to restore.  A sweep that could not decide is not
        an answer, so it is retried on a backoff instead of being latched.
        """
        if self.journal.status not in DAMAGED_JOURNAL_STATUSES:
            self._damage_attempts = 0
            self._next_damage_attempt = 0.0
            return
        now = time.monotonic()
        if now < self._next_damage_attempt:
            return
        self._damage_attempts += 1
        self._next_damage_attempt = now + min(
            2.0 * self._damage_attempts, JOURNAL_UNREADABLE_POLL_CEILING_SEC * 4
        )
        try:
            resolve_journal_damage(self.journal)
        except Exception:  # never let damage bookkeeping kill the only executor
            logger.exception(
                "Recovery guardian damage pass failed for %s; keeping the executor alive",
                self.journal.path,
            )

    def _ensure_worker(self, record: ParkRecord) -> None:
        hwnd = int(record.hwnd)
        worker = self._workers.get(hwnd)
        if worker is not None and worker.is_alive():
            return

        def start(target: ParkRecord = record) -> None:
            try:
                resolve(
                    self.journal,
                    target,
                    self.executor,
                    context="guardian",
                    stop=self.stop,
                )
            except Exception:  # pragma: no cover - defensive
                logger.exception("Recovery guardian worker crashed for hwnd=%s", hwnd)
            finally:
                current = threading.current_thread()
                if self._workers.get(hwnd) is current:
                    self._workers.pop(hwnd, None)

        thread = threading.Thread(target=start, name=f"Guardian-{hwnd}", daemon=True)
        self._workers[hwnd] = thread
        thread.start()

    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        self.stop.set()

    def _hand_off_on_stop(self) -> bool:
        """Hand every live obligation over before this process stops working.

        Unresolved damage counts as live work: a document that could not be read
        may still describe a parked window, so "no live records" is not the same
        answer as "nothing left to do", and stopping here would end the only
        executor that was retrying the sweep.
        """
        for thread in list(self._workers.values()):
            thread.join(timeout=1.0)
        try:
            snapshot = self.journal.snapshot()
        except JournalReadError:
            logger.error("Recovery guardian was asked to stop while the journal was unreadable")
            return False
        live = [record for record in snapshot.outstanding if self._claimable(record)]
        if not live and snapshot.damaged:
            logger.error(
                "Recovery guardian was asked to stop with an unresolved recovery journal "
                "(%s); the damage is still outstanding work",
                snapshot.damage or "unknown damage",
            )
        if not live and not snapshot.damaged:
            return True
        if self.handover():
            return True
        logger.error(
            "Recovery guardian %s could not hand %s outstanding obligation(s) over; "
            "continuing to execute them",
            self.executor.describe(),
            len(live) or 1,
        )
        return False

    def handover(self) -> bool:
        """Transfer every live obligation to a confirmed successor executor.

        The order matters: the successor must exist and be committed before this
        process gives anything up, and the executor mutex is released only after
        the leases are.  If the successor never confirms, the mutex is taken back
        and this guardian keeps working - an obligation may never be left parked
        with nobody left to restore it.
        """
        process = spawn_guardian(
            self.journal.path,
            # The successor inherits *this* executor's identity as its partner, so
            # it keeps watching for park operations this process may still write
            # instead of concluding from one empty read that there is no work.
            owner_pid=self.executor.pid,
            owner_created=self.executor.created or 0,
            owner_run_id=self.executor.executor_id,
            handover_from=self.executor.executor_id,
            timeout=HANDOVER_START_TIMEOUT_SEC,
        )
        if process is None:
            return False
        # The successor is alive and waiting for our mutex: releasing the leases
        # now can no longer leave an obligation unowned for longer than the
        # confirmation window below.
        self.journal.release_all(self.executor)
        self.release_singleton()
        if _confirm_handover(
            process,
            journal_id=self.journal.identity,
            predecessor=self.executor.executor_id,
        ):
            logger.info(
                "Recovery guardian %s handed its obligations to pid=%s",
                self.executor.describe(),
                process.pid,
            )
            return True
        # The successor never confirmed ownership.  If somebody holds the executor
        # mutex now, the obligations do have a live executor and we may leave;
        # otherwise take the mutex back and keep working.
        again = _acquire_singleton(self.mutex_name)
        if again is None:
            logger.info(
                "Recovery guardian successor %s owns the executor mutex %s; this "
                "guardian %s may leave",
                process.pid,
                self.mutex_name,
                self.executor.describe(),
            )
            return True
        self.singleton = again
        return False

    def release_singleton(self) -> None:
        singleton, self.singleton = self.singleton, None
        if singleton is not None:
            singleton.close()

    def cleanup(self) -> None:
        self.release_singleton()
        for thread in list(self._workers.values()):
            thread.join(timeout=0.1)
        try:
            # Free every lease this process still holds so that a later start (or
            # a guardian spawned by it) can continue immediately.  The records
            # themselves stay: they are obligations, not lease bookkeeping.
            self.journal.release_all(self.executor)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Guardian lease release failed", exc_info=True)


def _confirm_handover(
    process: subprocess.Popen,
    *,
    journal_id: str = "",
    predecessor: str = "",
) -> bool:
    """Wait for the successor to announce that it owns this journal's mutex.

    The confirmation is checked, not assumed: the token has to name *this*
    journal, and it must come from an executor other than the process that is
    giving up.  A "ready" that names a different journal, or that simply repeats
    our own id, is not a handover - it is the same false confirmation that used
    to let the main process exit on somebody else's success.
    """
    link = _link_for(process)
    deadline = time.monotonic() + HANDOVER_CONFIRM_TIMEOUT_SEC
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        line = link.wait_line(min(remaining, 0.5))
        if line is None:
            if link._eof or not link.usable:
                logger.error(
                    "Recovery guardian successor (pid %s) closed its handshake before "
                    "confirming the handover",
                    process.pid,
                )
                link.close()
                return False
            if process.poll() is not None:
                logger.error(
                    "Recovery guardian successor (pid %s) exited before confirming the handover",
                    process.pid,
                )
                link.close()
                return False
            continue
        if not _line_is(line, GUARDIAN_READY_TOKEN):
            continue
        detail = _line_detail(line, GUARDIAN_READY_TOKEN)
        owner = detail.get("owner", "")
        announced = detail.get("journal", "")
        if journal_id and announced != journal_id:
            logger.error(
                "Recovery guardian successor (pid %s) confirmed a handover for journal "
                "%r instead of %r; the obligations stay with this executor",
                process.pid,
                announced,
                journal_id,
            )
            link.close()
            return False
        if not owner:
            logger.error(
                "Recovery guardian successor (pid %s) did not name the executor that owns "
                "the handover; the obligations stay with this executor",
                process.pid,
            )
            link.close()
            return False
        if predecessor and owner == predecessor:
            logger.error(
                "Recovery guardian successor (pid %s) echoed this executor's own identity; "
                "the handover is not confirmed",
                process.pid,
            )
            link.close()
            return False
        link.close()
        return True
    logger.error(
        "Recovery guardian successor (pid %s) did not confirm the handover within %.0fs",
        process.pid,
        HANDOVER_CONFIRM_TIMEOUT_SEC,
    )
    link.close()
    return False


def run_guardian(
    journal_path,
    *,
    owner_pid: int = 0,
    owner_created: int = 0,
    owner_run_id: str = "",
    handover_from: str = "",
    announce: bool = True,
    executor: ExecutorIdentity | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Execute every outstanding park obligation until there is nothing left.

    The guardian is single-shot with respect to *work*: it owns the leftovers of
    one LookUp process and leaves once the journal is empty (or once its partner
    finished and every remaining record belongs to a live owner).  It has no
    lifetime limit while an obligation is outstanding, and it never gives one up
    without a confirmed successor.
    """
    identity = executor or new_executor_identity("guardian")
    journal = RecoveryJournal(Path(journal_path))
    journal_id = journal.identity
    mutex_name = guardian_mutex_name(journal.path)
    if handover_from:
        _announce_handover_successor(identity, journal_id)
    singleton = _acquire_singleton(mutex_name)
    waited_for_handover = False
    if singleton is None and handover_from:
        waited_for_handover = True
        singleton = _await_mutex_for_handover(identity, mutex_name)
    if singleton is None:
        if waited_for_handover:
            # During a handover the first holder is the predecessor itself.  A
            # timeout does not prove that a *different* live guardian owns the
            # journal, so announcing "delegate" would let both successor and
            # predecessor exit with no executor.  Stay silent; the predecessor
            # will either observe a real mutex owner or reacquire it and continue.
            logger.error(
                "Recovery guardian successor %s timed out waiting for executor mutex %s; "
                "handover is not confirmed",
                identity.describe(),
                mutex_name,
            )
            return 2
        # Normal startup only: a pre-existing mutex genuinely means another
        # guardian already executes this journal, so delegation is valid.
        logger.info(
            "Another recovery guardian is already executing %s; %s has nothing to do",
            journal.path,
            identity.describe(),
        )
        if announce:
            _signal_ready(detail=f"delegate journal={journal_id}")
        return 0
    if "guardian_ready_stall" in test_hooks():
        # Runtime release gate: a live frozen child that never sends readiness
        # must be cleaned up without defeating the parent's timeout.
        time.sleep(120.0)
    announced = False
    if announce:
        # Readiness is a promise that this process is *executing*, so it is only
        # made once the executor is initialised and the journal has been read
        # under control.  A process that confirms and then crashes on its first
        # parse has confirmed nothing.
        if await_initial_state(journal, identity):
            _signal_ready(detail=f"owner={identity.executor_id} journal={journal_id}")
            announced = True
        else:
            logger.error(
                "Recovery guardian %s could not read %s within %.0fs; no handover is "
                "confirmed and this executor keeps working",
                identity.describe(),
                journal.path,
                GUARDIAN_INITIAL_READ_SEC,
            )
    guardian = _Guardian(
        journal,
        executor=identity,
        partner_pid=owner_pid,
        partner_created=owner_created,
        partner_run_id=owner_run_id,
        singleton=singleton,
        mutex_name=mutex_name,
    )
    if stop is not None:
        guardian.stop = stop
    try:
        return guardian.run()
    finally:
        guardian.cleanup()
        if not announced and announce:
            logger.error(
                "Recovery guardian %s finished without ever confirming readiness",
                identity.describe(),
            )


def run_guardian_from_argv(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if GUARDIAN_ARG not in args:
        return 2
    index = args.index(GUARDIAN_ARG)
    path = args[index + 1] if index + 1 < len(args) else ""
    if not path:
        sys.stderr.write(f"{GUARDIAN_ARG} requires the journal path\n")
        return 2
    positional = [part for part in args[index + 2 :] if not part.startswith("--")]
    owner_pid = _int_arg(positional, 0)
    owner_created = _int_arg(positional, 1)
    owner_run_id = _text_arg(positional, 2)
    handover_from = ""
    if "--handover-from" in args:
        at = args.index("--handover-from")
        handover_from = args[at + 1] if at + 1 < len(args) else ""
    return run_guardian(
        path,
        owner_pid=owner_pid,
        owner_created=owner_created,
        owner_run_id=owner_run_id,
        handover_from=handover_from,
    )


def _int_arg(values: list[str], index: int) -> int:
    if len(values) <= index:
        return 0
    try:
        return int(values[index])
    except (TypeError, ValueError):
        return 0


def _text_arg(values: list[str], index: int) -> str:
    return values[index] if len(values) > index else ""


def wait_for_handover(journal: RecoveryJournal, timeout: float) -> bool:
    """Bounded wait for outstanding records to drain without a guardian.

    Used as the fail-closed fallback: if no guardian could be started, the
    application stays alive while it still owns a parked window instead of
    exiting and leaving the user with a lost window.

    Damage counts as outstanding: a document nobody can read may still describe a
    parked window, so it is not "drained" until somebody has swept it.
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        try:
            if journal.snapshot().proven_complete:
                return True
        except JournalReadError:
            pass
        time.sleep(POLL_SEC)
    try:
        return journal.snapshot().proven_complete
    except JournalReadError:
        return False


if __name__ == "__main__":
    raise SystemExit(run_guardian_from_argv())
