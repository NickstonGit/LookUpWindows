from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("lookupwindows")

# Schema of the on-disk journal.
#
# Version 2 adds the identity and fencing fields that make recovery ownership
# provable: ``ownerRunId``/``ownerCreated`` (a PID is a locator, not an
# identity), ``claimExecutor``/``claimGeneration``/``claimToken`` (a fencing
# token, so a stalled executor cannot mutate a newer claim) and an explicit
# ``state``.  Version 1 documents are migrated in place, inside the very
# transaction that read them - never by a separate read/convert/write.
JOURNAL_VERSION = 2
LEGACY_JOURNAL_VERSIONS = (1,)
SUPPORTED_JOURNAL_VERSIONS = (*LEGACY_JOURNAL_VERSIONS, JOURNAL_VERSION)

# A claim is a lease, not a lock: the owner has to renew it while it works.
CLAIM_LEASE_SEC = 30.0

MAX_JOURNAL_BYTES = 1024 * 1024
MAX_RECORDS = 512
# One writer must never produce a document its own reader would refuse.  Every
# field is therefore bounded to the same limit both sides use, and the writer
# checks the record count, the serialised size and the round trip *before* the
# active journal is replaced (see ``RecoveryJournal._write``).
MAX_CLASS_CHARS = 256
MAX_PROCESS_NAME_CHARS = 260
MAX_LABEL_CHARS = 512
MAX_OPERATION_ID_CHARS = 64
QUARANTINE_SUFFIX = ".invalid"
# A quarantined document that has been swept is renamed, never deleted: the
# document is the forensic trace of what an earlier process believed it owned, and
# the new name is what makes "this damage has been dealt with" durable for every
# later reader instead of only for the process that did the sweep.
QUARANTINE_SWEEPT_SUFFIX = ".swept"

# What the newest document actually says.  "Unreadable" and "empty" are very
# different answers: a quarantined journal does not prove that no window is
# parked, it proves that this process can no longer tell.
JOURNAL_STATUS_EMPTY = "empty"
JOURNAL_STATUS_VALID = "valid"
JOURNAL_STATUS_DEGRADED = "degraded"
JOURNAL_STATUS_UNRESOLVED = "unresolved"
DAMAGED_JOURNAL_STATUSES = (JOURNAL_STATUS_DEGRADED, JOURNAL_STATUS_UNRESOLVED)

# The journal is shared state: the application, a recovery guardian handed the
# leftovers at shutdown, and a second LookUp start may all write it at the same
# moment.  Serialising only inside one process would let a stale in-memory copy
# overwrite a newer obligation, so every read-modify-write cycle runs under one
# stable inter-process lock (see ``_InterProcessJournalLock``).
JOURNAL_LOCK_SUFFIX = ".lock"
JOURNAL_LOCK_TIMEOUT_SEC = 15.0
JOURNAL_LOCK_POLL_SEC = 0.01
# The in-process half of the journal lock used to be acquired with an untimed
# ``with``, so one slow transaction froze every other thread that touched the
# journal - including the UI thread that only wanted to look at a record.  The
# deadline now covers the in-process lock, the inter-process lock and the I/O, and
# running out of it means "unknown", never "empty".
JOURNAL_INPROCESS_LOCK_TIMEOUT_SEC = 5.0

# Result of a transaction that deliberately does not change the document.
_NO_WRITE = object()

# Ranges every recovery-control number has to fall into.  ``json.loads`` accepts
# ``Infinity``/``NaN`` by default, so a hand-edited or corrupted document can
# carry a lease that no executor may ever take over; rejecting the value keeps
# the record recoverable instead of locking it forever.
_HWND_LIMIT = 0xFFFFFFFF
_PID_LIMIT = 0xFFFFFFFF
_COORD_LIMIT = 1 << 20
# Instants recorded by us are Unix seconds: anything beyond a few thousand years
# is corruption, and anything negative cannot be a moment that already happened.
_EPOCH_SEC_LOW = 0.0
_EPOCH_SEC_HIGH = 1e11
# Instants reported by Win32 are FILETIME values (100ns intervals since 1601),
# which are a completely different scale - validating them against seconds would
# silently discard every real process creation time.
_FILETIME_LOW = 1
_FILETIME_HIGH = (1 << 63) - 1
_GENERATION_LIMIT = 1 << 62

# Explicit record states (the park state machine).  ``parked`` is derived, so
# the durable document carries one source of truth.
STATE_INTENT = "intent"
STATE_PARKED = "parked"
STATE_STALE = "stale"
RECORD_STATES = (STATE_INTENT, STATE_PARKED, STATE_STALE)

# Required fields of a durable record.  A record without them cannot be turned
# into a restore, so it is refused; a record *with* them but with nonsense in the
# optional fields is sanitised instead (see ``ParkRecord.from_dict``).
_REQUIRED_FIELDS = (
    "hwnd",
    "pid",
    "class",
    "processName",
    "screenRect",
    "showCmd",
    "placementFlags",
    "minPosition",
    "maxPosition",
    "normalPosition",
    "ownerPid",
    "recordedAt",
)


class JournalReadError(OSError):
    """The newest journal state is unknown; it must not be treated as empty."""


def _finite(value) -> float | None:
    """Return ``value`` as a finite float, or ``None`` when it is not a number.

    ``bool`` is refused on purpose: ``True`` is not a timestamp, and accepting it
    would silently produce ``1.0`` for a field that must be a real instant.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bounded_float(value, low: float, high: float, default=None):
    """A finite float inside ``[low, high]``, or ``default``."""
    number = _finite(value)
    if number is None or number < low or number > high:
        return default
    return number


def _bounded_int(value, low: int, high: int, default=None):
    """An integer inside ``[low, high]``, or ``default``.

    An ``int`` is used as-is and never passes through a float: Win32 creation
    times are 64-bit FILETIME values (about 1.3e17) and a float carries only 53
    bits of mantissa, so converting would corrupt the last digits of every real
    process identity and make a live window look like a reused one.
    """
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        number = value
    else:
        as_float = _finite(value)
        if as_float is None:
            return default
        number = int(as_float)
    if number < low or number > high:
        return default
    return number


def _safe_int(value, low: int, high: int) -> int:
    """``value`` as a bounded int, or ``0`` when it is not one at all.

    ``to_dict`` must be *total*: a record whose geometry or identity is corrupt
    has to produce a writable payload the reader can then refuse on its own
    terms, not an exception that unwinds the transaction that was trying to keep
    the journal honest.
    """
    number = _bounded_int(value, low, high)
    return 0 if number is None else number


def _safe_rect(values, size: int = 4) -> list[int]:
    """A rectangle the reader will accept, or ``[]`` for "unusable placement".

    The writer has to reach exactly the same conclusion as the reader about a
    corrupt coordinate: emitting a plausible zero instead would turn damage into
    a *trustworthy* 1x1 window at the origin, and the record would then be
    restored to a position the user never had.
    """
    if not isinstance(values, (list, tuple)) or len(values) != size:
        return []
    numbers: list[int] = []
    for value in values:
        number = _bounded_int(value, -_COORD_LIMIT, _COORD_LIMIT)
        if number is None:
            return []
        numbers.append(number)
    return numbers


def _optional_text(value, limit: int = 64) -> str:
    """A short identifier string; anything else is treated as absent.

    Run ids and claim tokens are compared for equality, so a value that is not a
    bounded string is corruption rather than a new identity.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > limit:
        return ""
    if any(character.isspace() for character in text):
        return ""
    return text


@dataclass(frozen=True)
class ExecutorIdentity:
    """Who is executing recovery, as distinct from where it can be found.

    ``executor_id`` is a random value created once per process, so two executors
    never collide even when the PID does.  ``created`` is the Win32 process
    creation time, which is what makes a recorded PID meaningful after the
    original process has gone.
    """

    executor_id: str
    pid: int = 0
    created: int | None = None
    label: str = ""

    @property
    def short_id(self) -> str:
        return (self.executor_id or "--------")[:8]

    def describe(self) -> str:
        return f"{self.short_id}@pid{self.pid}"


@dataclass(frozen=True)
class RecoverySnapshot:
    """One consistent answer about what this journal still owes.

    Records, salvaged entries and damage describe the *same* question, so they are
    read together: an executor that looks at only one of them is how "the list is
    empty" was mistaken for "there is nothing to restore".  Every exit, handover
    and stop decision is taken on this object instead of on a bare record list.
    """

    records: tuple["ParkRecord", ...] = ()
    unresolved: tuple["ParkRecord", ...] = ()
    status: str = JOURNAL_STATUS_EMPTY
    damage: str = ""
    damage_token: str = ""

    @property
    def damaged(self) -> bool:
        """Whether the document itself could not be read completely."""
        return self.status in DAMAGED_JOURNAL_STATUSES

    @property
    def outstanding(self) -> tuple["ParkRecord", ...]:
        """Every obligation this snapshot proves is still unexecuted.

        A readable record always wins over a salvaged entry for the same handle:
        it is the more trustworthy description of the same obligation.
        """
        merged: dict[int, ParkRecord] = {}
        for record in self.unresolved:
            merged[int(record.hwnd)] = record
        for record in self.records:
            merged[int(record.hwnd)] = record
        return tuple(merged.values())

    @property
    def proven_complete(self) -> bool:
        """Whether "nothing is outstanding" is *proved* rather than guessed.

        Damage is unfinished work in its own right: a document nobody can read may
        still describe a parked window, so it never authorises an exit.
        """
        return not self.damaged and not self.records and not self.unresolved

    def outstanding_hwnds(self) -> tuple[int, ...]:
        return tuple(sorted({int(record.hwnd) for record in self.outstanding}))


