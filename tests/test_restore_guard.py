"""An outstanding park obligation must always have an executor.

The record in the journal is not enough on its own - the previous failure mode
was a window that stayed off-screen because nothing was left alive to put it
back.  These tests cover the decision policy of the executor, the handover
contract between the application and the guardian, and the real guardian
process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import restoreguard
from recovery import (
    STATE_INTENT,
    STATE_PARKED,
    ExecutorIdentity,
    ParkRecord,
    RecoveryJournal,
)

try:
    import winapi
except Exception:  # pragma: no cover - non-Windows
    winapi = None

GUARD = (SRC / "restoreguard.py").read_text(encoding="utf-8")
APP = (SRC / "app.py").read_text(encoding="utf-8")
SMOKE = (ROOT / "tools" / "runtime_smoke.py").read_text(encoding="utf-8")

OWNER_RUN_ID = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


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
        "owner_run_id": OWNER_RUN_ID,
        "owner_created": 555,
        "recorded_at": time.time(),
        "label": "app.exe - Some",
        "state": STATE_PARKED,
    }
    values.update(overrides)
    return ParkRecord(**values)


def executor(tag: str = "a") -> ExecutorIdentity:
    return ExecutorIdentity(executor_id=f"exec-{tag}", pid=1000 + ord(tag), created=42, label=tag)


class FakeWinapi:
    """A scriptable stand-in for the Win32 layer the executor consults."""

    def __init__(self, *, is_window=True, pid=1234, class_name="SomeWindowClass",
                 created=999, parked=False, onscreen=True):
        self.is_alive = is_window
        self.pid = pid
        self.class_name = class_name
        self.created = created
        self.parked = parked
        self.onscreen = onscreen
        self.restore_calls = 0
        self.orphan_calls = 0
        self.restore_result = True
        self.orphan_result = True

    def is_window(self, hwnd):
        return self.is_alive

    def get_pid(self, hwnd):
        return self.pid

    def get_class_name(self, hwnd):
        return self.class_name

    class _Identity:
        def __init__(self, created):
            self.created = created

    def _query_process_identity(self, pid, query_name=True):
        return self._Identity(self.created)

    def looks_like_lookup_parked(self, hwnd):
        return self.parked

    def is_effectively_onscreen(self, hwnd, min_visible=24):
        return self.onscreen

    def window_matches_parked_state(self, hwnd, state):
        return True

    def restore_parked_window_sync(self, hwnd, state):
        self.restore_calls += 1
        if self.restore_result:
            self.parked = False
            self.onscreen = True
        return self.restore_result

    def recover_orphaned_lookup_park(self, hwnd):
        self.orphan_calls += 1
        if self.orphan_result:
            self.parked = False
            self.onscreen = True
        return self.orphan_result

    def state_from_record(self, record):
        # The stand-in never builds real Win32 structures: the executor only
        # hands this object back to the other fake methods below.
        return ("fake-parked-state", int(record.hwnd))


class AssessTests(unittest.TestCase):
    """What the executor is allowed to conclude about a record."""

    def test_a_vanished_window_ends_the_obligation(self):
        self.assertEqual(restoreguard.assess(FakeWinapi(is_window=False), make_record()), "gone")

    def test_a_reused_handle_ends_the_obligation(self):
        self.assertEqual(restoreguard.assess(FakeWinapi(pid=9999), make_record()), "reused")
        self.assertEqual(
            restoreguard.assess(FakeWinapi(class_name="Other"), make_record()), "reused"
        )
        self.assertEqual(
            restoreguard.assess(FakeWinapi(created=1000), make_record()), "reused"
        )

    def test_a_creation_time_that_differs_in_the_last_digits_is_still_a_reuse(self):
        # Win32 creation times are 64-bit FILETIME values.  Comparing them
        # through a float would corrupt the last digits and then *match*, which
        # would let an executor move a window that is not the one it recorded.
        record = make_record(process_created=134357124116156007)
        parked = FakeWinapi(created=record.process_created, parked=True, onscreen=False)
        self.assertEqual(restoreguard.assess(parked, record), "parked")
        self.assertEqual(
            restoreguard.assess(FakeWinapi(created=record.process_created + 7, parked=True, onscreen=False), record),
            "reused",
        )

    def test_an_unverifiable_identity_is_never_treated_as_obsolete(self):
        # Access denied on the target process must defer the decision, not
        # silently discard the only proof that a window is parked.
        self.assertEqual(restoreguard.assess(FakeWinapi(created=None), make_record()), "unverified-identity")

    def test_a_parked_window_is_still_parked(self):
        self.assertEqual(
            restoreguard.assess(FakeWinapi(parked=True, onscreen=False), make_record()), "parked"
        )

    def test_a_visible_unparked_window_is_released(self):
        self.assertEqual(restoreguard.assess(FakeWinapi(), make_record()), "visible")

    def test_a_window_off_every_monitor_is_not_released(self):
        self.assertEqual(
            restoreguard.assess(FakeWinapi(onscreen=False), make_record()), "offscreen"
        )


class ResolvePolicyTests(unittest.TestCase):
    """The outcome of one record, including the cases that must keep the record."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.fake = FakeWinapi()
        self.executor = executor("a")

    def tearDown(self):
        self._tmp.cleanup()

    def resolve(self, record, fake=None, **kwargs):
        module = fake or self.fake
        original = restoreguard.winapi
        restoreguard.winapi = lambda: module
        try:
            if record is None:
                record = make_record()
            if self.journal.get(int(record.hwnd)) is None:
                self.journal.record_intent(record)
            return restoreguard.resolve(
                self.journal, record, self.executor, **kwargs
            )
        finally:
            restoreguard.winapi = original

    def test_a_restored_window_is_cleared(self):
        self.fake.parked = True
        self.fake.onscreen = False
        outcome = self.resolve(make_record())
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "restored")
        self.assertFalse(self.path.exists())

    def test_a_dead_window_is_cleared_without_touching_it(self):
        outcome = self.resolve(make_record(), fake=FakeWinapi(is_window=False))
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "window no longer exists")
        self.assertFalse(self.path.exists())

    def test_a_reused_window_is_cleared(self):
        outcome = self.resolve(make_record(), fake=FakeWinapi(created=4242))
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "window handle was reused by another window")

    def test_a_committed_record_that_is_back_on_screen_is_released(self):
        # A committed record whose window is verifiably back on a monitor and no
        # longer parked needs no restore at all.
        outcome = self.resolve(make_record())
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "window is back on a monitor")
        self.assertEqual(self.fake.restore_calls, 0)

    def test_a_failed_restore_keeps_the_record_and_keeps_owning_it(self):
        # The caller gave up (a budget), not the obligation: the record stays and
        # the lease is released so another executor may continue immediately.
        self.fake.restore_result = False
        self.fake.orphan_result = False
        self.fake.parked = True
        self.fake.onscreen = False
        outcome = self.resolve(make_record(), budget_sec=0.3)
        self.assertFalse(outcome.resolved)
        record = RecoveryJournal(self.path).get(4242)
        self.assertIsNotNone(record)
        self.assertEqual(record.claim_executor, "", "the lease was not released")

    def test_a_committed_window_off_screen_is_restored_not_released(self):
        self.fake.parked = False
        self.fake.onscreen = False
        outcome = self.resolve(make_record())
        self.assertTrue(outcome.resolved)
        self.assertEqual(self.fake.restore_calls, 1)
        self.assertFalse(self.path.exists())

    def test_a_pending_intent_is_not_released_just_because_it_is_visible(self):
        # The park may still land after this process is gone: releasing the
        # record now is exactly how a delayed old park strands a window.
        self.journal.record_intent(make_record(state=STATE_INTENT))
        record = self.journal.get(4242)
        original_settle = restoreguard.PENDING_SETTLE_SEC
        restoreguard.PENDING_SETTLE_SEC = 30.0
        try:
            outcome = self.resolve(record, context="guardian", budget_sec=0.3)
        finally:
            restoreguard.PENDING_SETTLE_SEC = original_settle
        self.assertFalse(outcome.resolved)
        self.assertEqual([r.hwnd for r in RecoveryJournal(self.path).records()], [4242])

    def test_a_pending_intent_that_lands_later_is_restored(self):
        self.journal.record_intent(make_record(state=STATE_INTENT))
        record = self.journal.get(4242)
        fake = FakeWinapi(parked=True, onscreen=False)
        original_settle = restoreguard.PENDING_SETTLE_SEC
        restoreguard.PENDING_SETTLE_SEC = 5.0
        try:
            outcome = self.resolve(record, fake=fake, context="guardian")
        finally:
            restoreguard.PENDING_SETTLE_SEC = original_settle
        self.assertTrue(outcome.resolved)
        self.assertEqual(outcome.reason, "restored")

    def test_a_pending_intent_that_never_lands_is_released_after_settling(self):
        self.journal.record_intent(make_record(state=STATE_INTENT))
        record = self.journal.get(4242)
        original_settle = restoreguard.PENDING_SETTLE_SEC
        restoreguard.PENDING_SETTLE_SEC = 0.2
        try:
            outcome = self.resolve(record, context="guardian")
        finally:
            restoreguard.PENDING_SETTLE_SEC = original_settle
        self.assertTrue(outcome.resolved)
        self.assertIn("never applied", outcome.reason)

    def test_an_executor_that_owns_the_record_releases_its_lease(self):
        self.resolve(make_record())
        self.assertFalse(self.path.exists())

    def test_a_record_leased_by_another_executor_is_left_alone(self):
        record = make_record()
        self.journal.record_intent(record)
        self.journal.claim(4242, executor("b"))
        outcome = self.resolve(record)
        self.assertFalse(outcome.resolved)
        self.assertEqual(outcome.reason, "claimed-by-another-executor")
        self.assertEqual([r.hwnd for r in RecoveryJournal(self.path).records()], [4242])

    def test_a_fresh_executor_never_invents_obligations_from_a_damaged_file(self):
        # The guardian reloads from disk on every pass: a document it cannot read
        # has to read as "nothing to do", never as "a window is parked".
        self.journal.record_intent(make_record())
        self.path.write_text("{not json", encoding="utf-8")
        fresh = RecoveryJournal(self.path)
        self.assertEqual(fresh.records(), ())
        self.assertTrue(Path(str(self.path) + ".invalid").exists())


