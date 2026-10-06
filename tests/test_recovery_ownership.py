"""Recovery ownership invariants: the regressions this architecture was rebuilt for.

Every test here maps to a failure mode that used to lose a user's window:

* a guardian that gave up after a hard lifetime (R12),
* a PID-reuse block that froze recovery forever (R13),
* a stale executor that could clear a newer claim (ABA),
* non-finite control values that locked a record permanently (R14),
* and an accessibility predicate that called a window visible while it sat in the
  gap between two monitors.

The R-series continues test_production_safety_gates.py (R01-R11).
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import restoreguard  # noqa: E402  (after source-path bootstrap)
import recovery  # noqa: E402
import screen  # noqa: E402
from recovery import (  # noqa: E402
    STATE_PARKED,
    ExecutorIdentity,
    ParkRecord,
    RecoveryJournal,
)

try:
    import winapi  # noqa: E402
except Exception:  # pragma: no cover - non-Windows
    winapi = None


def make_record(hwnd: int = 4242, **overrides) -> ParkRecord:
    values = {
        "hwnd": hwnd,
        "pid": 1234,
        "class_name": "SomeWindowClass",
        "process_name": "app.exe",
        "process_created": 999,
        "screen_rect": (10, 20, 810, 620),
        "show_cmd": 1,
        "placement_flags": 0,
        "min_position": (-1, -1),
        "max_position": (-1, -1),
        "normal_position": (10, 20, 810, 620),
        "owner_pid": 77,
        "owner_run_id": "owner-run",
        "owner_created": 555,
        "recorded_at": time.time(),
        "label": "app.exe - Some",
        "state": STATE_PARKED,
    }
    values.update(overrides)
    return ParkRecord(**values)


def executor(tag: str = "a") -> ExecutorIdentity:
    return ExecutorIdentity(executor_id=f"exec-{tag}", pid=1000 + ord(tag), created=42, label=tag)


class StuckWinapi:
    """A parked window that refuses to come back, plus a switch to let it."""

    def __init__(self, *, parked=True, onscreen=False, restorable=False):
        self.parked = parked
        self.onscreen = onscreen
        self.restorable = restorable
        self.restore_calls = 0
        self.orphan_calls = 0

    # identity ---------------------------------------------------------
    def is_window(self, hwnd):
        return True

    def get_pid(self, hwnd):
        return 1234

    def get_class_name(self, hwnd):
        return "SomeWindowClass"

    class _Identity:
        created = 999
        name = "app.exe"
        access_denied = False

    def _query_process_identity(self, pid, query_name=True):
        return self._Identity()

    def get_process_creation_time(self, pid):
        return 999

    # placement --------------------------------------------------------
    def looks_like_lookup_parked(self, hwnd):
        return self.parked

    def is_effectively_onscreen(self, hwnd, min_visible=24):
        return self.onscreen

    def window_matches_parked_state(self, hwnd, state):
        return True

    def state_from_record(self, record):
        return ("fake-parked-state", int(record.hwnd))

    def restore_parked_window_sync(self, hwnd, state):
        self.restore_calls += 1
        if self.restorable:
            self.parked = False
            self.onscreen = True
        return self.restorable

    def recover_orphaned_lookup_park(self, hwnd):
        self.orphan_calls += 1
        return self.restore_parked_window_sync(hwnd, None)


def finished_pid() -> int:
    """A PID the kernel is guaranteed to have released."""
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    child.wait(timeout=30)
    return int(child.pid)


class GuardianLifetimeTests(unittest.TestCase):
    """R12: an executor must never stop executing a live obligation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.fake = StuckWinapi(restorable=False)
        self.identity = executor("g")

    def tearDown(self):
        self._tmp.cleanup()

    def _run_guardian(self, record: ParkRecord, *, seconds: float = 3.0):
        self.journal.record_intent(record)
        result: list[int] = []

        def target():
            with patch.object(restoreguard, "winapi", lambda: self.fake), patch.object(
                restoreguard, "BACKOFF_SCHEDULE_SEC", (0.05, 0.1, 0.2)
            ), patch.object(restoreguard, "RENEW_SEC", 0.2), patch.object(
                restoreguard, "POLL_SEC", 0.05
            ):
                result.append(restoreguard.run_guardian(self.path, announce=False, executor=self.identity))

        thread = threading.Thread(target=target, name="guardian-under-test", daemon=True)
        thread.start()
        return thread, result

    def test_a_guardian_keeps_owning_a_live_obligation_past_any_lifetime(self):
        # The old executor stopped after a hard deadline and logged an error.  With
        # the retry interval shrunk, many attempts fit in a few seconds, so this
        # would have abandoned the record long ago.
        record = make_record(owner_pid=finished_pid(), owner_created=1)
        thread, result = self._run_guardian(record, seconds=3.0)
        time.sleep(2.0)
        self.assertEqual(result, [], "the guardian exited while a live obligation was outstanding")
        stored = self.journal.get(record.hwnd)
        self.assertIsNotNone(stored, "the guardian dropped a live obligation")
        self.assertEqual(stored.claim_executor, self.identity.executor_id)
        self.assertGreater(stored.claim_generation, 0)
        self.assertGreater(self.fake.restore_calls, 1, "the guardian stopped retrying")

        # The window comes back: now, and only now, may the executor leave.
        self.fake.restorable = True
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and (thread.is_alive() or result == []):
            time.sleep(0.1)
        thread.join(timeout=10)
        self.assertEqual(result, [0])
        self.assertFalse(self.path.exists(), "the discharged obligation stayed in the journal")

    def test_a_guardian_that_cannot_hand_over_keeps_executing(self):
        # The only exit that is still allowed with a live obligation is a
        # *confirmed* handover.  When no successor can exist, the guardian keeps
        # working and only gives its lease back, so another executor - or the next
        # start - can pick the obligation up.
        record = make_record(owner_pid=finished_pid(), owner_created=1)
        self.journal.record_intent(record)
        stop = threading.Event()
        with patch.object(restoreguard, "winapi", lambda: self.fake), patch.object(
            restoreguard, "BACKOFF_SCHEDULE_SEC", (0.05, 0.1, 0.2)
        ), patch.object(restoreguard, "RENEW_SEC", 0.2), patch.object(
            restoreguard, "POLL_SEC", 0.05
        ), patch.object(restoreguard, "spawn_guardian", return_value=None):
            worker = threading.Thread(
                target=restoreguard.run_guardian,
                args=(self.path,),
                kwargs={"announce": False, "executor": self.identity, "stop": stop},
                daemon=True,
            )
            worker.start()
            time.sleep(1.0)
            stop.set()
            time.sleep(1.0)
            self.assertTrue(worker.is_alive(), "the guardian abandoned a live obligation")
            stored = self.journal.get(record.hwnd)
            self.assertIsNotNone(stored, "stopping the executor discarded the obligation")
            self.assertEqual(
                stored.claim_executor,
                self.identity.executor_id,
                "the executor released an obligation it is still executing",
            )

            # The window comes back; only now does the executor leave.
            self.fake.restorable = True
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline and worker.is_alive():
                time.sleep(0.1)
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.path.exists())

    def test_a_confirmed_handover_is_the_only_way_out_with_a_live_obligation(self):
        # Exit path B: this guardian gives up, but only after a successor has
        # confirmed that it owns the executor mutex.  The record itself stays - the
        # obligation is the successor's now, and it is exactly what proves the
        # handover was worth anything.
        record = make_record(owner_pid=finished_pid(), owner_created=1)
        self.journal.record_intent(record)
        stop = threading.Event()
        successor = SimpleNamespace(pid=4242, poll=lambda: None, stdout=None)
        with patch.object(restoreguard, "winapi", lambda: self.fake), patch.object(
            restoreguard, "BACKOFF_SCHEDULE_SEC", (0.05, 0.1, 0.2)
        ), patch.object(restoreguard, "RENEW_SEC", 0.2), patch.object(
            restoreguard, "POLL_SEC", 0.05
        ), patch.object(restoreguard, "spawn_guardian", return_value=successor) as spawn, patch.object(
            restoreguard, "_confirm_handover", return_value=True
        ):
            worker = threading.Thread(
                target=restoreguard.run_guardian,
                args=(self.path,),
                kwargs={"announce": False, "executor": self.identity, "stop": stop},
                daemon=True,
            )
            worker.start()
            time.sleep(1.0)
            stop.set()
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "the guardian did not hand its obligation over")
        spawn.assert_called_once()
        self.assertEqual(
            spawn.call_args.kwargs["handover_from"],
            self.identity.executor_id,
            "the successor was not told which executor it takes over from",
        )
        stored = self.journal.get(record.hwnd)
        self.assertIsNotNone(stored, "the obligation was discarded instead of handed over")
        self.assertEqual(stored.claim_executor, "", "the lease was not released to the successor")