def new_run_id() -> str:
    """A cryptographically random 128-bit identifier for one application run."""
    return uuid.uuid4().hex


def new_executor_identity(label: str = "") -> ExecutorIdentity:
    """The identity of *this* process as a recovery executor."""
    pid = os.getpid()
    created: int | None = None
    try:
        import winapi

        created = winapi.get_process_creation_time(pid)
    except Exception:  # pragma: no cover - non-Windows, or a locked-down kernel
        created = None
    return ExecutorIdentity(
        executor_id=new_run_id(), pid=pid, created=created, label=label
    )


@dataclass(frozen=True)
class Claim:
    """Proof that this executor currently owns one obligation.

    Every mutation carries the whole claim: the executor identity, the fencing
    generation and the token.  A record whose claim no longer matches is
    somebody else's now, and the mutation is refused.
    """

    hwnd: int
    executor: ExecutorIdentity
    generation: int
    token: str
    recorded_at: float = 0.0

    @property
    def short_token(self) -> str:
        return (self.token or "")[:8]


def _as_executor(value) -> ExecutorIdentity:
    """Accept an :class:`ExecutorIdentity` or a bare PID.

    Production code always passes a real identity.  A bare PID is still accepted
    so that a diagnostic tool can act on behalf of a process it observed from
    the outside; it is *not* an identity claim (see :class:`ExecutorIdentity`)
    and it is deliberately given a synthetic executor id, so two callers that
    pass the same dead PID cannot inherit each other's lease.
    """
    if isinstance(value, ExecutorIdentity):
        return value
    pid = _bounded_int(value, 0, _PID_LIMIT, 0) or 0
    return ExecutorIdentity(executor_id=f"pid:{pid}", pid=pid, created=None, label="legacy")


def executor_of(record: "ParkRecord") -> ExecutorIdentity:
    """The identity a record was registered by.

    Used when the caller did not pass its own identity explicitly, so that the
    decision "is this lease mine?" is still made against a run id rather than
    against a bare PID.
    """
    return ExecutorIdentity(
        executor_id=record.owner_run_id or f"pid:{record.owner_pid}",
        pid=int(record.owner_pid),
        created=record.owner_created,
        label="owner",
    )


_sanitised: set[tuple[int, str]] = set()


def _report_once(key: tuple[int, str], message: str, *args) -> None:
    """Warn about a durable defect once per process, not once per transaction.

    A corrupt record is re-read by every poll of every executor; without this the
    recovery log would repeat one line forever while learning nothing new.
    """
    if key in _sanitised:
        return
    _sanitised.add(key)
    logger.warning(message, *args)


_process_locks: dict[str, _ProcessJournalLock] = {}
_process_locks_guard = threading.Lock()


class _ProcessJournalLock:
    """A re-entrant per-path lock whose acquisition is bounded.

    Serialising the journal inside one process is mandatory, but "wait forever" is
    not an acceptable answer on a path that can be reached from paint and input
    handling: a worker blocked inside a slow transaction used to freeze the whole
    application.  Timing out means the newest state is unknown, and every caller
    already treats that as a refusal rather than as "nothing to do".
    """

    def __init__(self, lock: threading.RLock):
        self._lock = lock

    def __enter__(self):
        if not self._lock.acquire(timeout=max(0.001, JOURNAL_INPROCESS_LOCK_TIMEOUT_SEC)):
            raise JournalReadError(
                "Recovery journal is busy inside this process; its state is unknown"
            )
        return self._lock

    def __exit__(self, *_exc) -> None:
        try:
            self._lock.release()
        except RuntimeError:  # pragma: no cover - defensive (never held)
            pass

    def acquire(self, *args, **kwargs) -> bool:
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()


def _process_lock_for(key: str) -> _ProcessJournalLock:
    """One in-process lock per journal path, shared by every instance.

    Two ``RecoveryJournal`` objects for the same document inside one process
    must not deadlock each other against the file lock, and must not be able to
    interleave their read-modify-write cycles either.
    """
    with _process_locks_guard:
        lock = _process_locks.get(key)
        if lock is None:
            lock = _ProcessJournalLock(threading.RLock())
            _process_locks[key] = lock
        return lock


class _InterProcessJournalLock:
    """A stable cross-process lock for one journal document.

    Locking the journal itself would protect nothing: every write replaces the
    document with ``os.replace``, so each writer would lock a different file
    object.  The lock therefore lives in its own file, which

    * is never replaced, so all writers serialize on the same resource;
    * is released by the operating system when a process dies, so a hard kill
      cannot leave a stale lock behind;
    * makes a read-modify-write cycle an actual transaction between processes.
    """

    def __init__(self, path: Path, timeout: float = JOURNAL_LOCK_TIMEOUT_SEC):
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self._process_lock = _process_lock_for(os.path.normcase(os.path.abspath(str(self.path))))
        self._fd: int | None = None

    def __enter__(self) -> "_InterProcessJournalLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    def acquire(self) -> bool:
        if not self._process_lock.acquire(timeout=max(0.001, self.timeout)):
            logger.error("Recovery journal lock %s is busy inside this process", self.path)
            return False
        fd = self._open()
        if fd is None:
            self._process_lock.release()
            return False
        self._fd = fd
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_lock():
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(JOURNAL_LOCK_POLL_SEC)
        logger.error(
            "Recovery journal %s stayed locked by another process for %.1fs",
            self.path,
            self.timeout,
        )
        self._close()
        self._process_lock.release()
        return False

    def release(self) -> None:
        self._close()
        try:
            self._process_lock.release()
        except RuntimeError:  # pragma: no cover - defensive (never locked)
            pass

    def _open(self) -> int | None:
        try:
            parent = self.path.parent
            if str(parent) and not parent.exists():
                parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o666)
        except OSError as exc:
            logger.error("Recovery journal lock %s could not be opened: %s", self.path, exc)
            return None
        try:
            # A byte has to exist before it can be locked on every platform.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
        except OSError:  # pragma: no cover - the lock itself still works
            pass
        return fd

    def _close(self) -> None:
        if self._fd is None:
            return
        try:
            self._try_unlock()
        except OSError:  # pragma: no cover - releasing must never raise
            pass
        try:
            os.close(self._fd)
        except OSError:  # pragma: no cover - defensive
            pass
        self._fd = None

    def _try_lock(self) -> bool:
        if self._fd is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                return True
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, ImportError):
            return False

    def _try_unlock(self) -> None:
        if self._fd is None:
            return
        if os.name == "nt":
            import msvcrt

            os.lseek(self._fd, 0, os.SEEK_SET)
            msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(self._fd, fcntl.LOCK_UN)