class GuardianCommandTests(unittest.TestCase):
    def test_a_source_run_launches_the_guardian_module(self):
        command = restoreguard.guardian_command("C:/tmp/x.park.json", 4321, 555, OWNER_RUN_ID)
        self.assertEqual(command[0], sys.executable)
        self.assertIn("restoreguard.py", command[1])
        self.assertEqual(command[2], restoreguard.GUARDIAN_ARG)
        self.assertEqual(command[3], "C:/tmp/x.park.json")
        # The partner is a full identity, not a PID: a recycled PID must not be
        # able to keep the guardian waiting for an owner that is long gone.
        self.assertEqual(command[4:7], ["4321", "555", OWNER_RUN_ID])
        self.assertNotIn("--handover-from", command)

    def test_a_successor_declares_its_predecessor(self):
        command = restoreguard.guardian_command("C:/tmp/x.park.json", 0, 0, "", "exec-old")
        self.assertIn("--handover-from", command)
        self.assertEqual(command[command.index("--handover-from") + 1], "exec-old")

    def test_a_missing_owner_identity_stays_empty(self):
        command = restoreguard.guardian_command("C:/tmp/x.park.json")
        self.assertEqual(command[4:], ["0", "0", ""])

    def test_a_frozen_build_re_executes_its_own_executable(self):
        original = getattr(sys, "frozen", None)
        original_executable = sys.executable
        sys.frozen = True  # type: ignore[attr-defined]
        sys.executable = r"C:\dist\LookUpWindows.exe"
        try:
            command = restoreguard.guardian_command("C:/tmp/x.park.json")
        finally:
            if original is None:
                del sys.frozen  # type: ignore[attr-defined]
            else:
                sys.frozen = original  # type: ignore[attr-defined]
            sys.executable = original_executable
        self.assertEqual(command[0], r"C:\dist\LookUpWindows.exe")
        self.assertEqual(command[1], restoreguard.GUARDIAN_ARG)

    def test_the_guardian_argument_is_recognised(self):
        self.assertEqual(restoreguard.run_guardian_from_argv(["other"]), 2)
        self.assertEqual(restoreguard.run_guardian_from_argv([restoreguard.GUARDIAN_ARG]), 2)