class ClaimFencingTests(unittest.TestCase):
    """ABA: a stalled executor may not mutate the claim that replaced its own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.fake = StuckWinapi(restorable=False)

    def test_an_executor_whose_claim_was_taken_over_stops_and_keeps_the_record(self):
        record = make_record(owner_pid=finished_pid(), owner_created=1)
        self.journal.record_intent(record)
        outcomes: list = []
        stop = threading.Event()

        def target():
            with patch.object(restoreguard, "winapi", lambda: self.fake), patch.object(
                restoreguard, "BACKOFF_SCHEDULE_SEC", (0.05, 0.1, 0.2)
            ), patch.object(restoreguard, "RENEW_SEC", 0.2):
                outcomes.append(
                    restoreguard.resolve(self.journal, record, executor("a"), context="test", stop=stop)
                )

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not self._claimed_by_a():
            time.sleep(0.05)
        self.assertTrue(self._claimed_by_a(), "the executor never claimed the record")

        # The lease expires and a second executor takes the record over.
        self._expire_lease(record.hwnd)
        successor = self.journal.claim(record.hwnd, executor("b"))
        self.assertIsNotNone(successor)
        stop.set()
        worker.join(timeout=30)
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0].resolved)
        self.assertIn(outcomes[0].reason, (restoreguard.STALE_CLAIM, "restore not verified yet"))
        stored = self.journal.get(record.hwnd)
        self.assertIsNotNone(stored, "the obligation was lost during the handover")
        self.assertEqual(stored.claim_executor, "exec-b")

    def _claimed_by_a(self) -> bool:
        try:
            stored = self.journal.get(4242)
        except OSError:
            return False
        return bool(stored and stored.claim_executor == "exec-a")

    def _expire_lease(self, hwnd: int) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        for item in document["records"]:
            if int(item["hwnd"]) == int(hwnd):
                item["claimUntil"] = time.time() - 60.0
        self.path.write_text(json.dumps(document), encoding="utf-8")


class OwnerPidReuseTests(unittest.TestCase):
    """R13: the owner of an obligation is an identity, not a number."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.fake = StuckWinapi(parked=True, onscreen=False, restorable=True)

    def test_a_reused_pid_does_not_block_recovery(self):
        # This process really is alive under that PID; the recorded owner is not.
        record = make_record(owner_pid=os.getpid(), owner_created=1)
        self.assertTrue(restoreguard.process_is_alive(record.owner_pid))
        self.journal.record_intent(record)
        self.assertFalse(
            restoreguard.owner_is_alive(record),
            "a live process with a different creation time was accepted as the owner",
        )
        with patch.object(restoreguard, "winapi", lambda: self.fake):
            outcome = restoreguard.resolve(
                self.journal, record, executor("g"), context="test"
            )
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "restored")
        self.assertFalse(self.path.exists())

    def test_the_matching_owner_keeps_the_obligation_with_itself(self):
        record = make_record(
            owner_pid=os.getpid(),
            owner_created=winapi.get_process_creation_time(os.getpid()) if winapi else None,
        )
        if record.owner_created is None:
            self.skipTest("this platform does not report a process creation time")
        self.assertTrue(restoreguard.owner_is_alive(record))