@dataclass
class ParkRecord:
    """Everything needed to put one foreign window back exactly as it was."""

    hwnd: int
    pid: int
    class_name: str
    process_name: str
    process_created: int | None
    screen_rect: tuple[int, int, int, int]
    show_cmd: int
    placement_flags: int
    min_position: tuple[int, int]
    max_position: tuple[int, int]
    normal_position: tuple[int, int, int, int]
    # Owner of the obligation: the LookUp run that registered the park.  The PID
    # is only a locator; ``owner_created`` plus ``owner_run_id`` are what make it
    # an identity that survives PID reuse.
    owner_pid: int
    recorded_at: float
    label: str = ""
    state: str = STATE_INTENT
    owner_run_id: str = ""
    owner_created: int | None = None
    # Identity of *this park operation*.  An HWND alone is not a lifecycle: the
    # same window is parked, restored and parked again, and only the operation
    # id tells an idempotent repeat of one park apart from a new obligation that
    # happens to target the same handle.
    operation_id: str = ""
    # Where the park put the window, and the virtual-desktop origin that position
    # was derived from.  Durable, because the parking *signature* computed from the
    # current topology stops matching as soon as a monitor is plugged in, removed
    # or re-arranged - and that is the only proof a record has once its own mark
    # on the window is gone.
    park_rect: tuple[int, int, int, int] = ()
    park_origin: tuple[int, int] = (0, 0)
    # Set when the recorded placement cannot be trusted.  The obligation itself is
    # kept - the window is still parked - but only the conservative orphan
    # restore may be used for it.
    placement_usable: bool = True
    # Execution ownership (a fenced lease, see :class:`Claim`).
    claim_pid: int = 0
    claim_until: float = 0.0
    claim_executor: str = ""
    claim_created: int | None = None
    claim_token: str = ""
    claim_generation: int = 0

    @property
    def parked(self) -> bool:
        """Whether the off-screen move is known to have happened."""
        return self.state == STATE_PARKED

    def _read_park_geometry(self, data: dict) -> None:
        """Load the recorded park position, keeping only a usable rectangle."""
        self.park_rect = tuple(_read_rect(data.get("parkRect")))
        origin = _read_point(data.get("parkOrigin"))
        self.park_origin = tuple(origin) if origin else (0, 0)

    @property
    def owner_identity(self) -> str:
        return self.owner_run_id or f"pid{self.owner_pid}"

    def describe(self) -> str:
        """Loggable identity of the target, never pixels."""
        created = self.process_created if self.process_created is not None else "?"
        return (
            f"hwnd={self.hwnd} pid={self.pid} created={created} "
            f"owner={self.owner_identity} state={self.state}"
        )

    def to_dict(self) -> dict:
        # Every text field is bounded *here*, at the writer, so the document the
        # journal produces is always inside the reader's limits.  A record whose
        # label was a megabyte long used to be written successfully and then
        # rejected by the very next read.
        return {
            "hwnd": _safe_int(self.hwnd, 0, _HWND_LIMIT),
            "pid": _safe_int(self.pid, 0, _PID_LIMIT),
            "class": self.class_name[:MAX_CLASS_CHARS],
            "processName": self.process_name[:MAX_PROCESS_NAME_CHARS],
            "processCreated": self.process_created,
            "screenRect": _safe_rect(self.screen_rect),
            "showCmd": _safe_int(self.show_cmd, 0, 0xFFFF),
            "placementFlags": _safe_int(self.placement_flags, 0, 0xFFFF),
            "minPosition": _safe_rect(self.min_position, 2),
            "maxPosition": _safe_rect(self.max_position, 2),
            "normalPosition": _safe_rect(self.normal_position),
            "ownerPid": _safe_int(self.owner_pid, 0, _PID_LIMIT),
            "ownerRunId": self.owner_run_id,
            "ownerCreated": self.owner_created,
            "recordedAt": _bounded_float(self.recorded_at, _EPOCH_SEC_LOW, _EPOCH_SEC_HIGH, 0.0),
            "label": self.label[:MAX_LABEL_CHARS],
            "state": self.state,
            "operationId": self.operation_id,
            "parkRect": _safe_rect(self.park_rect),
            "parkOrigin": _safe_rect(self.park_origin, 2),
            "placementUsable": bool(self.placement_usable),
            "claimPid": _safe_int(self.claim_pid, 0, _PID_LIMIT),
            "claimUntil": _bounded_float(self.claim_until, _EPOCH_SEC_LOW, _EPOCH_SEC_HIGH, 0.0),
            "claimExecutor": self.claim_executor,
            "claimCreated": self.claim_created,
            "claimToken": self.claim_token,
            "claimGeneration": _safe_int(self.claim_generation, 0, _GENERATION_LIMIT),
        }

    @classmethod
    def from_dict(cls, data, *, version: int = JOURNAL_VERSION) -> "ParkRecord | None":
        """Parse one durable record, sanitising what can be sanitised.

        Two different failures have to be told apart:

        * a record without the fields a restore needs is not a fact at all and is
          refused;
        * a record whose *optional* control values are corrupt (an ``Infinity``
          lease, a ``NaN`` coordinate) still describes a window that may be
          parked off-screen, so it is kept - with the unusable part neutralised.
          Refusing it would silently drop the only proof that a window needs to
          come back.
        """
        if not isinstance(data, dict):
            return None
        if any(field not in data for field in _REQUIRED_FIELDS):
            return None
        hwnd = _bounded_int(data["hwnd"], 1, _HWND_LIMIT)
        pid = _bounded_int(data["pid"], 0, _PID_LIMIT)
        owner_pid = _bounded_int(data["ownerPid"], 0, _PID_LIMIT)
        if hwnd is None or pid is None or owner_pid is None:
            return None
        recorded_at = _bounded_float(data["recordedAt"], _EPOCH_SEC_LOW, _EPOCH_SEC_HIGH)
        if recorded_at is None:
            # An obligation has no expiry, so a missing instant does not make it
            # obsolete; it only means its age is unknown.
            _report_once(
                (hwnd, "recordedAt"),
                "Recovery record for hwnd=%s carries an unusable recordedAt (%r); "
                "kept with an unknown age",
                hwnd,
                data["recordedAt"],
            )
            recorded_at = 0.0

        placement_usable, geometry = _read_geometry(data)
        screen_rect = geometry["screenRect"]
        min_position = geometry["minPosition"]
        max_position = geometry["maxPosition"]
        normal_position = geometry["normalPosition"]
        record = cls(
            hwnd=hwnd,
            pid=pid,
            class_name=_text(data["class"], MAX_CLASS_CHARS),
            process_name=_text(data["processName"], MAX_PROCESS_NAME_CHARS),
            # ``processCreated`` is deliberately *not* a required field: the code
            # supports an unknown creation time, so the key is read with ``get``
            # and an absent value means "identity unproven", never a KeyError
            # escaping the parser and taking the guardian down with it.
            process_created=_bounded_int(data.get("processCreated"), _FILETIME_LOW, _FILETIME_HIGH),
            screen_rect=screen_rect,
            show_cmd=_bounded_int(data["showCmd"], 0, 0xFFFF, 0) or 0,
            placement_flags=_bounded_int(data["placementFlags"], 0, 0xFFFF, 0) or 0,
            min_position=min_position,
            max_position=max_position,
            normal_position=normal_position,
            owner_pid=owner_pid,
            recorded_at=recorded_at,
            label=_text(data.get("label"), MAX_LABEL_CHARS),
            owner_run_id=_optional_text(data.get("ownerRunId")),
            owner_created=_bounded_int(data.get("ownerCreated"), _FILETIME_LOW, _FILETIME_HIGH),
            operation_id=_optional_text(data.get("operationId"), MAX_OPERATION_ID_CHARS),
            placement_usable=placement_usable,
        )
        record.state = _record_state(data, version=version)
        record._read_claim(data)
        record._read_park_geometry(data)
        return record

    def _read_claim(self, data: dict) -> None:
        """Load the fencing claim, dropping it entirely when it is corrupt.

        An unusable lease is reset rather than repaired: the safe reading of
        ``claimUntil = Infinity`` is "nobody proved they still hold it", and a
        fresh claim may be taken immediately.  Inventing an expiry instead would
        either lock the record forever or drop a lease that was genuinely held.
        """
        until = _bounded_float(data.get("claimUntil"), _EPOCH_SEC_LOW, _EPOCH_SEC_HIGH, 0.0)
        if until is None:
            _report_once(
                (self.hwnd, "claimUntil"),
                "Recovery record for hwnd=%s carried a non-finite claimUntil (%r); "
                "the claim was dropped so recovery can continue",
                self.hwnd,
                data.get("claimUntil"),
            )
            until = 0.0
        self.claim_until = until
        self.claim_pid = _bounded_int(data.get("claimPid"), 0, _PID_LIMIT, 0) or 0
        self.claim_executor = _optional_text(data.get("claimExecutor"))
        self.claim_token = _optional_text(data.get("claimToken"))
        self.claim_created = _bounded_int(data.get("claimCreated"), _FILETIME_LOW, _FILETIME_HIGH)
        generation = _bounded_int(data.get("claimGeneration"), 0, _GENERATION_LIMIT, 0) or 0
        self.claim_generation = generation
        if not self._is_claim_usable():
            _report_once(
                (self.hwnd, "claim"),
                "Recovery record for hwnd=%s carried an incomplete claim "
                "(executor=%r token=%r generation=%s); the claim was dropped",
                self.hwnd,
                self.claim_executor,
                self.claim_token,
                generation,
            )
            self.clear_claim()

    def _is_claim_usable(self) -> bool:
        if not self.claim_until:
            return True  # no lease
        return bool(self.claim_executor and self.claim_token and self.claim_generation > 0)

    def clear_claim(self) -> None:
        self.claim_pid = 0
        self.claim_until = 0.0
        self.claim_executor = ""
        self.claim_created = None
        self.claim_token = ""
        # The generation is never reset.  A released record therefore hands out a
        # strictly higher generation to its next claim, so an executor that
        # remembers generation N can never be mistaken for the holder of N+1.

    # ------------------------------------------------------------------ #
    # Ownership questions
    # ------------------------------------------------------------------ #
    def lease_is_live(self, executor: ExecutorIdentity, now: float | None = None) -> bool:
        """True while a *different* executor still holds this record's lease."""
        if not self.claim_until:
            return False
        if (time.time() if now is None else float(now)) >= self.claim_until:
            return False
        holder = self.claim_executor
        if holder:
            return holder != executor.executor_id
        # A claim without an executor identity is a claim from before identity
        # existed: it is only ours if the PID says so, and it expires like any
        # other lease.
        return bool(self.claim_pid) and int(self.claim_pid) != int(executor.pid)

    def claim_matches(self, claim: Claim) -> bool:
        """Fencing check: is ``claim`` still the current claim of this record?"""
        return (
            self.claim_executor == claim.executor.executor_id
            and self.claim_token == claim.token
            and int(self.claim_generation) == int(claim.generation)
            and int(self.hwnd) == int(claim.hwnd)
        )

    def may_be_retired_by(self, executor: ExecutorIdentity) -> bool:
        """Whether ``executor`` may drop the record without holding its claim.

        Only the run that registered the obligation, or the executor that
        explicitly claimed it, may retire it.  A *different* process that merely
        observes the window on screen is exactly the losing side of the
        "delayed park lands after the record was cleared" race, so it has to
        claim the record first - which is a fenced, serialized operation.
        """
        if self.claim_executor and self.claim_until:
            if self.claim_until > time.time():
                return self.claim_executor == executor.executor_id
        if self.owner_run_id:
            return self.owner_run_id == executor.executor_id
        # No owner identity on record: nobody may retire it passively.
        return False

    def age_sec(self) -> float:
        """How long ago the obligation was registered (diagnostics only).

        Age is never a reason to forget a record: only a verified restore or a
        window that is provably gone may discharge it.
        """
        if not self.recorded_at:
            return 0.0
        return max(0.0, time.time() - float(self.recorded_at))