class OwnerIdentityTests(unittest.TestCase):
    """A PID is a locator; the creation time is the identity."""

    def test_this_process_with_its_recorded_creation_time_is_a_live_owner(self):
        record = make_record(owner_pid=os.getpid(), owner_created=restoreguard.process_creation_time(os.getpid()))
        self.assertTrue(restoreguard.owner_is_alive(record))

    def test_a_reused_pid_with_a_different_creation_time_is_a_dead_owner(self):
        # The exact failure mode: LookUp died, Windows handed its PID to an
        # unrelated process, and the obligation must not be blocked by it.
        record = make_record(owner_pid=os.getpid(), owner_created=1)
        self.assertFalse(restoreguard.owner_is_alive(record))

    def test_an_unverifiable_creation_time_is_not_trusted(self):
        record = make_record(owner_pid=os.getpid(), owner_created=1)
        self.assertFalse(restoreguard.owner_is_alive(record, creation_probe=lambda _pid: None))

    def test_a_record_without_owner_identity_is_never_trusted(self):
        self.assertFalse(restoreguard.owner_is_alive(make_record(owner_created=None)))
        self.assertFalse(restoreguard.owner_is_alive(make_record(owner_pid=0)))

    def test_a_dead_pid_is_a_dead_owner(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait(timeout=30)
        record = make_record(owner_pid=child.pid, owner_created=12345)
        self.assertFalse(restoreguard.owner_is_alive(record))


class ProcessLivenessTests(unittest.TestCase):
    def test_a_dead_pid_is_reported_as_dead(self):
        # A finished child of this test is the cheapest way to get a PID that is
        # certainly not running.
        child = subprocess.Popen(
            [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        child.wait(timeout=30)
        self.assertFalse(restoreguard.process_is_alive(child.pid))

    def test_this_process_is_reported_as_alive(self):
        self.assertTrue(restoreguard.process_is_alive(os.getpid()))

    def test_an_impossible_pid_is_reported_as_dead(self):
        self.assertFalse(restoreguard.process_is_alive(0))
        self.assertFalse(restoreguard.process_is_alive(-1))


@unittest.skipIf(winapi is None, "the guardian restores real windows; Windows only")
class GuardianProcessTests(unittest.TestCase):
    """The guardian really runs as a separate process and really executes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal = RecoveryJournal(Path(self._tmp.name) / "settings.json.park.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_an_empty_journal_makes_the_guardian_exit_at_once(self):
        started = time.monotonic()
        self.assertEqual(restoreguard.run_guardian(self.journal.path, announce=False), 0)
        self.assertLess(time.monotonic() - started, 30.0)

    def test_a_second_guardian_stops_and_lets_the_first_finish(self):
        first = subprocess.Popen(
            restoreguard.guardian_command(str(self.journal.path)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(first.kill)
        mutex = restoreguard.guardian_mutex_name(self.journal.path)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            # Once the first guardian owns the mutex of *this* journal, acquiring
            # it here must report "taken"; the probe handle has to be released
            # again so the rest of the suite is not blocked by this test.
            probe = restoreguard._acquire_singleton(mutex)
            if probe is None:
                break
            probe.close()
            time.sleep(0.2)
        else:
            self.fail("the first guardian never took the singleton mutex")
        second = subprocess.run(
            restoreguard.guardian_command(str(self.journal.path)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        # The delegate has to name the journal it is answering for, or the
        # predecessor cannot tell a real successor from another document's.
        self.assertIn(
            f"journal={restoreguard.journal_identity(self.journal.path)}",
            second.stdout,
        )
        first.wait(timeout=60)

    def test_the_guardian_outlives_the_process_that_handed_over_to_it(self):
        # A park worker blocked just before it registers ownership writes its
        # record while the handing-off process is still alive.  A guardian that
        # trusted a single empty read would exit and leave that window parked.
        blocked = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(blocked.kill)
        guardian = subprocess.Popen(
            restoreguard.guardian_command(str(self.journal.path), blocked.pid),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(guardian.kill)
        time.sleep(3.0)
        self.assertIsNone(guardian.poll(), "the guardian exited while its partner was alive")
        finished = subprocess.Popen(
            [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        finished.wait(timeout=30)
        self.journal.record_intent(make_record(hwnd=0x7FFFFFF0, owner_pid=finished.pid))
        blocked.kill()
        blocked.wait(timeout=30)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and guardian.poll() is None:
            time.sleep(0.25)
        self.assertIsNotNone(guardian.poll(), "the guardian did not exit after draining")
        self.assertEqual(guardian.returncode, 0)
        self.assertFalse(Path(self.journal.path).exists())


class HandoverContractTests(unittest.TestCase):
    """Static contracts for the shutdown handover."""

    def test_quit_hands_the_obligation_over_before_exiting(self):
        quit_block = APP.split("    def quit(self)", 1)[1].split("\n    def ", 1)[0]
        # The exit only *publishes* the request: the flush, the joins, the restore
        # barrier, the journal reads and the guardian handshake all block, and none
        # of them may run on the Tk thread while the panel is still up.
        self.assertIn("_start_shutdown_worker(wait=False)", quit_block)
        self.assertIn("self._shutdown_requested.set()", quit_block)
        self.assertNotIn("_restore_sources_for_shutdown", quit_block)
        self.assertNotIn("_teardown_services()", quit_block)
        self.assertNotIn("self._finish_shutdown()", quit_block)
        self.assertNotIn("_handover_unfinished_restores()", quit_block)
        # ... and without a worker there is no exit at all, rather than an inline
        # fallback that would put the journal lock on the UI thread.
        self.assertIn("_notify_shutdown_unavailable()", quit_block)
        teardown = APP.split("def _finish_shutdown(self)", 1)[1].split("\n\n\ndef ", 1)[0]
        self.assertIn("destroy_window", teardown)
        worker = APP.split("def _shutdown_worker(self)", 1)[1].split("\n    def ", 1)[0]
        # The worker keeps retrying and only finishes the teardown after the
        # obligations really have an executor somewhere else.
        self.assertIn("_teardown_services", worker)
        self.assertIn("_handover_unfinished_restores", worker)
        self.assertIn("while not handed", worker)
        self.assertIn("self.defer(self._finish_shutdown)", worker)
        # The barrier and the journal work are grouped into the one place that runs
        # off the UI thread.
        teardown_services = APP.split("def _teardown_services(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_restore_sources_for_shutdown", teardown_services)

    def test_the_handover_starts_a_guardian_and_needs_its_readiness(self):
        handover = APP.split("def _handover_unfinished_restores", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("restoreguard.spawn_guardian", handover)
        self.assertIn("RESTORE_HANDOVER_TIMEOUT_SEC", handover)

    def test_without_a_guardian_the_process_waits_instead_of_walking_away(self):
        handover = APP.split("def _handover_unfinished_restores", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("restoreguard.wait_for_handover", handover)
        self.assertIn("RESTORE_FALLBACK_WAIT_SEC", handover)

    def test_the_guardian_only_exits_when_nothing_is_outstanding(self):
        guardian_class = GUARD.split("class _Guardian:", 1)[1].split("\ndef ", 1)[0]
        # One snapshot answers records, salvaged entries and damage together: a
        # record list alone cannot tell "nothing is parked" from "the document
        # could not be read".
        self.assertIn("snapshot = self.journal.snapshot()", guardian_class)
        self.assertIn("mine = [record for record in records", guardian_class)
        self.assertIn("outstanding = outstanding or snapshot.damaged", guardian_class)
        self.assertIn("GUARDIAN_IDLE_EXIT_SEC", guardian_class)
        self.assertIn("release_all", guardian_class)
        self.assertIn("JOURNAL_UNREADABLE_POLL_CEILING_SEC", guardian_class)

    def test_an_unreadable_journal_never_ends_the_executor(self):
        # The bug this replaces: after 30s of unreadable journal the guardian
        # returned 1.  The application may already have exited on the handover, so
        # nobody was left who could ever restore the window - and a saved JSON
        # file is not a live executor.
        guardian_class = GUARD.split("class _Guardian:", 1)[1].split("\ndef ", 1)[0]
        unreadable = guardian_class.split("except JournalReadError:", 1)[1].split("\n", 1)[0]
        self.assertNotIn("return 1", unreadable)
        self.assertNotIn("JOURNAL_UNREADABLE_EXIT_SEC", GUARD)
        self.assertIn("self._unreadable_attempts += 1", guardian_class)

    def test_a_damaged_journal_never_reports_no_obligations(self):
        # Quarantining keeps the file, not the obligation: the damage has to be
        # resolved by actually bringing the stranded windows back.
        self.assertIn("resolve_journal_damage", GUARD)
        self.assertIn("def resolve_journal_damage", GUARD)
        # A sweep that could not decide - because it found a window it could not
        # bring back, or because the desktop could not be enumerated - is not an
        # answer, and must not lift the damage.
        self.assertIn("def _sweep_result", GUARD)
        self.assertIn("decided = sweep.decided and not outstanding", GUARD)

    def test_the_guardian_has_no_lifetime_limit(self):
        # A hard lifetime is the defect this executor was rebuilt to remove: it is
        # what used to leave a live parked window with nobody to restore it.
        for gone in ("GUARDIAN_MAX_LIFETIME_SEC", "COMMITTED_MAX_SEC", "PENDING_MAX_SEC"):
            self.assertNotIn(gone, GUARD)
        guardian_class = GUARD.split("class _Guardian:", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("deadline", guardian_class)
        self.assertNotIn("max_lifetime", GUARD)

    def test_a_stopped_guardian_only_leaves_after_a_confirmed_handover(self):
        guardian_class = GUARD.split("class _Guardian:", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("def _hand_off_on_stop", guardian_class)
        self.assertIn("def handover", guardian_class)
        self.assertIn("_confirm_handover(", guardian_class)
        # The successor must exist before anything is given up.
        handover = guardian_class.split("def handover", 1)[1]
        self.assertLess(handover.index("spawn_guardian"), handover.index("release_all"))
        self.assertLess(handover.index("release_all"), handover.index("_confirm_handover"))
        # ... and it must be told which executor it takes over from, so it keeps
        # watching for park operations this process may still write.
        self.assertIn("owner_pid=self.executor.pid", handover)
        self.assertIn("owner_run_id=self.executor.executor_id", handover)

    def test_the_handover_confirmation_is_checked_not_assumed(self):
        confirm = GUARD.split("def _confirm_handover(", 1)[1].split("\ndef ", 1)[0]
        # Both stages of the handshake are read from one pipe, so the second
        # token is not written into a stream that was already closed.
        self.assertIn("_link_for(process)", confirm)
        self.assertIn("journal_id", confirm)
        self.assertIn("announced != journal_id", confirm)

    def test_the_executor_mutex_is_named_after_the_journal(self):
        self.assertIn("def guardian_mutex_name", GUARD)
        self.assertIn("journal_identity", GUARD)
        run_guardian = GUARD.split("def run_guardian(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("guardian_mutex_name(journal.path)", run_guardian)

    def test_the_guardian_only_works_on_records_of_dead_owners(self):
        self.assertIn("owner_is_alive(record)", GUARD)
        self.assertIn("def owner_is_alive", GUARD)

    def test_a_record_another_executor_is_still_working_on_is_not_nothing_outstanding(self):
        # A startup worker that outlived its budget keeps its lease; treating that
        # as "no obligations" is how a late-finishing worker lost its window.
        guardian_class = GUARD.split("class _Guardian:", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("def _leased_elsewhere", guardian_class)
        self.assertIn("_leased_elsewhere(record)", guardian_class)

    def test_readiness_is_announced_only_after_the_journal_was_read(self):
        run_guardian = GUARD.split("def run_guardian(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("await_initial_state(journal, identity)", run_guardian)
        self.assertLess(
            run_guardian.index("await_initial_state(journal, identity)"),
            run_guardian.index("_signal_ready(detail=f\"owner="),
        )

    def test_the_guardian_is_not_a_busy_loop(self):
        self.assertIn("BACKOFF_SCHEDULE_SEC", GUARD)
        self.assertIn("def backoff_delay", GUARD)
        self.assertIn("BACKOFF_CEILING_SEC", GUARD)
        self.assertIn("def throttled", GUARD)

    def test_the_recovery_executor_is_not_bound_to_the_capture_job(self):
        # Capture helpers are killed with the application; the recovery executor
        # has to survive it, so it is started with a job breakaway.
        self.assertIn("_CREATE_BREAKAWAY_FROM_JOB", GUARD)
        self.assertIn("close_fds=True", GUARD)

    def test_the_executor_is_allowed_only_outside_a_kill_on_close_job(self):
        # Membership in *some* job is not evidence of danger: an ordinary job,
        # or none, is where a supervised desktop application normally runs.  Only
        # a confirmed kill-on-close job that forbids breakaway forbids the
        # guardian, and park fails closed with it.
        classify = GUARD.split("def _classify_job_flags", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", classify)
        self.assertIn("can_escape", classify)
        read = GUARD.split("def enclosing_job", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("QueryInformationJobObject(", read)
        self.assertIn("_classify_job_flags(flags)", read)
        self.assertIn(
            "GUARDIAN_CONTEXT_NONE, GUARDIAN_CONTEXT_ORDINARY",
            GUARD.split("def permits_guardian", 1)[1].split("\ndef ", 1)[0],
            "only those two job contexts may carry a recovery executor",
        )
        self.assertFalse(
            restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_UNREADABLE, None).permits_guardian,
            "a job whose limits cannot be read is not an ordinary job",
        )
        self.assertIn(
            "EnclosingJob(GUARDIAN_CONTEXT_UNREADABLE, None)",
            read,
            "an unreadable job has to be classified, not silently treated as ordinary",
        )

    def test_the_two_breakaway_limits_stay_separate(self):
        # SILENT_BREAKAWAY_OK detaches children by itself and forbids
        # CREATE_BREAKAWAY_FROM_JOB unless BREAKAWAY_OK is also set, so treating
        # the two limits as one is not a simplification but a failed launch.
        model = GUARD.split("class EnclosingJob", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("def explicit_breakaway", model)
        self.assertIn("def silent_breakaway", model)
        self.assertIn("def can_escape", model)
        self.assertNotIn("def breaks_away", model)
        flags = GUARD.split("def _guardian_creation_flags", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("enclosing.explicit_breakaway", flags)
        self.assertNotIn(
            "enclosing.can_escape",
            flags,
            "can_escape answers 'may the child leave', not 'is the flag legal'",
        )

    def test_guardian_refusal_is_decided_before_anything_is_launched(self):
        spawn = GUARD.split("def _spawn", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not enclosing.permits_guardian:", spawn)
        self.assertLess(
            spawn.index("if not enclosing.permits_guardian:"),
            spawn.index("subprocess.Popen("),
            "a hostile enclosing job must be refused before a process is created",
        )
        self.assertIn("_guardian_creation_flags(enclosing)", spawn)

    def test_guardian_breakaway_is_fail_closed_and_verified(self):
        spawn = GUARD.split("def _spawn", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("process_in_any_job(process)", spawn)
        self.assertIn("if inherited_job is None:", spawn)
        self.assertIn("enclosing.kind == GUARDIAN_CONTEXT_NONE and inherited_job", spawn)
        self.assertIn("if not job.adopt(process)", spawn)
        self.assertIn("breakaway was denied", spawn)
        self.assertNotIn("starting it inside", spawn)
        self.assertNotIn("for attempt, flags", spawn)


    def test_startup_recovery_uses_the_same_executor(self):
        self.assertIn("restoreguard.resolve_all", APP)
        self.assertIn("restoreguard.resolve(", APP)
        self.assertIn("self._executor", APP)


JOB_REPORT_CHILD = r"""import ctypes
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, {src!r})
import restoreguard

go, out, job_name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
while not go.exists():
    time.sleep(0.01)
context = restoreguard.enclosing_job()
creationflags = restoreguard._guardian_creation_flags(context)
report = {{
    "kind": context.kind,
    "flags": context.limit_flags,
    "explicit": context.explicit_breakaway,
    "silent": context.silent_breakaway,
    "can_escape": context.can_escape,
    "permits": context.permits_guardian,
    "requested_breakaway": bool(creationflags & restoreguard._CREATE_BREAKAWAY_FROM_JOB),
}}
# Launching a throwaway child is what makes the two breakaway limits separable
# in practice instead of only in theory: a silent-breakaway job detaches the
# child by itself, and a job that granted only the silent one rejects
# CREATE_BREAKAWAY_FROM_JOB outright - so a wrong request shows up here as a
# real OS failure rather than as a difference nobody can observe.
try:
    child = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
except OSError as exc:
    report["child_launched"] = False
    report["child_error"] = getattr(exc, "winerror", None)
    report["child_in_this_job"] = None
else:
    with child:
        report["child_launched"] = True
        # Membership of *this* job, asked with that job's own handle: "is the
        # child in some job" cannot answer it, because a child that did break
        # away is still legitimately inside the job the test host itself runs
        # in.  None means the question could not be asked, never "yes".
        report["child_in_this_job"] = None
        kernel32 = restoreguard._job_kernel32()
        job = kernel32.OpenJobObjectW(restoreguard._JOB_OBJECT_ALL_ACCESS, False, job_name)
        if job:
            try:
                in_job = ctypes.c_int()
                if kernel32.IsProcessInJob(child._handle, job, ctypes.byref(in_job)):
                    report["child_in_this_job"] = bool(in_job.value)
            finally:
                kernel32.CloseHandle(job)
out.write_text(json.dumps(report), encoding="utf-8")
"""


class JobLimitConstantTests(unittest.TestCase):
    """The limit flags of ``JOBOBJECT_BASIC_LIMIT_INFORMATION``, spelled out.

    Everything else in this file reads the flags back out of the module that
    also interprets them, so it cannot catch a wrong bit: with the old values
    the whole suite agreed with itself while reading a real kill-on-close job
    with real breakaway as a job nobody can leave.  0x00004000 is
    ``JOB_OBJECT_LIMIT_SUBSET_AFFINITY`` and 0x08000000 is not a job limit at
    all, so the numbers are asserted here against what WinNT.h defines.
    """

    def test_the_job_limits_are_the_values_win32_defines(self):
        self.assertEqual(restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK, 0x00000800)
        self.assertEqual(restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, 0x00001000)
        self.assertEqual(restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0x00002000)

    def test_the_breakaway_limits_do_not_alias_the_creation_flag(self):
        # 0x08000000 was carried here as "breakaway ok"; it is CREATE_NO_WINDOW,
        # and reading it back out of LimitFlags could never match a real job.
        for flag in (
            restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK,
            restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
            restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        ):
            with self.subTest(flag=hex(flag)):
                self.assertNotEqual(flag, restoreguard._CREATE_NO_WINDOW)
                self.assertNotEqual(flag, restoreguard._CREATE_BREAKAWAY_FROM_JOB)

    def test_the_two_breakaway_limits_are_independent_bits(self):
        flags = (
            restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK
            | restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            | restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        self.assertEqual(
            flags
            & (
                restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK
                | restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            ),
            0x00001800,
        )


# The job limits this file configures, written out rather than read back from
# the module under test.  Configuring a job with ``restoreguard``'s own
# constants would let a wrong bit define the fixture and then be confirmed by
# it - the failure that shipped, where the suite agreed with itself about a job
# that Windows had never been asked to create.
WIN_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
WIN_JOB_LIMIT_BREAKAWAY_OK = 0x00000800
WIN_JOB_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000


class EnclosingJobTests(unittest.TestCase):
    """The enclosing Job decides whether a recovery executor may exist at all.

    These are real Job Objects on a real machine: the classification decides
    whether LookUp may park a window at all, so it is checked by what Windows
    reports rather than by what the code says about itself.
    """

    def setUp(self):
        if os.name != "nt":
            self.skipTest("Windows Job Objects")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _classify_under(self, adopt, job_name: str = "") -> dict:
        go = self.dir / "go"
        report = self.dir / "report.json"
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                JOB_REPORT_CHILD.format(src=str(SRC)),
                str(go),
                str(report),
                job_name,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(self._reap, child)
        self.assertTrue(adopt(child), "the child could not be placed in the controlled job")
        go.touch()
        child.wait(timeout=60.0)
        self.assertTrue(report.exists(), "the child reported no job context")
        return json.loads(report.read_text(encoding="utf-8"))

    @staticmethod
    def _reap(child) -> None:
        if child.poll() is None:
            child.kill()
            try:
                child.wait(timeout=10.0)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _job_with(self, limit_flags: int):
        """A named real job with exactly these limits, and a callback to enter it.

        The limits are the point: Windows refuses
        ``CREATE_BREAKAWAY_FROM_JOB`` for a job that did not ask for it, so the
        verdict is only observable against a job that genuinely was configured
        the way the test claims.  The name lets the helper ask about *this* job
        by handle instead of asking whether the child is in some job at all.
        """
        import ctypes

        kernel32 = restoreguard._job_kernel32()
        name = "Local\\LookUpWindows-JobProbe-%s" % os.urandom(8).hex()
        handle = kernel32.CreateJobObjectW(None, name)
        self.assertTrue(handle, "the controlled job could not be created")
        self.addCleanup(kernel32.CloseHandle, ctypes.c_void_p(handle))
        info = restoreguard._JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = limit_flags
        self.assertTrue(
            kernel32.SetInformationJobObject(
                handle,
                restoreguard._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ),
            f"the job rejected the limits {limit_flags:#x} (error {ctypes.get_last_error()})",
        )
        read_back = restoreguard._JobExtendedLimitInformation()
        self.assertTrue(
            kernel32.QueryInformationJobObject(
                handle,
                restoreguard._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(read_back),
                ctypes.sizeof(read_back),
                None,
            )
        )
        self.assertEqual(
            int(read_back.BasicLimitInformation.LimitFlags),
            limit_flags,
            "the job under test is not the job this test claims it configured",
        )

        def adopt(child):
            return bool(kernel32.AssignProcessToJobObject(handle, ctypes.c_void_p(child._handle)))

        return name, adopt

    def test_a_kill_on_close_job_without_breakaway_forbids_the_guardian(self):
        # The production failure this whole decision exists for: a launcher that
        # closes the job takes LookUp and its executor in the same step.
        job = restoreguard._GuardianLaunchJob()
        self.addCleanup(job.close)
        self.assertTrue(job.available)
        report = self._classify_under(job.adopt, job.name)
        self.assertEqual(report["kind"], restoreguard.GUARDIAN_CONTEXT_HOSTILE)
        self.assertTrue(
            report["flags"] & WIN_JOB_LIMIT_KILL_ON_JOB_CLOSE,
            "the job under test must really be a kill-on-close one",
        )
        self.assertFalse(
            report["flags"] & (WIN_JOB_LIMIT_BREAKAWAY_OK | WIN_JOB_LIMIT_SILENT_BREAKAWAY_OK),
            "the job under test must grant no breakaway at all",
        )
        self.assertFalse(report["permits"])
        self.assertFalse(report["can_escape"])
        self.assertFalse(report["explicit"])
        self.assertFalse(report["silent"])
        self.assertFalse(report["requested_breakaway"])
        # The control for the two tests below: without breakaway the child stays
        # put, which is what makes their "the child left" result mean something.
        self.assertTrue(report["child_launched"], report.get("child_error"))
        self.assertIs(
            report["child_in_this_job"],
            True,
            "a child of a job that forbids breakaway cannot leave it",
        )

    def test_a_kill_on_close_job_with_breakaway_ok_still_permits_the_guardian(self):
        # Being killed with the launcher is survivable exactly when the child can
        # be created outside that job, so the guardian must still run - and must
        # ask for the breakaway by name, because that is the only limit that
        # makes CREATE_BREAKAWAY_FROM_JOB legal.
        name, adopt = self._job_with(
            WIN_JOB_LIMIT_KILL_ON_JOB_CLOSE | WIN_JOB_LIMIT_BREAKAWAY_OK
        )
        report = self._classify_under(adopt, name)
        self.assertEqual(report["kind"], restoreguard.GUARDIAN_CONTEXT_ORDINARY)
        self.assertTrue(report["permits"])
        self.assertTrue(report["explicit"])
        self.assertFalse(report["silent"])
        self.assertTrue(report["can_escape"])
        self.assertTrue(
            report["requested_breakaway"],
            "BREAKAWAY_OK is what CREATE_BREAKAWAY_FROM_JOB requires",
        )
        self.assertTrue(report["child_launched"], report.get("child_error"))
        self.assertIs(
            report["child_in_this_job"],
            False,
            "the child has to land outside the job that would kill the owner",
        )

    def test_a_kill_on_close_job_with_silent_breakaway_permits_the_guardian(self):
        # The silent limit detaches children by itself.  Asking for
        # CREATE_BREAKAWAY_FROM_JOB on top is not required, and without
        # BREAKAWAY_OK it is refused - which used to turn a launch that could
        # have worked into a failure that then refused to park.
        name, adopt = self._job_with(
            WIN_JOB_LIMIT_KILL_ON_JOB_CLOSE | WIN_JOB_LIMIT_SILENT_BREAKAWAY_OK
        )
        report = self._classify_under(adopt, name)
        self.assertEqual(report["kind"], restoreguard.GUARDIAN_CONTEXT_ORDINARY)
        self.assertTrue(report["permits"])
        self.assertTrue(report["silent"])
        self.assertFalse(report["explicit"])
        self.assertTrue(report["can_escape"])
        self.assertFalse(
            report["requested_breakaway"],
            "a silent-breakaway job already releases children and forbids the flag",
        )
        self.assertTrue(report["child_launched"], report.get("child_error"))
        self.assertIs(
            report["child_in_this_job"],
            False,
            "silent breakaway has to happen by itself, with no creation flag",
        )

    def test_an_ordinary_job_still_permits_the_guardian(self):
        # Being in *some* job is how a supervised application normally runs; it
        # is not evidence that the job would kill the executor.
        import ctypes

        kernel32 = restoreguard._job_kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        self.assertTrue(handle)
        self.addCleanup(kernel32.CloseHandle, ctypes.c_void_p(handle))

        def adopt(child):
            return bool(kernel32.AssignProcessToJobObject(handle, ctypes.c_void_p(child._handle)))

        report = self._classify_under(adopt)
        self.assertEqual(report["kind"], restoreguard.GUARDIAN_CONTEXT_ORDINARY)
        self.assertTrue(report["permits"])
        self.assertFalse(
            report["flags"] & restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            "the ordinary job under test must not be a kill-on-close one",
        )
        self.assertFalse(report["requested_breakaway"])

    def test_the_classification_of_this_process_agrees_with_its_own_limits(self):
        # Whichever job the machine running the tests puts this process in, the
        # verdict has to follow the limits it actually reports - never the other
        # way round.
        context = restoreguard.enclosing_job()
        self.assertIn(context.kind, (
            restoreguard.GUARDIAN_CONTEXT_NONE,
            restoreguard.GUARDIAN_CONTEXT_ORDINARY,
            restoreguard.GUARDIAN_CONTEXT_HOSTILE,
            restoreguard.GUARDIAN_CONTEXT_UNREADABLE,
        ))
        if context.kind in (restoreguard.GUARDIAN_CONTEXT_NONE, restoreguard.GUARDIAN_CONTEXT_UNREADABLE):
            self.assertIsNone(context.limit_flags)
        else:
            flags = context.limit_flags
            kill_on_close = bool(flags & restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
            self.assertEqual(context.kind == restoreguard.GUARDIAN_CONTEXT_HOSTILE, kill_on_close and not context.can_escape)
            self.assertEqual(context.permits_guardian, not kill_on_close)

    def test_every_combination_of_limits_reaches_one_verdict(self):
        # The whole policy over every job limit bit that matters, checked as a
        # table instead of one remembered case: hostile exactly when the job
        # kills and cannot be left, and the creation flag exactly when the job
        # asked for breakaway by name.
        kill = restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        explicit = restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK
        silent = restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        for mask in range(8):
            flags = 0
            if mask & 1:
                flags |= kill
            if mask & 2:
                flags |= explicit
            if mask & 4:
                flags |= silent
            with self.subTest(flags=hex(flags)):
                context = restoreguard.EnclosingJob(
                    restoreguard._classify_job_flags(flags), flags
                )
                self.assertEqual(context.explicit_breakaway, bool(flags & explicit))
                self.assertEqual(context.silent_breakaway, bool(flags & silent))
                self.assertEqual(context.can_escape, bool(flags & (explicit | silent)))
                self.assertEqual(
                    context.kind == restoreguard.GUARDIAN_CONTEXT_HOSTILE,
                    bool(flags & kill) and not context.can_escape,
                )
                self.assertEqual(
                    context.permits_guardian, context.kind != restoreguard.GUARDIAN_CONTEXT_HOSTILE
                )
                requested = restoreguard._guardian_creation_flags(context)
                self.assertEqual(
                    bool(requested & restoreguard._CREATE_BREAKAWAY_FROM_JOB),
                    context.explicit_breakaway,
                    "the breakaway flag is requested only where Win32 honours it",
                )
                self.assertTrue(requested & restoreguard._CREATE_NO_WINDOW)

    def test_breakaway_is_requested_exactly_where_it_can_be_honoured(self):
        kill = restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        explicit = restoreguard._JOB_OBJECT_LIMIT_BREAKAWAY_OK
        silent = restoreguard._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        for context, expected in (
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_NONE, None), False),
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_ORDINARY, 0), False),
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_ORDINARY, explicit), True),
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_ORDINARY, silent), False),
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_ORDINARY, kill), False),
            (restoreguard.EnclosingJob(restoreguard.GUARDIAN_CONTEXT_HOSTILE, kill), False),
        ):
            with self.subTest(kind=context.kind, flags=context.limit_flags):
                requested = restoreguard._guardian_creation_flags(context)
                self.assertEqual(
                    bool(requested & restoreguard._CREATE_BREAKAWAY_FROM_JOB), expected
                )

    def test_a_hostile_job_refuses_the_launch_without_creating_anything(self):
        hostile = restoreguard.EnclosingJob(
            restoreguard.GUARDIAN_CONTEXT_HOSTILE,
            restoreguard._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        )
        with (
            patch.object(restoreguard, "enclosing_job", return_value=hostile),
            patch.object(restoreguard.subprocess, "Popen") as popen,
        ):
            self.assertIsNone(restoreguard._spawn(["anything"], timeout=1.0, expect=b"ready"))
        popen.assert_not_called()


class SmokeGateTests(unittest.TestCase):
    """Smoke coverage: every scenario in the smoke has to assert something real."""

    def test_every_scenario_has_a_handover_or_restart_gate(self):
        for scenario in ("responsive", "slow", "inflight", "hardkill", "aged", "badjournal",
                         "jobfail", "journalrace", "guardianlimit", "ownerpidreuse",
                         "badlease", "claimaba", "monitorgap", "outerjob"):
            self.assertIn(f'"{scenario}"', SMOKE)

    def test_outerjob_gate_uses_a_real_no_breakaway_job(self):
        body = SMOKE.split("def scenario_outerjob", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_GuardianLaunchJob", body)
        self.assertIn("outer.adopt(helper)", body)
        self.assertIn("restoreguard._spawn", body)
        self.assertIn("result.get(\"accepted\")", body)
        self.assertIn(
            "job_context",
            body,
            "the gate has to prove it ran under the hostile job it set up",
        )

    def test_a_failed_scenario_never_reports_a_pass_for_its_cleanup(self):
        runner = SMOKE.split("def run_all", 1)[1].split("\ndef ", 1)[0]
        failed_branch = runner.split("FAILED", 1)[1]
        self.assertIn("wait_for_executor_exit(args)", failed_branch)
        self.assertNotIn(
            "run_tail(",
            failed_branch,
            "the cleanup after a FAILED scenario must not print a PASS of its own",
        )
        self.assertEqual(
            SMOKE.count('log("PASS")'),
            1,
            "only the pass of a scenario itself may be reported as a pass",
        )
        self.assertIn("log(\"PASS\")", SMOKE.split("def run_tail", 1)[1].split("\ndef ", 1)[0])

    def test_the_lifetime_gate_watches_an_executor_that_never_gives_up(self):
        # The regression this gate exists for: an executor that reached a lifetime
        # limit while a window was still parked off-screen.
        body = SMOKE.split("def scenario_guardianlimit", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("guardian_fast_backoff", body)
        self.assertIn("guardian.poll() is not None", body)
        self.assertIn("abandoned a live obligation", body)
        self.assertIn("journal_has_hwnd", body)
        self.assertIn("assert_restored", body)
        self.assertLess(
            body.index("guardian.poll() is not None"),
            body.index("assert_restored"),
            "the gate must watch the guardian before it waits for the restore",
        )

    def test_the_pid_reuse_gate_plants_a_live_pid_with_a_foreign_creation_time(self):
        body = SMOKE.split("def scenario_ownerpidreuse", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("plant_owner_identity", body)
        self.assertIn("owner_pid=os.getpid()", body)
        self.assertIn("owner_created=1", body)

    def test_the_bad_lease_gate_plants_non_finite_leases(self):
        body = SMOKE.split("def scenario_badlease", 1)[1].split("\ndef ", 1)[0]
        for literal in ("Infinity", "NaN", "-Infinity"):
            self.assertIn(literal, body)
        self.assertIn("plant_claim", body)
        self.assertIn("assert_restored", body)

    def test_the_aba_gate_makes_a_stale_executor_mutate_a_newer_claim(self):
        body = SMOKE.split("def scenario_claimaba", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("plant_stale_claim", body)
        self.assertIn("clear_claimed(stale)", body)
        self.assertIn("release(stale)", body)
        self.assertIn("journal_has_hwnd", body)

    def test_the_monitor_gap_gate_uses_the_per_monitor_predicate(self):
        body = SMOKE.split("def scenario_monitorgap", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("display_monitor_rects", body)
        self.assertIn("screen.bounding_rect", body)
        self.assertIn("is_visible_on_monitors(gap, monitors)", body)
        self.assertIn("assert_restored", body)
        self.assertIn("import screen", SMOKE)

    def test_the_slow_scenario_requires_a_restart_free_restore(self):
        slow = SMOKE.split("def scenario_slow", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("require_guardian_handover", slow)
        self.assertNotIn("recover_on_restart", slow)

    def test_the_inflight_scenario_requires_a_restart_free_restore(self):
        inflight = SMOKE.split("def scenario_inflight", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("require_guardian_handover", inflight)
        self.assertNotIn("recover_on_restart", inflight)

    def test_the_handover_gate_watches_for_a_live_guardian(self):
        gate = SMOKE.split("def require_guardian_handover", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("guardian_pids", gate)
        self.assertIn("assert_restored", gate)
        self.assertIn("the guardian to drain the recovery journal", gate)
        self.assertIn("the application to hand its unfinished restore over", gate)

    def test_the_handover_gate_proves_the_handover_in_the_application_log(self):
        # The record may already be gone by the time the process has finished
        # leaving main, so the gate reads the handover the app itself recorded.
        gate = SMOKE.split("def require_guardian_handover", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("app_log_path()", gate)
        self.assertIn("log_lines_since", gate)

    def test_guardians_are_watched_before_the_quit_request(self):
        for scenario in ("scenario_slow", "scenario_inflight"):
            body = SMOKE.split(f"def {scenario}", 1)[1].split("\ndef ", 1)[0]
            self.assertLess(body.index("watcher.start()"), body.index("first.request_quit()"))

    def test_the_aged_scenario_ages_the_record_and_restarts(self):
        aged = SMOKE.split("def scenario_aged", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("age_journal_records", aged)
        self.assertIn("recover_on_restart", aged)

    def test_the_badjournal_scenario_plants_damage_before_every_start(self):
        bad = SMOKE.split("def scenario_badjournal", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("broken_journal_documents", bad)
        self.assertIn(".invalid", bad)
        self.assertLess(bad.index("write_text"), bad.index("first.start()"))

    def test_the_jobfail_scenario_requires_that_no_helper_is_started(self):
        jobfail = SMOKE.split("def scenario_jobfail", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("helper_pids", jobfail)
        self.assertIn("raise SmokeError", jobfail)

    def test_the_jobfail_hook_is_passed_only_to_that_scenario(self):
        self.assertIn("LOOKUPWINDOWS_TEST_HOOKS", SMOKE)
        self.assertIn("environment.pop(\"LOOKUPWINDOWS_TEST_HOOKS\"", SMOKE)

    def test_the_journalrace_scenario_races_a_guardian_and_a_second_writer(self):
        # Journal race: the journal is shared by the application and the
        # guardian, so the release has to prove that overlapping writers keep
        # every obligation.
        race = SMOKE.split("def scenario_journalrace", 1)[1].split("\nSCENARIOS = {", 1)[0]
        self.assertIn("require_guardian_working", race)
        self.assertIn("assert_journal_retains_obligations", race)
        # A second *process* has to write into the journal the guardian is working
        # on, and its own obligation must be proven to be in the document.
        self.assertIn("spawn_journal_writer", race)
        self.assertIn("{target.hwnd, race_target.hwnd} - outstanding", race)
        self.assertIn("assert_restored(target.hwnd", race)
        self.assertIn("the journal to drain after the overlapping writers finished", race)
        self.assertIn("require_no_guardian", race)

    def test_the_journal_writer_is_a_real_separate_process(self):
        writer = SMOKE.split("def spawn_journal_writer", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("subprocess.run", writer)
        self.assertIn('sys.executable, "-c", script', writer)
        # It has to perform the real operations, not just touch the file.
        self.assertIn("park_window_offscreen_sync", writer)
        self.assertIn("record_from_state", writer)
        self.assertIn("RecoveryJournal", writer)

    def test_the_journal_race_does_not_depend_on_a_timing_coincidence(self):
        # The second writer parks its window while the application is still
        # running, and the application only quits afterwards: both records are
        # therefore written before the handover, deterministically.
        race = SMOKE.split("def scenario_journalrace", 1)[1].split("\nSCENARIOS = {", 1)[0]
        self.assertLess(race.index("spawn_journal_writer"), race.index("ensure_park"))
        self.assertLess(race.index("ensure_park"), race.index("require_guardian_working"))
        self.assertIn("an obligation written by another process was erased", race)

    def test_the_guardian_must_not_reuse_the_parents_frozen_extraction(self):
        # A frozen child pointed at the parent's _MEIPASS keeps that directory
        # open.  The guardian outlives the application, so the application's
        # launcher could no longer delete the extraction directory and reported a
        # modal failure instead of exiting - which left a dialog on screen and a
        # process that never exited.
        self.assertIn("def guardian_environment", GUARD)
        spawn = GUARD.split("def _spawn", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("guardian_environment()", spawn)
        self.assertIn("env=env", spawn)
        environment = GUARD.split("def guardian_environment", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('key.startswith("_PYI_")', environment)
        self.assertIn('key.startswith("_MEIPASS")', environment)
        # A source run must inherit its environment unchanged.
        self.assertIn('if not getattr(sys, "frozen", False):', environment)

    def test_the_window_lookups_are_scoped_to_the_process_that_owns_them(self):
        # Two instances of the same executable register the same window classes,
        # so a class-only lookup would drive the wrong application.
        self.assertIn("def find_window_of", SMOKE)
        self.assertIn('lambda: find_window_of("WPCtrl", pid)', SMOKE)
        self.assertIn('lambda: find_window_of("WPCard", pid)', SMOKE)
        # A onefile build runs a bootloader plus the real application process,
        # and only the child owns the windows.
        self.assertIn("def resolve_app_pid", SMOKE)

    def test_the_journalrace_gate_checks_the_guardian_while_the_second_app_writes(self):
        gate = SMOKE.split("def require_guardian_working", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("recovery guardian working", gate)
        self.assertIn("no recovery guardian process was observed", gate)
        self.assertIn("assert_journal_retains_obligations", gate)
        self.assertNotIn("the guardian to drain the recovery journal", gate)

    def test_the_retention_gate_only_accepts_a_discharged_obligation(self):
        gate = SMOKE.split("def assert_journal_retains_obligations", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_effectively_onscreen", gate)
        self.assertIn("still parked but the recovery journal lost its record", gate)


if __name__ == "__main__":
    unittest.main()