class LeasePoisoningTests(unittest.TestCase):
    """R14: a non-finite control value may not lock a record forever."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.fake = StuckWinapi(parked=True, onscreen=False, restorable=True)

    def _plant(self, **fields) -> None:
        document = {
            "version": 2,
            "records": [dict(make_record(owner_pid=finished_pid(), owner_created=1).to_dict(), **fields)],
        }
        self.path.write_text(json.dumps(document), encoding="utf-8")

    def test_a_poisoned_lease_still_lets_the_executor_work(self):
        for literal in (math.inf, -math.inf, math.nan):
            with self.subTest(literal=literal):
                self.path.unlink(missing_ok=True)
                self._plant(claimPid=999999, claimUntil=literal)
                with patch.object(restoreguard, "winapi", lambda: self.fake):
                    outcome = restoreguard.resolve(
                        RecoveryJournal(self.path), make_record(), executor("g"), context="test"
                    )
                self.assertTrue(outcome.resolved, f"claimUntil={literal} blocked recovery")
                self.assertFalse(self.path.exists())

    def test_a_poisoned_record_is_not_treated_as_already_claimed(self):
        self._plant(claimPid=999999, claimUntil=math.inf)
        identity = executor("g")
        stored = RecoveryJournal(self.path).get(4242)
        self.assertFalse(stored.lease_is_live(identity))
        self.assertIsNotNone(RecoveryJournal(self.path).claim(4242, identity))


class ProcessCreationTimePrecisionTests(unittest.TestCase):
    """Win32 creation times are 64-bit; a float round trip would corrupt them.

    This is not hypothetical: parsing a creation time through a float changed the
    last digits, so ``assess()`` concluded that a *live* window was a reused one
    and cleared the obligation without ever restoring the window.
    """

    FILETIME = 134357124116156007

    def test_a_filetime_keeps_every_digit(self):
        record = ParkRecord.from_dict(make_record(process_created=self.FILETIME).to_dict())
        self.assertEqual(record.process_created, self.FILETIME)
        self.assertEqual(record.owner_created, 555)

    def test_a_real_process_creation_time_survives_the_journal(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "settings.json.park.json"
        journal = RecoveryJournal(path)
        journal.record_intent(make_record(process_created=self.FILETIME))
        stored = RecoveryJournal(path).get(4242)
        self.assertEqual(stored.process_created, self.FILETIME)
        self.assertNotEqual(
            self.FILETIME,
            int(float(self.FILETIME)),
            "this test is meaningless while an int fits a float exactly",
        )

    def test_this_process_identity_matches_its_own_record(self):
        created = winapi.get_process_creation_time(os.getpid()) if winapi else None
        if created is None:
            self.skipTest("this platform does not report a process creation time")
        record = ParkRecord.from_dict(make_record(process_created=created).to_dict())
        self.assertEqual(record.process_created, created)
        self.assertTrue(
            restoreguard.owner_is_alive(make_record(owner_pid=os.getpid(), owner_created=created))
        )


class MonitorGeometryTests(unittest.TestCase):
    """The gap between monitors is not a visible area."""

    LEFT = (0, 0, 1920, 1080)
    UPPER_RIGHT = (1920, 0, 3200, 800)
    GAP = (2000, 900, 2600, 1040)

    def test_a_window_in_the_gap_is_not_on_any_monitor(self):
        for monitors in ((self.LEFT, self.UPPER_RIGHT), (self.UPPER_RIGHT, self.LEFT)):
            with self.subTest(monitors=monitors):
                self.assertFalse(screen.is_visible_on_monitors(self.GAP, monitors))

    def test_the_gap_is_inside_the_virtual_bounding_box(self):
        bounding = screen.bounding_rect((self.LEFT, self.UPPER_RIGHT))
        self.assertIsNotNone(screen.intersection(self.GAP, bounding))
        self.assertTrue(
            screen.inside_gap(self.GAP, (self.LEFT, self.UPPER_RIGHT), bounding),
            "the regression needs a gap inside the bounding box",
        )

    def test_a_window_on_a_monitor_edge_is_visible(self):
        self.assertTrue(screen.is_visible_on_monitors((1900, 100, 2000, 200), (self.LEFT, self.UPPER_RIGHT)))
        self.assertTrue(screen.is_visible_on_monitors((1920, 100, 2000, 200), (self.LEFT, self.UPPER_RIGHT)))

    def test_a_window_that_only_brushes_a_monitor_is_not_visible(self):
        # 20 visible pixels is below the threshold a usable window needs.
        self.assertFalse(screen.is_visible_on_monitors((1900, 100, 1920, 200), (self.LEFT,)))

    def test_a_window_spanning_two_monitors_counts_as_visible(self):
        monitors = ((0, 0, 1920, 1080), (1920, 0, 3840, 1080))
        self.assertTrue(screen.is_visible_on_monitors((1800, 100, 2000, 200), monitors))

    def test_no_monitors_means_nothing_is_visible(self):
        self.assertFalse(screen.is_visible_on_monitors((0, 0, 800, 600), ()))


@unittest.skipIf(winapi is None, "monitor enumeration needs Windows")
class RealMonitorPredicateTests(unittest.TestCase):
    """The production predicate must be driven by real monitors."""

    def test_the_predicate_uses_enumerated_monitors_not_the_virtual_box(self):
        monitors = winapi.display_monitor_rects()
        self.assertTrue(monitors, "no monitor could be enumerated")
        for monitor in monitors:
            self.assertTrue(screen.is_visible_on_monitors(monitor, monitors))

    def test_the_reported_monitors_agree_with_an_independent_probe(self):
        # MonitorFromPoint is a different Win32 path than EnumDisplayMonitors, so
        # agreement proves the enumeration is not simply returning a cached guess.
        reported = {tuple(rect) for rect in winapi.display_monitor_rects()}
        probed = set()
        for rect in reported:
            x = (rect[0] + rect[2]) // 2
            y = (rect[1] + rect[3]) // 2
            monitor = winapi.user32.MonitorFromPoint(winapi.wintypes.POINT(x, y), 2)
            info = winapi.MONITORINFO()
            info.cbSize = ctypes_sizeof(winapi.MONITORINFO)
            self.assertTrue(winapi.user32.GetMonitorInfoW(monitor, ctypes.byref(info)))
            probed.add((int(info.rcMonitor.left), int(info.rcMonitor.top),
                        int(info.rcMonitor.right), int(info.rcMonitor.bottom)))
        self.assertEqual(reported, probed)

    def test_a_gap_window_is_invisible_while_the_bounding_box_would_call_it_visible(self):
        # Synthetic L-shaped layout, evaluated by the very function the executor
        # uses for "the window is verifiably back".
        monitors = ((0, 0, 1920, 1080), (2560, 0, 3840, 1080))
        gap = (2100, 500, 2500, 700)
        self.assertFalse(winapi.is_visible_on_monitors(gap, monitors))
        bounding = screen.bounding_rect(monitors)
        self.assertIsNotNone(screen.intersection(gap, bounding))

    def test_a_window_off_every_monitor_is_not_effectively_onscreen(self):
        parked = (-32000, -32000, -31900, -31900)
        self.assertFalse(winapi.is_visible_on_monitors(parked, winapi.display_monitor_rects()))


def ctypes_sizeof(struct_type) -> int:
    return ctypes.sizeof(struct_type)


class RecoverySubsystemShapeTests(unittest.TestCase):
    """Static contracts for the invariants this architecture promises."""

    GUARD = (SRC / "restoreguard.py").read_text(encoding="utf-8")
    APP = (SRC / "app.py").read_text(encoding="utf-8")
    JOURNAL = (SRC / "recovery.py").read_text(encoding="utf-8")
    WINAPI = (SRC / "winapi.py").read_text(encoding="utf-8")

    def test_the_journal_documents_both_identities(self):
        for field in ("ownerRunId", "ownerCreated", "claimExecutor", "claimGeneration", "claimToken"):
            self.assertIn(f'"{field}"', self.JOURNAL)

    def test_every_mutation_reads_the_newest_document_under_the_lock(self):
        for mutation in (
            "def record_intent(self, record: ParkRecord)",
            "def mark_parked(self, hwnd: int",
            "def clear_unless_claimed(self, hwnd: int",
            "def claim(",
            "def renew(self, claim: Claim",
            "def release(self, claim: Claim",
            "def release_all(self, executor)",
            "def clear_claimed(self, claim: Claim",
        ):
            body = self.JOURNAL.split(f"    {mutation}", 1)[1].split("\n    def ", 1)[0]
            self.assertTrue(
                "self._commit(" in body or "self._transact(" in body,
                f"{mutation} does not run inside a transaction",
            )

    def test_a_mutation_that_is_not_a_transaction_does_not_exist(self):
        # Every writer goes through _commit/_transact; there is no other write path.
        self.assertEqual(self.JOURNAL.count("self._write()"), 2, "a new write path appeared")

    def test_the_executor_logs_the_identity_of_what_it_works_on(self):
        for token in ("claim gen=", "short_token", "describe()"):
            self.assertIn(token, self.GUARD)

    def test_the_application_records_its_own_identity(self):
        self.assertIn("new_executor_identity(\"main\")", self.APP)
        self.assertIn("owner=self._executor", self.APP)

    def test_the_parking_hook_aborts_without_durable_ownership(self):
        park = self.WINAPI.split("def park_window_offscreen_sync", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if before_park is not None and not before_park(state):", park)

    def test_the_restore_is_verified_against_a_real_monitor(self):
        self.assertIn("is_visible_on_monitors", self.WINAPI)
        self.assertIn("EnumDisplayMonitors", self.WINAPI)
        restore = self.WINAPI.split("def restore_parked_window_sync", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_effectively_onscreen(hwnd)", restore)


class HandoverTimeoutTests(unittest.TestCase):
    """A successor that cannot inherit the mutex must not claim it executed.

    The predecessor treats a readiness token as proof that somebody else now owns
    the journal and exits on it.  So the token must never be emitted on the path
    where the successor merely *gave up waiting* - that merged two different
    situations and let both processes leave with the obligation still on disk.
    """

    GUARD = (SRC / "restoreguard.py").read_text(encoding="utf-8")

    def test_a_successor_that_timed_out_announces_nothing(self):
        run = self.GUARD.split("def run_guardian(", 1)[1].split("\ndef ", 1)[0]
        timeout_branch = run.split("if waited_for_handover:", 1)[1].split("# Normal startup only", 1)[0]
        code = "\n".join(
            line for line in timeout_branch.splitlines() if not line.strip().startswith("#")
        )
        self.assertNotIn("_signal_ready", code)
        self.assertNotIn("delegate", code)
        self.assertIn("return 2", code)

    def test_a_handover_confirmation_must_name_an_owner(self):
        confirm = self.GUARD.split("def _confirm_handover(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not owner:", confirm)
        rejection = confirm.split("if not owner:", 1)[1].split("if predecessor and owner ==", 1)[0]
        self.assertIn("return False", rejection)


class DamageReadFailureTests(unittest.TestCase):
    """A damage pass that cannot read must not take the executor down with it.

    ``_pass`` guards its own snapshot, but resolving damage reads the journal a
    second time and touches quarantine artefacts.  Both were unguarded, so a busy
    or swept journal raised straight out of ``run()`` - and the guardian announces
    readiness before it starts working, so the application may already have exited
    on the strength of that confirmation.
    """

    def test_the_second_damage_read_failure_keeps_the_resolution_undecided(self):
        class BusyJournal:
            path = Path("busy.park.json")
            damage_reason = "unreadable"

            def snapshot(self):
                raise recovery.JournalReadError("the journal lock is busy")

        result = restoreguard.resolve_journal_damage(BusyJournal(), module=object())
        self.assertFalse(result.decided)
        self.assertIn("could not be re-read", result.reason)

    def test_a_quarantine_artefact_swept_mid_token_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json.park.json"
            journal = RecoveryJournal(path)
            journal.snapshot()
            artefact = path.with_name(path.name + recovery.QUARANTINE_SUFFIX)
            artefact.write_text('{"records": []}', encoding="utf-8")
            self.assertTrue(recovery._quarantine_names(path))
            real_stat = Path.stat

            def swept(path_to_stat, *args, **kwargs):
                if Path(path_to_stat) == artefact:
                    raise FileNotFoundError("another executor swept this artefact")
                return real_stat(path_to_stat, *args, **kwargs)

            with patch.object(Path, "stat", swept):
                token = journal._damage_token()
            self.assertIsInstance(token, str)
            self.assertTrue(token)


class StaleRecordTests(unittest.TestCase):
    """A stale record must stay executable, not degrade into a bare intent.

    ``ParkRecord.parked`` is derived from the state, so it is False for a stale
    record.  Mapping that straight onto an intent produced a durable obligation
    that nothing restores - the exact silent loss this journal exists to prevent.
    """

    def _stale_entry(self) -> ParkRecord:
        return make_record(
            101,
            owner_pid=finished_pid(),
            owner_created=1,
            owner_run_id="previous-run",
            state=recovery.STATE_STALE,
        )

    def test_claiming_a_stale_record_promotes_it_to_parked(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json.park.json"
            journal = RecoveryJournal(path)
            self.assertTrue(journal.record_intent(self._stale_entry()))
            claimed = journal.claim(101, executor("s"))
            self.assertIsNotNone(claimed)
            stored = journal.get(101)
            self.assertEqual(stored.state, recovery.STATE_PARKED)
            self.assertTrue(stored.parked)


if __name__ == "__main__":
    unittest.main()