def _text(value, limit: int) -> str:
    if isinstance(value, str):
        return value[:limit]
    return ""


def _record_state(data: dict, *, version: int = JOURNAL_VERSION) -> str:
    """Read the state machine position, migrating the legacy ``parked`` flag."""
    state = data.get("state")
    if isinstance(state, str) and state in RECORD_STATES:
        return state
    if version < JOURNAL_VERSION:
        # Version 1 had no state machine: its ``parked`` flag is exactly the
        # INTENT/PARKED distinction.
        return STATE_PARKED if bool(data.get("parked")) else STATE_INTENT
    if "parked" in data:
        _report_once(
            (_bounded_int(data.get("hwnd"), 0, _HWND_LIMIT, 0) or 0, "state"),
            "Recovery record carried a legacy parked flag without a usable state; "
            "mapped to %s",
            STATE_PARKED if data.get("parked") else STATE_INTENT,
        )
        return STATE_PARKED if bool(data.get("parked")) else STATE_INTENT
    return STATE_INTENT


def _coord(value) -> int | None:
    return _bounded_int(value, -_COORD_LIMIT, _COORD_LIMIT)


def _read_rect(value) -> list[int]:
    """A usable rectangle from the document, or ``[]`` for "not recorded"."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return []
    parts = [_coord(item) for item in value]
    if any(part is None for part in parts):
        return []
    left, top, right, bottom = parts
    if right <= left or bottom <= top:
        return []
    return [left, top, right, bottom]


def _read_point(value) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return []
    parts = [_coord(item) for item in value]
    if any(part is None for part in parts):
        return []
    return [parts[0], parts[1]]


def _read_geometry(data: dict) -> tuple[bool, dict]:
    """Read the placement of a record, flagging it unusable when it is corrupt."""
    geometry = {
        "screenRect": (0, 0, 0, 0),
        "minPosition": (-1, -1),
        "maxPosition": (-1, -1),
        "normalPosition": (0, 0, 0, 0),
    }
    usable = True

    def rect(field: str) -> tuple[int, int, int, int] | None:
        value = data.get(field)
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return None
        parts = [_coord(item) for item in value]
        if any(part is None for part in parts):
            return None
        left, top, right, bottom = parts
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)

    def point(field: str) -> tuple[int, int] | None:
        value = data.get(field)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        parts = [_coord(item) for item in value]
        if any(part is None for part in parts):
            return None
        return (parts[0], parts[1])

    screen_rect = rect("screenRect")
    normal_position = rect("normalPosition")
    min_position = point("minPosition")
    max_position = point("maxPosition")
    if screen_rect is not None:
        geometry["screenRect"] = screen_rect
    if normal_position is not None:
        geometry["normalPosition"] = normal_position
    if min_position is not None:
        geometry["minPosition"] = min_position
    if max_position is not None:
        geometry["maxPosition"] = max_position
    usable = screen_rect is not None and normal_position is not None
    if not usable:
        _report_once(
            (_bounded_int(data.get("hwnd"), 0, _HWND_LIMIT, 0) or 0, "placement"),
            "Recovery record for hwnd=%s has an unusable placement; the obligation is "
            "kept, but only a conservative restore may be attempted",
            _bounded_int(data.get("hwnd"), 0, _HWND_LIMIT, 0),
        )
    return usable, geometry


def _parse_record(item, version: int) -> "ParkRecord | None":
    """Parse one durable record, turning any parser failure into ``None``.

    A hand-edited or corrupted entry used to be able to raise out of
    ``from_dict`` (``KeyError``/``TypeError``) and take the whole read - and with
    it a guardian that had just announced readiness - down.  A record that cannot
    be parsed is data we do not have; it must never become an exception.
    """
    try:
        return ParkRecord.from_dict(item, version=version)
    except Exception:  # pragma: no cover - defensive: the parser is total
        logger.warning("Recovery journal record could not be parsed", exc_info=True)
        return None


def salvage_record(data) -> "ParkRecord | None":
    """Build a conservative record from a journal entry the strict parser refused.

    A damaged entry is not a licence to forget a window: it may still be the only
    proof that a foreign window was moved off-screen.  What it is *not* is a
    trustworthy placement, so the salvaged record is always marked
    ``placement_usable = False``.  That restricts recovery to the orphan path,
    which refuses to touch any window that does not carry LookUp's own parking
    signature - so salvaging cannot move a stranger, while a genuinely stranded
    window still comes back.
    """
    if not isinstance(data, dict):
        return None
    hwnd = _bounded_int(data.get("hwnd"), 1, _HWND_LIMIT)
    if hwnd is None:
        return None
    _usable, geometry = _read_geometry(data)
    record = ParkRecord(
        hwnd=hwnd,
        pid=_bounded_int(data.get("pid"), 0, _PID_LIMIT, 0) or 0,
        class_name=_text(data.get("class"), MAX_CLASS_CHARS),
        process_name=_text(data.get("processName"), MAX_PROCESS_NAME_CHARS),
        process_created=_bounded_int(data.get("processCreated"), _FILETIME_LOW, _FILETIME_HIGH),
        screen_rect=geometry["screenRect"],
        show_cmd=_bounded_int(data.get("showCmd"), 0, 0xFFFF, 0) or 0,
        placement_flags=_bounded_int(data.get("placementFlags"), 0, 0xFFFF, 0) or 0,
        min_position=geometry["minPosition"],
        max_position=geometry["maxPosition"],
        normal_position=geometry["normalPosition"],
        owner_pid=_bounded_int(data.get("ownerPid"), 0, _PID_LIMIT, 0) or 0,
        recorded_at=_bounded_float(data.get("recordedAt"), _EPOCH_SEC_LOW, _EPOCH_SEC_HIGH, 0.0) or 0.0,
        label=_text(data.get("label"), MAX_LABEL_CHARS),
        state=_record_state(data),
        owner_run_id=_optional_text(data.get("ownerRunId")),
        owner_created=_bounded_int(data.get("ownerCreated"), _FILETIME_LOW, _FILETIME_HIGH),
        operation_id=_optional_text(data.get("operationId"), MAX_OPERATION_ID_CHARS),
        placement_usable=False,
    )
    record._read_claim(data)
    record._read_park_geometry(data)
    return record


def _finite_safe(value, depth: int = 0):
    """A JSON-serialisable copy of a value taken from a damaged document.

    ``json.loads`` accepts ``NaN``/``Infinity``, while the writer serialises with
    ``allow_nan=False``.  A preserved entry that cannot be written back would
    freeze the entire journal - every later claim of every record refused - which
    is a far worse failure than losing one corrupt value, so a non-finite number
    is preserved as text instead.  Depth and key types are bounded for the same
    reason.
    """
    if depth > 12:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {
            str(key): _finite_safe(item, depth + 1)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [_finite_safe(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, str, float)):
        return value
    return repr(value)


def _keep_damaged_entry(
    damaged: list[dict], salvaged: list[ParkRecord], item
) -> None:
    """Preserve one unusable document entry, and salvage what can be salvaged.

    The entry is kept (sanitised so it can be written back) instead of only being
    remembered in memory: an obligation nobody could parse is still the only proof
    that a foreign window may be off-screen, and a writer that passed it by used
    to erase it.  Duplicates are collapsed - a hand-edited document may list the
    same broken entry in ``records`` and in ``damaged`` - so the preserved list
    cannot grow without bound.
    """
    salvage = salvage_record(item)
    if not isinstance(item, dict):
        item = {"unparsed": _finite_safe(item)}
    entry = _finite_safe(item)
    key = json.dumps(entry, sort_keys=True, default=repr)
    if any(json.dumps(existing, sort_keys=True, default=repr) == key for existing in damaged):
        return
    damaged.append(entry)
    if salvage is not None:
        salvaged.append(salvage)


def journal_path_for(settings_path: Path) -> Path:
    return Path(str(settings_path) + ".park.json")


def _same_obligation(existing: ParkRecord, candidate: ParkRecord) -> bool:
    """Whether ``candidate`` is the very same park as ``existing``.

    This is what makes a repeated ``record_intent`` idempotent instead of
    destructive: only an unchanged original state and the same owning run count
    as "the same park, retried".  A changed geometry is a new park, and a new
    park may not silently adopt an open obligation.
    """
    return (
        int(existing.pid) == int(candidate.pid)
        and existing.class_name == candidate.class_name
        and existing.process_created == candidate.process_created
        and tuple(existing.screen_rect) == tuple(candidate.screen_rect)
        and tuple(existing.normal_position) == tuple(candidate.normal_position)
        and existing.owner_run_id == candidate.owner_run_id
    )


def journal_identity(path) -> str:
    """Stable identity of one journal *document*.

    The journal depends on ``--config``, on portable mode and on where the
    application is installed, so two sessions can own two completely different
    obligations.  Anything that has to name "the one guardian of this journal"
    therefore derives its name from this value instead of from a session-wide
    constant: a mutex that cannot tell two documents apart lets a guardian claim
    to have taken over a journal it is not executing.
    """
    try:
        normalised = os.path.normcase(os.path.abspath(str(Path(path))))
    except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
        normalised = str(path)
    return hashlib.sha256(normalised.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def record_from_state(
    hwnd: int,
    state,
    *,
    owner_pid: int | None = None,
    owner: ExecutorIdentity | None = None,
    label: str = "",
) -> ParkRecord:
    """Build the durable record for a park that is about to be registered."""
    placement = state.placement
    return ParkRecord(
        hwnd=int(hwnd),
        pid=int(state.pid),
        class_name=state.class_name,
        process_name=state.process_name,
        process_created=state.process_created,
        screen_rect=tuple(int(value) for value in state.screen_rect),
        show_cmd=int(placement.showCmd),
        placement_flags=int(placement.flags),
        min_position=(int(placement.ptMinPosition.x), int(placement.ptMinPosition.y)),
        max_position=(int(placement.ptMaxPosition.x), int(placement.ptMaxPosition.y)),
        normal_position=(
            int(placement.rcNormalPosition.left),
            int(placement.rcNormalPosition.top),
            int(placement.rcNormalPosition.right),
            int(placement.rcNormalPosition.bottom),
        ),
        owner_pid=int(owner.pid if owner is not None else (os.getpid() if owner_pid is None else owner_pid)),
        owner_run_id=owner.executor_id if owner is not None else new_run_id(),
        owner_created=owner.created if owner is not None else _self_creation_time(),
        recorded_at=time.time(),
        label=label,
        state=STATE_INTENT,
        # The operation id the park will stamp on the window itself, so recovery
        # can still recognise it after a monitor change.
        operation_id=str(getattr(state, "operation_id", "") or "") or new_run_id(),
        park_rect=tuple(int(value) for value in getattr(state, "park_rect", ()) or ()),
        park_origin=tuple(int(value) for value in getattr(state, "park_origin", ()) or ()),
    )


def _self_creation_time() -> int | None:
    try:
        import winapi

        return winapi.get_process_creation_time(os.getpid())
    except Exception:  # pragma: no cover - non-Windows, or a locked-down kernel
        return None


def state_from_record(record: ParkRecord, module=None):
    """Rebuild the exact pre-park placement that ``record`` describes.

    ``module`` is the Win32 layer to build the placement with and defaults to
    the real one; passing it in keeps callers that already have a Win32 layer
    (or a stand-in for it) from importing Windows-only types for no reason.
    """
    if module is None:
        import winapi

        module = winapi

    placement = module.WINDOWPLACEMENT()
    placement.length = ctypes_sizeof(module.WINDOWPLACEMENT)
    placement.flags = int(record.placement_flags)
    placement.showCmd = int(record.show_cmd)
    placement.ptMinPosition = module.wintypes.POINT(*record.min_position)
    placement.ptMaxPosition = module.wintypes.POINT(*record.max_position)
    placement.rcNormalPosition = module.wintypes.RECT(*record.normal_position)
    return module.ParkedWindowState(
        placement=placement,
        screen_rect=tuple(int(value) for value in record.screen_rect),
        pid=int(record.pid),
        class_name=record.class_name,
        process_name=record.process_name,
        process_created=record.process_created,
        park_rect=tuple(int(value) for value in record.park_rect),
        operation_id=record.operation_id or "",
    )


def ctypes_sizeof(struct_type) -> int:
    import ctypes

    return ctypes.sizeof(struct_type)


def journal_problem(data) -> str | None:
    """Return why ``data`` is not a readable journal document, or ``None``.

    Validation is intentionally strict.  A silently accepted half-document is
    worse than a rejected one: the journal is the only durable proof that a
    foreign window was moved off-screen, so an unreadable document must be
    quarantined instead of half-honoured.  A *supported older* version is not
    damage: it is migrated in place, transactionally.
    """
    if not isinstance(data, dict):
        return f"root is {type(data).__name__}, expected an object"
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return f"version is {version!r}, expected an integer"
    if version not in SUPPORTED_JOURNAL_VERSIONS:
        return f"unsupported journal version {version}"
    records = data.get("records")
    if not isinstance(records, list):
        return f"records is {type(records).__name__}, expected a list"
    if len(records) > MAX_RECORDS:
        return f"records holds {len(records)} entries, more than {MAX_RECORDS}"
    damaged = data.get("damaged", [])
    if not isinstance(damaged, list):
        return f"damaged is {type(damaged).__name__}, expected a list"
    if len(damaged) > MAX_RECORDS:
        return f"damaged holds {len(damaged)} entries, more than {MAX_RECORDS}"
    # One limit for both lists, exactly as ``_write`` counts them: a writer must
    # never be handed a document its own reader refuses.
    if len(records) + len(damaged) > MAX_RECORDS:
        return (
            f"records plus damaged hold {len(records) + len(damaged)} entries, "
            f"more than {MAX_RECORDS}"
        )
    return None


def _quarantine_names(path: Path, limit: int = 10) -> list[Path]:
    """The quarantined documents beside ``path`` that have not been swept yet."""
    found: list[Path] = []
    for index in range(limit):
        suffix = QUARANTINE_SUFFIX if index == 0 else f"{QUARANTINE_SUFFIX}.{index}"
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            found.append(candidate)
    return found


def _quarantine_artefacts(path: Path) -> int:
    """How many quarantined documents sit next to ``path``.

    Their presence is durable evidence that an earlier process could not read
    what it had written.  Only a process that has *swept* them may call the damage
    dealt with, and it says so by renaming them (see :meth:`RecoveryJournal.
    acknowledge_damage`), so a new process sees the damage again until somebody has
    actually recovered the stranded windows - which is exactly what keeps an
    obligation from quietly disappearing across a restart.
    """
    return len(_quarantine_names(path))


def quarantined_entries(path: Path) -> tuple[dict, ...]:
    """Every document entry preserved in the quarantined files beside ``path``.

    Quarantine keeps the document because it is the only record of what a previous
    process believed it owned.  Reading it back is what turns that file from a
    forensic trace into an executable obligation: the sweep can then restore the
    *specific* windows the lost document names - with their own park rectangles -
    instead of guessing from the geometry of the current monitor layout, which a
    monitor change has meanwhile invalidated.

    Only entries are taken, and only ones that still carry a usable handle, so this
    can never move a window the document does not name.
    """
    found: list[dict] = []
    seen: set[str] = set()
    for artefact in _quarantine_names(Path(path)):
        try:
            raw = artefact.read_bytes()
        except OSError as exc:
            logger.warning(
                "Quarantined recovery document %s could not be read back: %s",
                artefact.name,
                exc,
            )
            continue
        if len(raw) > MAX_JOURNAL_BYTES:
            logger.warning(
                "Quarantined recovery document %s is %s bytes and was not parsed",
                artefact.name,
                len(raw),
            )
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            logger.warning(
                "Quarantined recovery document %s is not valid JSON; only the "
                "conservative sweep can decide",
                artefact.name,
            )
            continue
        if not isinstance(data, dict):
            continue
        entries = [item for source in ("records", "damaged") for item in (
            data.get(source) if isinstance(data.get(source), list) else []
        )]
        for item in entries:
            if len(found) >= MAX_RECORDS:
                break
            if not isinstance(item, dict):
                continue
            key = json.dumps(item, sort_keys=True, default=repr)
            if key in seen:
                continue
            seen.add(key)
            found.append(item)
    return tuple(found)


class RecoveryJournal:
    """Transactional JSON journal of every window this machine has parked.

    Several writers are possible at the same time (the running application, a
    recovery guardian handed the leftovers at shutdown, and a second LookUp
    start), so no state is ever kept as a cached snapshot that is written back
    later: every mutation is one inter-process transaction that re-reads the
    newest document, applies exactly one change and commits it atomically.
    That is what keeps a newer obligation from being erased by an unrelated
    stale write, and what makes a fenced lease an exclusive, cross-process right.
    """

    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock_path = Path(str(self._path) + JOURNAL_LOCK_SUFFIX)
        self._records: dict[int, ParkRecord] = {}
        self._revision = 0
        # True while the newest document on disk used an older schema and has to
        # be rewritten.  The rewrite happens inside the same transaction, under
        # the same lock, as the read that discovered it.
        self._needs_migration = False
        # What the newest document actually said.  A quarantined document does
        # *not* turn into "empty": it stays unresolved until somebody has swept
        # the stranded windows, because "I cannot read the journal" is not the
        # same answer as "there is nothing to restore".
        self._status = JOURNAL_STATUS_EMPTY
        self._damage = ""
        self._unresolved: tuple[ParkRecord, ...] = ()
        # Entries of the document that the strict parser refused, kept *verbatim*.
        # They are written back unchanged by every writer: a salvaged obligation
        # that only exists in one process's memory is erased by the next claim of
        # an unrelated record, which is how a partly damaged document used to lose
        # a window while looking perfectly valid afterwards.
        self._damaged: list[dict] = []
        # True once this process has dealt with whatever damage it saw, so the
        # quarantined documents left behind for forensics do not keep reporting a
        # resolved problem.
        self._acknowledged = False
        # One lock per journal path, shared with every other instance in this
        # process: serialization is mandatory between parking workers, between
        # those and startup recovery, and between both and the guardian.
        self._lock = _process_lock_for(os.path.normcase(os.path.abspath(str(self._lock_path))))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def identity(self) -> str:
        """The stable id of this document, as the guardian handshake names it."""
        return journal_identity(self._path)

    @property
    def status(self) -> str:
        """``valid`` / ``empty`` / ``degraded`` / ``unresolved`` for the newest read."""
        try:
            with self._lock:
                return self._status
        except JournalReadError:
            return self._status

    @property
    def damaged(self) -> bool:
        """Whether the newest document was partly or wholly unreadable."""
        try:
            with self._lock:
                return self._status in DAMAGED_JOURNAL_STATUSES
        except JournalReadError:
            # Busy means "unknown", and unknown about damage means assume damage:
            # an executor must never be told "nothing outstanding" on a guess.
            return True

    @property
    def damage_reason(self) -> str:
        try:
            with self._lock:
                return self._damage
        except JournalReadError:
            return self._damage

    @property
    def unresolved(self) -> tuple[ParkRecord, ...]:
        """Salvaged records from a damaged document, kept for orphan recovery.

        They are deliberately *not* part of :meth:`records`: a salvaged entry has
        no trustworthy placement, so it may only be executed through the
        conservative orphan path, which is a decision for the caller, not for a
        read.  They *are* part of the document, though: :meth:`damaged_entries`
        holds them verbatim so no writer can drop them on the way past.
        """
        try:
            with self._lock:
                return self._unresolved
        except JournalReadError:
            return self._unresolved

    @property
    def damaged_entries(self) -> tuple[dict, ...]:
        """The document entries no reader could use, exactly as they were found."""
        try:
            with self._lock:
                return tuple(self._damaged)
        except JournalReadError:
            return tuple(self._damaged)

    def _damage_token(self) -> str:
        artifacts = []
        for path in _quarantine_names(self._path):
            try:
                info = path.stat()
            except OSError:
                # Another process swept or quarantined this artefact between the
                # listing and the stat.  Leaving it out makes the token describe a
                # document state that no longer exists, which is the safe answer:
                # the acknowledgement then simply does not commit and the damage
                # stays unresolved for the next attempt.
                continue
            artifacts.append((path.name, info.st_size, info.st_mtime_ns))
        payload = json.dumps([self._damaged, artifacts], sort_keys=True, default=repr)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def acknowledge_damage(self, *, expected_token: str | None = None) -> bool:
        """Record durably that the damage has been dealt with.

        Called only after a sweep that has brought every stranded window back or
        proved - completely - that none is left.  Until then ``status`` stays
        ``degraded``/``unresolved`` so no executor may conclude that this document
        holds no obligations.

        The acknowledgement is itself a journal transaction, because an in-memory
        one is erased by the next write or lost at exit: the salvaged entries are
        dropped from the document and the quarantined artefacts are renamed.  A
        transaction that cannot be committed returns ``False`` and leaves the
        damage exactly where it was, which is the only safe answer when the disk
        state is unknown.
        """
        if expected_token is None:
            expected_token = self._damage_token()

        def change(records: dict[int, ParkRecord]):
            if self._damage_token() != expected_token:
                return _NO_WRITE
            self._damaged = []
            self._unresolved = ()
            self._damage = ""
            self._acknowledged = True
            self._status = JOURNAL_STATUS_VALID if records else JOURNAL_STATUS_EMPTY
            return True

        committed, result = self._transact(change, after_commit=self._sweep_quarantine_artefacts)
        if not committed or result is _NO_WRITE:
            logger.error(
                "Recovery journal %s could not record the acknowledgement of its "
                "damage; the obligations stay unresolved",
                self._path,
            )
            return False
        return True

    def _sweep_quarantine_artefacts(self) -> int:
        """Rename quarantined documents to a name that no longer reports damage.

        The trace is kept (nothing is deleted), but every later reader stops
        treating it as unfinished work, so a swept document does not keep an
        executor alive for the rest of the machine's life.
        """
        swept = 0
        for artefact in _quarantine_names(self._path):
            target = Path(str(artefact) + QUARANTINE_SWEEPT_SUFFIX)
            for index in range(1, 10):
                if not target.exists():
                    break
                target = Path(f"{artefact}{QUARANTINE_SWEEPT_SUFFIX}.{index}")
            try:
                os.replace(artefact, target)
            except OSError as exc:
                logger.warning(
                    "Quarantined recovery document %s could not be marked as swept: %s",
                    artefact.name,
                    exc,
                )
                continue
            swept += 1
        return swept

    @property
    def revision(self) -> int:
        """The document generation this instance last committed or read."""
        try:
            with self._lock:
                return int(self._revision)
        except JournalReadError:
            return int(self._revision)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def records(self) -> tuple[ParkRecord, ...]:
        return self.reload()

    def has_pending(self) -> bool:
        return bool(self.reload())

    def get(self, hwnd: int) -> ParkRecord | None:
        if not self._read_snapshot():
            raise JournalReadError(f"Recovery journal could not be read: {self._path}")
        try:
            with self._lock:
                return self._records.get(int(hwnd))
        except JournalReadError as exc:
            raise JournalReadError(str(exc)) from None

    def reload(self) -> tuple[ParkRecord, ...]:
        """Re-read the newest document and return its records.

        Used by a second writer so it merges with, instead of clobbering, the
        records another process has written in the meantime.  It is also the
        default behaviour of every mutation, which is what makes the reload
        here a convenience rather than a correctness requirement.
        """
        if not self._read_snapshot():
            raise JournalReadError(f"Recovery journal could not be read: {self._path}")
        return self._snapshot()

    def snapshot(self) -> RecoverySnapshot:
        """Re-read the newest document and answer *all* of its questions at once.

        Records, salvaged entries and damage belong together: an executor that
        reads only the record list cannot tell "no window is parked" from "the
        document could not be read, so nobody knows what was parked".  This is
        the single answer every exit, handover and stop decision is taken on.
        """
        if not self._read_snapshot():
            raise JournalReadError(f"Recovery journal could not be read: {self._path}")
        try:
            with self._lock:
                return RecoverySnapshot(
                    records=tuple(self._records.values()),
                    unresolved=tuple(self._unresolved),
                    status=self._status,
                    damage=self._damage,
                    damage_token=self._damage_token(),
                )
        except JournalReadError as exc:
            raise JournalReadError(str(exc)) from None

    def _read_snapshot(self) -> bool:
        """Refresh the cache from disk under the inter-process lock."""
        try:
            self._lock.__enter__()
        except JournalReadError as exc:
            # A busy in-process lock is the same kind of answer as a busy
            # inter-process lock: the newest state is unknown.
            logger.warning("Recovery journal %s is busy in this process: %s", self._path, exc)
            return False
        try:
            lock = _InterProcessJournalLock(self._lock_path)
            if not lock.acquire():
                # Never invent a "no obligations" answer out of a lock failure:
                # the last snapshot is the only honest thing left to report.
                logger.warning(
                    "Recovery journal %s could not be read; last known obligations preserved, "
                    "disk state unknown",
                    self._path,
                )
                return False
            try:
                if not self._read_document():
                    return False
                if self._needs_migration:
                    # Migration is part of the read that discovered it: lock ->
                    # read newest -> migrate -> atomic write -> unlock.  A separate
                    # convert-then-write would let another process write in
                    # between and lose its obligation.
                    self._needs_migration = False
                    self._revision += 1
                    if not self._write():
                        logger.error("Recovery journal %s could not be migrated", self._path)
                return True
            finally:
                lock.release()
        finally:
            self._lock.__exit__(None, None, None)

    def _snapshot(self) -> tuple[ParkRecord, ...]:
        try:
            with self._lock:
                return tuple(self._records.values())
        except JournalReadError:
            # The last known snapshot is the only honest answer available.
            return tuple(self._records.values())

    # ------------------------------------------------------------------ #
    # Mutations: each one is a single inter-process transaction
    # ------------------------------------------------------------------ #
    def record_intent(self, record: ParkRecord) -> bool:
        """Register ownership *before* any window is moved off-screen.

        Creation is transactional, which means it is a compare-and-set against
        the newest document rather than an assignment:

        * no record for this window - the intent is created;
        * a record for the *same operation* describing the same original state -
          an idempotent repeat, so the caller may proceed;
        * anything else - refused.  Overwriting an unfinished obligation would
          silently replace the geometry, the owner and the deadline another
          executor is already working with, and would leave that executor holding
          a fencing token over a record that is no longer the one it wrote.
        """
        operation = _optional_text(record.operation_id, MAX_OPERATION_ID_CHARS) or new_run_id()
        record.operation_id = operation
        refused = {"value": False}

        def change(records: dict[int, ParkRecord]):
            existing = records.get(int(record.hwnd))
            if existing is None:
                if any(item.hwnd == record.hwnd for item in self._unresolved):
                    refused["value"] = True
                    logger.error("Recovery intent for hwnd=%s refused: a damaged obligation is still open", record.hwnd)
                    return _NO_WRITE
                records[int(record.hwnd)] = record
                return True
            if existing.operation_id == operation and _same_obligation(existing, record):
                # Already durable and unchanged: nothing to write, and the
                # caller is entitled to move the window.
                return _NO_WRITE
            refused["value"] = True
            logger.error(
                "Recovery intent for hwnd=%s refused: an unfinished obligation "
                "(operation=%s owner=%s state=%s) is still open; finish it first",
                record.hwnd,
                existing.operation_id or "-",
                existing.owner_identity,
                existing.state,
            )
            return _NO_WRITE

        committed, _result = self._transact(change)
        return bool(committed) and not refused["value"]

    def mark_parked(self, hwnd: int, executor=None) -> bool:
        """Commit the park, but only for the run that registered it.

        A process that merely observes the record may not turn another run's
        intent into a committed park: that would hand a window it never moved to
        a state whose meaning is "this process parked it and will restore it".
        """

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(hwnd))
            if entry is None:
                return _NO_WRITE
            if entry.state == STATE_PARKED:
                # Already committed by this or another writer: nothing to write,
                # but the caller's intent is satisfied.
                return True
            identity = _as_executor(executor if executor is not None else entry.owner_run_id or 0)
            if not entry.may_be_retired_by(identity):
                logger.warning(
                    "Park commit for hwnd=%s refused: it belongs to another run (%s)",
                    hwnd,
                    entry.owner_identity,
                )
                return _NO_WRITE
            entry.state = STATE_PARKED
            return True

        committed, result = self._transact(change)
        # The caller is told whether the park is now committed, not merely whether
        # the transaction ran: a refused commit must not look like a parked window.
        return bool(committed) and result is not _NO_WRITE

    def clear(self, hwnd: int) -> bool:
        def change(records: dict[int, ParkRecord]):
            if records.pop(int(hwnd), None) is None:
                return _NO_WRITE
            return True

        return self._commit(change)

    def clear_if(self, hwnd: int, recorded_at: float) -> bool:
        """Drop the record only while it is still the one the caller worked on."""

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(hwnd))
            if entry is None:
                return _NO_WRITE
            if abs(float(entry.recorded_at) - float(recorded_at)) > 1e-6:
                return _NO_WRITE
            records.pop(int(hwnd), None)
            return True

        return self._commit(change)

    def clear_unless_claimed(self, hwnd: int, executor, *, recorded_at: float | None = None) -> bool:
        """Drop the record while this executor is entitled to retire it.

        Ownership of an obligation belongs to the run that registered it, or to
        the executor that explicitly claimed it.  A foreign process that merely
        sees the window on screen must claim first, which serialises the decision
        against every other executor and closes the "delayed park lands after
        somebody cleared the record" race.
        """

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(hwnd))
            if entry is None:
                return _NO_WRITE
            identity = _as_executor(executor)
            if entry.lease_is_live(identity):
                # Somebody else is actively working on it: that executor, and
                # only that executor, ends the obligation.
                return _NO_WRITE
            if not entry.may_be_retired_by(identity):
                return _NO_WRITE
            if recorded_at is not None and abs(float(entry.recorded_at) - float(recorded_at)) > 1e-6:
                return _NO_WRITE
            records.pop(int(hwnd), None)
            return True

        return self._commit(change)

    # ------------------------------------------------------------------ #
    # Fenced claims
    # ------------------------------------------------------------------ #
    def claim(
        self,
        hwnd: int,
        executor,
        lease_sec: float = CLAIM_LEASE_SEC,
    ) -> Claim | None:
        """Take a renewable, fenced lease on one record and return it, or None.

        The compare-and-set is decided on the newest document while the
        inter-process lock is held, so two executors in two processes can never
        both believe they own the same obligation.  Every successful claim takes a
        new fencing generation and a new token: a lease that expired and was
        re-taken is therefore distinguishable from the one its previous holder
        still remembers.
        """
        identity = _as_executor(executor)
        lease = _bounded_float(lease_sec, 1.0, 24 * 3600.0, CLAIM_LEASE_SEC)
        held: dict[str, object] = {}

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(hwnd))
            if entry is None:
                return _NO_WRITE
            if entry.lease_is_live(identity):
                return _NO_WRITE
            entry.claim_pid = identity.pid
            entry.claim_until = time.time() + lease
            entry.claim_executor = identity.executor_id
            entry.claim_created = identity.created
            entry.claim_token = new_run_id()
            entry.claim_generation = int(entry.claim_generation) + 1
            if entry.state == STATE_STALE:
                # A stale record means the move happened but its confirmation was
                # lost.  "parked" is derived from the state, so it is False here and
                # the record would silently degrade to an intent that nothing
                # restores.  Promoting it is the direction that keeps the
                # obligation executable: restoring an already-visible window is a
                # no-op, losing the obligation is not.
                entry.state = STATE_PARKED
            held["claim"] = Claim(
                hwnd=int(hwnd),
                executor=identity,
                generation=int(entry.claim_generation),
                token=entry.claim_token,
                recorded_at=float(entry.recorded_at),
            )
            return True

        committed, result = self._transact(change)
        if not committed or result is _NO_WRITE:
            return None
        return held.get("claim")

    def renew(self, claim: Claim, lease_sec: float = CLAIM_LEASE_SEC) -> bool:
        """Extend a lease, but only for the exact generation it was granted for."""
        lease = _bounded_float(lease_sec, 1.0, 24 * 3600.0, CLAIM_LEASE_SEC)

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(claim.hwnd))
            if entry is None or not entry.claim_matches(claim):
                return _NO_WRITE
            entry.claim_until = time.time() + lease
            entry.claim_pid = claim.executor.pid
            return True

        return self._commit(change)

    def release(self, claim: Claim) -> bool:
        """Give a lease up early so another executor can continue at once."""

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(claim.hwnd))
            if entry is None or not entry.claim_matches(claim):
                return _NO_WRITE
            entry.clear_claim()
            return True

        return self._commit(change)

    def release_all(self, executor) -> None:
        """Free every lease ``executor`` holds (used when an executor exits)."""
        identity = _as_executor(executor)

        def change(records: dict[int, ParkRecord]):
            ours = [
                entry
                for entry in records.values()
                if entry.claim_executor == identity.executor_id
            ]
            if not ours:
                return _NO_WRITE
            for entry in ours:
                entry.clear_claim()
            return True

        self._transact(change)

    def clear_claimed(self, claim: Claim, recorded_at: float | None = None) -> bool:
        """Remove a record while proving that this executor still owns it.

        This is the only way a restore may end an obligation: the removal has to
        prove generation *and* token, so an executor whose lease expired while it
        was blocked cannot clear the newer owner's record.
        """

        def change(records: dict[int, ParkRecord]):
            entry = records.get(int(claim.hwnd))
            if entry is None or not entry.claim_matches(claim):
                return _NO_WRITE
            if recorded_at is not None and abs(float(entry.recorded_at) - float(recorded_at)) > 1e-6:
                return _NO_WRITE
            records.pop(int(claim.hwnd), None)
            return True

        return self._commit(change)

    # ------------------------------------------------------------------ #
    # Transaction plumbing
    # ------------------------------------------------------------------ #
    def _commit(self, change) -> bool:
        """Run one mutation and report whether the new state was committed."""
        committed, result = self._transact(change)
        if not committed:
            return False
        return result is not _NO_WRITE

    def _transact(self, change, *, after_commit=None) -> tuple[bool, object]:
        """Read the newest document, apply exactly one change, commit it.

        The whole cycle runs under the in-process lock *and* the inter-process
        lock, so no second writer can slip a newer obligation in between the read
        and the write.  ``change`` receives the freshly read mapping and returns
        the value the caller should see, or ``_NO_WRITE`` when the document must
        be left untouched.
        """
        try:
            self._lock.__enter__()
        except JournalReadError as exc:
            # Fail closed: an unsynchronised write would be exactly the lost
            # update this journal must never perform.
            logger.error(
                "Recovery journal %s is busy in this process (%s); the change was not "
                "written",
                self._path,
                exc,
            )
            return False, None
        try:
            lock = _InterProcessJournalLock(self._lock_path)
            if not lock.acquire():
                # Fail closed: an unsynchronised write would be exactly the
                # lost update this journal must never perform.
                logger.error(
                    "Recovery journal %s could not be locked; the change was not written",
                    self._path,
                )
                return False, None
            try:
                if not self._read_document():
                    # An I/O failure says nothing about the newest obligations.
                    # Never merge against a cached or invented empty document.
                    return False, None
                migrating = self._needs_migration
                self._needs_migration = False
                result = change(self._records)
                if result is _NO_WRITE and not migrating:
                    return True, _NO_WRITE
                self._revision += 1
                if not self._write():
                    return False, None
                if after_commit is not None:
                    after_commit()
                return True, result
            finally:
                lock.release()
        finally:
            self._lock.__exit__(None, None, None)

    # ------------------------------------------------------------------ #
    # Document I/O (only ever called with the journal lock held)
    # ------------------------------------------------------------------ #
    def _read_document(self) -> bool:
        """Load the newest document into ``self._records``.

        Records are never dropped because of their age: an obligation only ends
        when a verified restore (or a dead/reused window) says so.  Damage is
        reported through :attr:`status` instead of being flattened into "empty",
        because an unreadable document is not proof that nothing is parked.
        """
        self._needs_migration = False
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            self._records = {}
            self._revision = 0
            self._mark_valid()
            return True
        except OSError as exc:
            logger.warning("Recovery journal could not be read: %s", exc)
            return False
        if len(raw) > MAX_JOURNAL_BYTES:
            return self._quarantine(f"document is {len(raw)} bytes, more than {MAX_JOURNAL_BYTES}")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            return self._quarantine(f"document is not valid JSON ({exc.__class__.__name__})")
        problem = journal_problem(data)
        if problem is not None:
            return self._quarantine(problem)
        version = int(data["version"])
        self._revision = 0
        revision = data.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            self._revision = int(revision)
        records: dict[int, ParkRecord] = {}
        salvaged: list[ParkRecord] = []
        damaged: list[dict] = []
        broken = 0
        for item in data["records"]:
            record = _parse_record(item, version=version)
            if record is None:
                broken += 1
                _keep_damaged_entry(damaged, salvaged, item)
                continue
            records[record.hwnd] = record
        # Entries an earlier writer could not use are still obligations.  They are
        # re-read here and kept verbatim, so the damage survives *this* process
        # instead of living only in its memory.
        carried = data.get("damaged")
        if isinstance(carried, list):
            for item in carried:
                if not isinstance(item, dict):
                    # Not even the handle survived: nothing can be executed, but the
                    # fact that the document was damaged has to stay durable.
                    broken += 1
                    _keep_damaged_entry(damaged, salvaged, item)
                    continue
                broken += 1
                _keep_damaged_entry(damaged, salvaged, item)
        if damaged or broken:
            logger.warning(
                "Recovery journal %s contained %s unusable entr(ies); %s readable "
                "record(s) kept, %s preserved for orphan recovery",
                self._path.name,
                broken,
                len(records),
                len(damaged),
            )
        # Records are never dropped because of their age: an obligation only ends
        # when a verified restore (or a dead/reused window) says so.
        self._records = records
        self._damaged = damaged
        if damaged or broken:
            # The document stays active for the records that *are* readable, but
            # the ones that are not must not silently disappear from the recovery
            # picture either, so the status says "degraded" and the salvaged
            # entries stay available to a conservative sweep.
            self._damage = f"{broken} unusable record(s), {len(damaged)} preserved"
            self._unresolved = tuple(salvaged)
            self._status = JOURNAL_STATUS_DEGRADED
        else:
            self._mark_valid()
        if version != JOURNAL_VERSION:
            # The record contents are already migrated in memory; the caller
            # commits them while still holding the lock.
            self._needs_migration = True
            logger.info(
                "Migrating recovery journal %s from schema %s to %s in place",
                self._path,
                version,
                JOURNAL_VERSION,
            )
        return True

    def _mark_valid(self) -> None:
        """The newest document was read completely; report exactly that."""
        stranded = _quarantine_artefacts(self._path)
        if stranded:
            self._status = JOURNAL_STATUS_DEGRADED
            self._damage = f"{stranded} quarantined recovery document(s) beside it"
            self._unresolved = ()
            self._damaged = []
            return
        self._status = JOURNAL_STATUS_VALID if self._records else JOURNAL_STATUS_EMPTY
        self._damage = ""
        self._unresolved = ()
        self._damaged = []

    def _quarantine(self, reason: str) -> bool:
        """Move an unreadable document aside instead of blocking every start.

        The document is renamed, never deleted: it is the only forensic trace of
        what a previous process believed it owned. If the rename fails, the
        state stays unknown and no mutation may overwrite the damaged file.

        Quarantining is *not* discharging.  The status becomes ``unresolved``
        rather than ``empty``, because after this point nobody can say how many
        windows the lost document described; only a sweep that brings the
        stranded windows back (or proves there are none) may clear it.
        """
        target = None
        for index in range(10):
            suffix = QUARANTINE_SUFFIX if index == 0 else f"{QUARANTINE_SUFFIX}.{index}"
            candidate = Path(str(self._path) + suffix)
            if not candidate.exists():
                target = candidate
                break
        if target is None:
            logger.error("Recovery journal %s is unreadable (%s)", self._path, reason)
            self._status = JOURNAL_STATUS_UNRESOLVED
            self._damage = reason
            return False
        try:
            os.replace(self._path, target)
        except OSError as exc:
            logger.error("Recovery journal %s is unreadable (%s) and could not be quarantined: %s",
                         self._path, reason, exc)
            self._status = JOURNAL_STATUS_UNRESOLVED
            self._damage = reason
            return False
        self._records = {}
        self._revision = 0
        self._status = JOURNAL_STATUS_UNRESOLVED
        self._damage = reason
        self._unresolved = ()
        # The whole document, damage and all, now lives in the quarantine file: the
        # preservation job is done by keeping that file, not by rewriting the
        # active one.
        self._damaged = []
        logger.error(
            "Recovery journal %s was unreadable (%s) and has been moved to %s; its "
            "obligations are unresolved, not absent",
            self._path,
            reason,
            target.name,
        )
        return True

    def _remove(self) -> bool:
        """Delete an emptied journal, tolerating a transient file lock.

        A sharing violation here is common and short-lived (an indexer, a virus
        scanner or the other reader of this very file).  Retrying matters: giving
        up would leave a record that advertises a window as parked long after it
        is back, and nothing else would ever remove it.
        """
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                self._path.unlink()
                return True
            except FileNotFoundError:
                return True
            except OSError as exc:
                last_error = exc
                time.sleep(0.04 * (attempt + 1))
        logger.warning(
            "Recovery journal could not be removed after %s attempt(s): %s",
            5,
            last_error,
        )
        return False

    def _write(self) -> bool:
        """Commit the in-memory state of the current transaction to disk.

        Everything the *reader* enforces is enforced here too, before the active
        document is replaced.  A writer that can produce a document its own reader
        quarantines turns "park registered" into "obligation lost", because the
        next read answers "unreadable" and the executor has nothing left to
        execute.  A refusal here keeps the previous document intact, so the park
        that asked for this record is simply not started.
        """
        if not self._records and not self._damaged:
            return self._remove()
        if len(self._records) + len(self._damaged) > MAX_RECORDS:
            logger.error(
                "Recovery journal %s cannot hold %s record(s) plus %s preserved "
                "unusable entr(ies) (limit %s); the change was not written",
                self._path,
                len(self._records),
                len(self._damaged),
                MAX_RECORDS,
            )
            return False
        entries: list[dict] = []
        for record in self._records.values():
            payload = record.to_dict()
            # Round-trip the exact schema that is about to hit the disk: a record
            # that cannot be read back must not be written at all.
            if _parse_record(payload, JOURNAL_VERSION) is None:
                logger.error(
                    "Recovery journal %s refused a record that does not survive its "
                    "own schema (hwnd=%s); the change was not written",
                    self._path,
                    record.hwnd,
                )
                return False
            entries.append(payload)
        payload = {
            "version": JOURNAL_VERSION,
            # Monotonically increasing generation.  It is diagnostics for a
            # stale writer, not the serialization itself: two processes can
            # still pick the same number, and the lock is what orders the
            # documents.
            "revision": int(self._revision),
            "updatedAt": time.time(),
            "records": entries,
            # The entries no reader could use, exactly as they were found.  They
            # are written by every writer, including writers that only ever touch
            # an unrelated record: dropping them here is what turned "partly
            # damaged" into "obligation gone" as soon as something else was
            # claimed.
            "damaged": self._damaged,
            "damage": self._damage if self._damaged else "",
        }
        try:
            data = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.warning("Recovery journal could not be serialized: %s", exc)
            return False
        if len(data) > MAX_JOURNAL_BYTES:
            logger.error(
                "Recovery journal %s would grow to %s bytes (limit %s); the change "
                "was not written",
                self._path,
                len(data),
                MAX_JOURNAL_BYTES,
            )
            return False
        tmp = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=self._path.name + ".", suffix=".tmp"
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
            tmp = None
            return True
        except OSError as exc:
            logger.warning("Recovery journal write failed: %s", exc)
            return False
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
