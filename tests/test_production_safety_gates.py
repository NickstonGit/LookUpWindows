"""Behavioural regressions for the production recovery path (R01-R11).

Each class executes the failure it was written for instead of grepping for a name:
the cases are all of the shape "a mutation, a failed restore or a failed query
was read as a completed recovery", and only real behaviour can prove that the
reading is gone.  Each case carries an R-number so a failure names its own
section, and the series continues in test_recovery_ownership.py (R12-R14).

    R01  a journal mutation may not erase a partly damaged document's obligation
    R02  a failed restore is not a completed recovery
    R03  the executor does not walk away from unresolved damage
    R04  more than three failed guardian launches do not end the supervision
    R05  an exception after the move keeps the recovered state
    R06  no blocking recovery work runs on the UI thread, and there is no fallback
    R07  an unanswered identity query is UNKNOWN, never REUSED
    R08  a parked window stays identifiable after the monitor layout changes
    R09  a park waits for a confirmed executor, and the record survives a crash
    R10  a published checksum describes the bytes that are actually shipped
    R11  the release gate runs the scenario registry itself
"""

from __future__ import annotations

import ast
import ctypes
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import recovery  # noqa: E402  (after source-path bootstrap)
import restoreguard  # noqa: E402
from recovery import (  # noqa: E402
    DAMAGED_JOURNAL_STATUSES,
    JOURNAL_STATUS_DEGRADED,
    JOURNAL_VERSION,
    ParkRecord,
    RecoveryJournal,
    RecoverySnapshot,
)

try:
    import winapi  # noqa: E402
except Exception:  # pragma: no cover - non-Windows
    winapi = None

APP_SOURCE = (SRC / "app.py").read_text(encoding="utf-8")
OWNER_RUN_ID = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


def make_record(hwnd: int = 4242, **overrides) -> ParkRecord:
    values = {
        "hwnd": hwnd,
        "pid": 1234,
        "class_name": "SampleWindow",
        "process_name": "a.exe",
        "process_created": 999,
        "screen_rect": (10, 10, 310, 210),
        "show_cmd": 1,
        "placement_flags": 0,
        "min_position": (-1, -1),
        "max_position": (-1, -1),
        "normal_position": (10, 10, 310, 210),
        "owner_pid": 77,
        "owner_run_id": OWNER_RUN_ID,
        "owner_created": 555,
        "recorded_at": time.time(),
        "label": "a.exe - sample",
        "state": "parked",
        "operation_id": "op" + f"{hwnd:028d}",
        "park_rect": (-299, -199, 1, 1),
        "park_origin": (0, 0),
    }
    values.update(overrides)
    return ParkRecord(**values)


def executor(tag: str = "a") -> recovery.ExecutorIdentity:
    return recovery.ExecutorIdentity(
        executor_id=f"exec-{tag}", pid=1000 + sum(map(ord, tag)), created=42, label=tag
    )


class SweepResult:
    """The four answers a sweep owes the executor, and nothing else."""

    def __init__(self, recovered=(), pending=(), unknown=(), complete=True):
        self.recovered = tuple(recovered)
        self.pending = tuple(pending)
        self.unknown = tuple(unknown)
        self.complete = bool(complete)

    @property
    def decided(self) -> bool:
        return self.complete and not self.pending and not self.unknown

    def describe(self) -> str:
        return f"pending={list(self.pending)} complete={self.complete}"


class FakeWinapi:
    """A scriptable Win32 layer for the executor."""

    def __init__(self, *, restores=True, complete=True, parked=True, onscreen=False):
        self.restores = restores
        self.complete = complete
        self.parked = parked
        self.onscreen = onscreen
        self.swept: list[set] = []
        self.pid = 1234
        self.class_name = "SampleWindow"
        self.created = 999
        self.alive = True
        self.restore_calls = 0

    def is_window(self, hwnd):
        return self.alive

    def get_pid(self, hwnd):
        return self.pid

    def get_class_name(self, hwnd):
        return self.class_name

    def _query_process_identity(self, pid, query_name=True):
        return SimpleNamespace(created=self.created, name="a.exe", access_denied=False)

    def looks_like_lookup_parked(self, hwnd):
        return self.parked

    def is_effectively_onscreen(self, hwnd, min_visible=24):
        return self.onscreen

    def state_from_record(self, record):
        return ("fake-parked-state", int(record.hwnd))

    def window_matches_parked_state(self, hwnd, state):
        return self.alive and state[1] == hwnd

    def restore_parked_window_sync(self, hwnd, state):
        self.restore_calls += 1
        if self.restores:
            self.parked = False
            self.onscreen = True
        return self.restores

    def recover_orphaned_lookup_park(self, hwnd, **kwargs):
        if not self.restores or not self.parked:
            return False
        self.parked = False
        self.onscreen = True
        return True

    def recover_all_orphaned_parks(self, exclude=()):
        self.swept.append(set(exclude))
        if not self.parked:
            return SweepResult(complete=self.complete)
        if not self.restores:
            return SweepResult(pending=(1234,), complete=self.complete)
        self.parked = False
        self.onscreen = True
        return SweepResult(recovered=(1234,), complete=self.complete)


class AppMethod:
    """Compile one ``App`` method on its own, the way the suite already does."""

    def __init__(self, method_name: str, namespace: dict, class_name: str = "App"):
        tree = ast.parse(APP_SOURCE)
        cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        node = next(
            item
            for item in cls.body
            if isinstance(item, ast.FunctionDef) and item.name == method_name
        )
        module = ast.Module(
            body=[
                ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                node,
            ],
            type_ignores=[],
        )
        self.namespace = namespace
        exec(compile(ast.fix_missing_locations(module), "app.py", "exec"), namespace)
        self.function = namespace[method_name]

    def __call__(self, *args, **kwargs):
        return self.function(*args, **kwargs)


class JournalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)

    def plant_broken_entry(self, hwnd: int = 102, *, drop=("normalPosition",)) -> dict:
        """Write a document with one good record and one unusable entry."""
        self.journal.record_intent(make_record(101))
        self.journal.mark_parked(101)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        broken = make_record(hwnd).to_dict()
        for field in drop:
            broken.pop(field, None)
        document["records"].append(broken)
        self.path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return broken


# --------------------------------------------------------------------------- #
# R01 - a mutation may not erase a partly damaged document's obligation
# --------------------------------------------------------------------------- #
class DurableSalvageTests(JournalCase):
    def test_claiming_an_unrelated_record_keeps_the_broken_one_on_disk(self):
        self.plant_broken_entry()
        before = RecoveryJournal(self.path).snapshot()
        self.assertEqual(before.status, JOURNAL_STATUS_DEGRADED)
        self.assertEqual([record.hwnd for record in before.unresolved], [102])

        # The failing sequence: claim a *good* record, then read again.
        claimer = RecoveryJournal(self.path)
        claim = claimer.claim(101, executor("claimer"))
        self.assertIsNotNone(claim)
        self.assertTrue(claimer.renew(claim))
        claimer.release_all(executor("claimer"))

        after = RecoveryJournal(self.path).snapshot()
        self.assertEqual([record.hwnd for record in after.records], [101])
        self.assertEqual([record.hwnd for record in after.unresolved], [102])
        self.assertTrue(after.damaged)
        self.assertFalse(after.proven_complete)
        # The damage marker is durable, so the next process still sees the work.
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(document.get("damaged"))
        self.assertTrue(document["damage"])

    def test_clearing_the_good_record_does_not_clear_the_damage(self):
        self.plant_broken_entry()
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record(101))
        self.assertTrue(journal.clear(101))
        after = RecoveryJournal(self.path).snapshot()
        self.assertEqual(after.records, ())
        self.assertEqual([record.hwnd for record in after.unresolved], [102])
        self.assertTrue(after.damaged)

    def test_the_whole_lifecycle_of_an_unrelated_record_keeps_the_obligation(self):
        # claim -> renew -> clear of a good record, then a *new* reader and a
        # new process: the broken window must still be there to be recovered.
        self.plant_broken_entry()
        first = RecoveryJournal(self.path)
        claim = first.claim(101, executor("claimer"))
        self.assertIsNotNone(claim)
        self.assertTrue(first.renew(claim))
        self.assertTrue(first.clear_claimed(claim))
        survivor = RecoveryJournal(self.path).snapshot()
        self.assertEqual(survivor.outstanding_hwnds(), (102,))

    def test_a_preserved_entry_is_writable(self):
        # A preserved entry that cannot be written back would freeze the journal -
        # every later claim of every record refused - which is worse than losing it.
        self.plant_broken_entry()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["records"][1]["claimUntil"] = float("nan")
        self.path.write_text(json.dumps(document), encoding="utf-8")
        journal = RecoveryJournal(self.path)
        self.assertIsNotNone(journal.claim(101, executor("c")))
        self.assertTrue(RecoveryJournal(self.path).snapshot().damaged)

    def test_an_acknowledgement_is_durable_and_renames_the_quarantined_document(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        self.assertTrue(Path(str(self.path) + ".invalid").exists())
        self.assertTrue(journal.acknowledge_damage())
        # A different reader no longer sees unfinished work...
        self.assertFalse(RecoveryJournal(self.path).snapshot().damaged)
        # ... while the damaged document is still there as a trace.
        self.assertTrue(Path(str(self.path) + ".invalid.swept").exists())

    def test_a_failed_acknowledgement_leaves_the_damage_exactly_where_it_was(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        with patch.object(recovery.RecoveryJournal, "_write", return_value=False):
            self.assertFalse(journal.acknowledge_damage())
        self.assertIn(RecoveryJournal(self.path).snapshot().status, DAMAGED_JOURNAL_STATUSES)


class SnapshotContractTests(JournalCase):
    """One answer about what a journal owes, and one predicate for "finished"."""

    def test_records_salvaged_entries_and_damage_are_a_single_answer(self):
        self.plant_broken_entry()
        snapshot = RecoveryJournal(self.path).snapshot()
        self.assertEqual([record.hwnd for record in snapshot.records], [101])
        self.assertEqual([record.hwnd for record in snapshot.unresolved], [102])
        self.assertEqual(snapshot.outstanding_hwnds(), (101, 102))
        self.assertTrue(snapshot.damaged)
        self.assertFalse(snapshot.proven_complete)

    def test_only_a_complete_journal_is_proven_finished(self):
        empty = RecoverySnapshot()
        self.assertTrue(empty.proven_complete)
        self.assertTrue(RecoverySnapshot(records=(make_record(),)).proven_complete is False)
        damaged = RecoverySnapshot(status=JOURNAL_STATUS_DEGRADED, damage="x")
        self.assertFalse(damaged.proven_complete)
        self.assertTrue(damaged.damaged)

    def test_every_exit_path_asks_the_snapshot(self):
        guard = (SRC / "restoreguard.py").read_text(encoding="utf-8")
        self.assertIn("snapshot = self.journal.snapshot()", guard)
        for path in ("_pass", "_hand_off_on_stop"):
            block = guard.split(f"    def {path}", 1)[1].split("\n    def ", 1)[0]
            self.assertIn("snapshot()", block, f"{path} decides on a bare record list")
        self.assertIn("journal.snapshot().proven_complete", guard)

    def test_the_application_handover_decides_on_the_snapshot(self):
        handover = APP_SOURCE.split("    def _handover_unfinished_restores", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("snapshot = self.recovery_journal.snapshot()", handover)
        self.assertIn("if not outstanding and not snapshot.damaged:", handover)
        supervisor = APP_SOURCE.split("    def _recovery_supervisor_worker", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self.recovery_journal.snapshot()", supervisor)


# --------------------------------------------------------------------------- #
# R02 - a failed restore is not a completed recovery
# --------------------------------------------------------------------------- #
class SweepDecisionTests(JournalCase):
    def damage(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        return journal

    def test_a_restore_that_fails_keeps_the_journal_damaged(self):
        journal = self.damage()
        layer = FakeWinapi(restores=False)
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertFalse(result.decided)
        self.assertEqual(result.outstanding_hwnds, (1234,))
        self.assertTrue(RecoveryJournal(self.path).snapshot().damaged)

    def test_an_unenumerable_desktop_decides_nothing(self):
        journal = self.damage()
        layer = FakeWinapi(restores=True, complete=False)
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertFalse(result.decided)
        self.assertTrue(RecoveryJournal(self.path).snapshot().damaged)

    def test_a_sweep_that_cannot_run_at_all_decides_nothing(self):
        journal = self.damage()
        layer = FakeWinapi()
        layer.recover_all_orphaned_parks = None
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertFalse(result.decided)
        self.assertIn("cannot sweep", result.reason)
        self.assertTrue(journal.damaged)

    def test_a_sweep_that_raises_decides_nothing(self):
        journal = self.damage()
        layer = FakeWinapi()
        layer.recover_all_orphaned_parks = Mock(side_effect=PermissionError("no desktop access"))
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertFalse(result.decided)
        self.assertTrue(journal.damaged)

    def test_a_window_of_unknown_state_keeps_the_journal_damaged(self):
        journal = self.damage()
        layer = FakeWinapi(restores=True)
        layer.recover_all_orphaned_parks = lambda exclude=(): SweepResult(unknown=(1234,))
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertFalse(result.decided)
        self.assertEqual(result.outstanding_hwnds, (1234,))

    def test_only_a_complete_sweep_that_restored_everything_lifts_the_damage(self):
        journal = self.damage()
        layer = FakeWinapi(restores=True)
        result = restoreguard.resolve_journal_damage(journal, module=layer)
        self.assertTrue(result.decided)
        self.assertEqual(list(result.recovered), [1234])
        fresh = RecoveryJournal(self.path).snapshot()
        self.assertFalse(fresh.damaged)
        self.assertTrue(fresh.proven_complete)

    def test_the_winapi_sweep_reports_pending_and_unknown_instead_of_nothing(self):
        if winapi is None:  # pragma: no cover - non-Windows
            self.skipTest("winapi requires Windows")
        # The geometry: one parked window whose restore keeps failing, and a
        # desktop that cannot be enumerated.
        parked = SimpleNamespace(parked=True, onscreen=False, own_pid=0)
        with patch.object(winapi, "enumerate_recovery_windows", return_value=((77,), True)), patch.object(
            winapi, "is_window", return_value=True
        ), patch.object(winapi, "get_pid", return_value=4321), patch.object(
            winapi, "query_window_rect", return_value=(-299, -199, 1, 1)
        ), patch.object(
            winapi, "is_stranded_park", side_effect=lambda hwnd, **kwargs: bool(parked.parked)
        ), patch.object(
            winapi, "recover_orphaned_lookup_park", return_value=False
        ), patch.object(
            winapi, "looks_like_lookup_parked", return_value=bool(parked.parked)
        ):
            result = winapi.recover_all_orphaned_parks()
        self.assertEqual(result.recovered, ())
        self.assertEqual(result.pending, (77,))
        self.assertTrue(result.complete)
        self.assertFalse(result.decided)
        self.assertIn("pending", result.describe())

    def test_a_failed_enumeration_is_not_an_empty_desktop(self):
        if winapi is None:  # pragma: no cover - non-Windows
            self.skipTest("winapi requires Windows")
        with patch.object(winapi, "enumerate_recovery_windows", return_value=((), False)):
            result = winapi.recover_all_orphaned_parks()
        self.assertFalse(result.complete)
        self.assertFalse(result.decided)


# --------------------------------------------------------------------------- #
# R03 - the executor does not walk away from unresolved damage
# --------------------------------------------------------------------------- #
class GuardianExitTests(JournalCase):
    def guardian(self, journal):
        return restoreguard._Guardian(
            journal,
            executor=executor("g"),
            singleton=restoreguard._SingletonLock(None),
        )

    def test_an_unresolved_journal_keeps_the_executor_past_the_idle_deadline(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        idle = restoreguard.GUARDIAN_IDLE_EXIT_SEC
        restoreguard.GUARDIAN_IDLE_EXIT_SEC = 0.0
        self.addCleanup(setattr, restoreguard, "GUARDIAN_IDLE_EXIT_SEC", idle)
        guardian = self.guardian(journal)
        # The sweep cannot succeed in this layer, so the damage is never resolved.
        guardian._handle_damage = lambda: None
        result: list = []
        worker = threading.Thread(
            target=lambda: result.append(guardian._pass()), name="guardian-pass", daemon=True
        )
        worker.start()
        worker.join(timeout=2.0)
        still_working = worker.is_alive()
        guardian.stop.set()
        worker.join(timeout=5.0)
        self.assertTrue(still_working, "the executor exited with damage outstanding")
        self.assertIn(journal.status, DAMAGED_JOURNAL_STATUSES)

    def test_the_executor_exits_once_the_damage_is_actually_resolved(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        layer = FakeWinapi(restores=True)
        idle = restoreguard.GUARDIAN_IDLE_EXIT_SEC
        restoreguard.GUARDIAN_IDLE_EXIT_SEC = 0.0
        self.addCleanup(setattr, restoreguard, "GUARDIAN_IDLE_EXIT_SEC", idle)
        with patch.object(restoreguard, "winapi", lambda: layer), patch.object(
            restoreguard, "POLL_SEC", 0.01
        ):
            outcome = restoreguard.run_guardian(
                journal.path, announce=False, executor=executor("g")
            )
        self.assertEqual(outcome, 0)
        self.assertEqual(len(layer.swept), 1)

    def test_a_stop_request_does_not_end_unresolved_damage(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        guardian = self.guardian(journal)
        guardian.handover = Mock(return_value=False)
        guardian._workers = {}
        # There are no live records, so only the damage is left: stopping here would
        # end the only executor that was still sweeping.
        self.assertFalse(guardian._hand_off_on_stop())
        guardian.handover.assert_called_once()

    def test_a_stop_request_is_honoured_once_nothing_is_left(self):
        journal = self.journal
        self.assertTrue(self.guardian(journal)._hand_off_on_stop())

    def test_the_handover_helper_counts_damage_as_outstanding(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.reload()
        started = time.monotonic()
        self.assertFalse(restoreguard.wait_for_handover(journal, 0.2))
        self.assertLess(time.monotonic() - started, 5.0)


# --------------------------------------------------------------------------- #
# R04 - the supervision does not end after three failed launches
# --------------------------------------------------------------------------- #
class SupervisorTests(JournalCase):
    def app(self, **overrides):
        import threading as _threading

        values = {
            "recovery_journal": self.journal,
            "_executor": executor("a"),
            "_shutting_down": False,
            "_recovery_guardian": None,
            "_recovery_supervisor": None,
            "_recovery_supervisor_stop": _threading.Event(),
            "_recovery_supervisor_wanted": _threading.Event(),
            "_recovery_lock": _threading.Lock(),
            "_guardian_start_lock": _threading.Lock(),
            "logger": Mock(),
            "_start_session_guardian": lambda: False,
            "_supervisor_has_live_executor": lambda: False,
            "restoreguard": SimpleNamespace(
                resolve_all=Mock(),
                resolve_journal_damage=Mock(return_value=SimpleNamespace(recovered=())),
            ),
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def supervisor(self, layer=None):
        return AppMethod(
            "_recovery_supervisor_worker",
            {
                "logger": Mock(),
                "time": time,
                "SUPERVISOR_IDLE_POLL_SEC": 0.01,
                "SUPERVISOR_POLL_CEILING_SEC": 0.01,
                "STARTUP_RECOVERY_BUDGET_SEC": 0.05,
                "restoreguard": layer or SimpleNamespace(
                    resolve_all=Mock(),
                    resolve_journal_damage=Mock(return_value=SimpleNamespace(recovered=())),
                ),
            },
        )

    def run_supervisor_until(self, app, predicate, timeout=5.0):
        stop = app._recovery_supervisor_stop
        done = threading.Event()

        def run():
            try:
                self.supervisor(app.restoreguard)(app)
            finally:
                done.set()

        worker = threading.Thread(target=run, name="supervisor", daemon=True)
        worker.start()
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)
        stop.set()
        worker.join(timeout=5.0)
        return worker.is_alive() or done.is_set()

    def test_more_than_three_failed_launches_still_leave_a_live_executor(self):
        self.journal.record_intent(make_record(123))
        attempts = []

        def guardian():
            attempts.append(1)
            return len(attempts) > 3

        app = self.app(_start_session_guardian=guardian)
        self.run_supervisor_until(app, lambda: len(attempts) > 3)
        self.assertGreater(len(attempts), 3, "the supervisor gave up after its old limit")
        self.assertEqual([record.hwnd for record in RecoveryJournal(self.path).reload()], [123])

    def test_it_executes_the_obligations_itself_when_no_guardian_can_start(self):
        self.journal.record_intent(make_record(123))
        resolve_all = Mock()
        app = self.app(
            restoreguard=SimpleNamespace(
                resolve_all=resolve_all,
                resolve_journal_damage=Mock(return_value=SimpleNamespace(recovered=())),
            )
        )
        self.run_supervisor_until(app, lambda: resolve_all.call_count > 0)
        self.assertGreater(resolve_all.call_count, 0, "nobody executed the record")

    def test_an_unreadable_journal_never_ends_the_supervision(self):
        self.journal.record_intent(make_record(123))
        app = self.app()
        real_snapshot = self.journal.snapshot
        calls = []

        def transient_read():
            calls.append(1)
            if len(calls) <= 4:
                raise recovery.JournalReadError("busy")
            return real_snapshot()

        with patch.object(self.journal, "snapshot", side_effect=transient_read):
            self.run_supervisor_until(app, lambda: len(calls) > 4)
        self.assertGreater(len(calls), 4)
        self.assertTrue(app.restoreguard.resolve_all.called)

    def test_the_supervisor_replaces_a_dead_guardian(self):
        self.journal.record_intent(make_record(1))
        app = self.app(_recovery_guardian=SimpleNamespace(pid=1, exit_code=1))
        started = AppMethod(
            "_start_session_guardian",
            {
                "logger": Mock(),
                "RESTORE_HANDOVER_TIMEOUT_SEC": 1,
                "restoreguard": SimpleNamespace(spawn_guardian=Mock(return_value=SimpleNamespace(pid=2))),
            },
        )
        self.assertTrue(started(app))
        self.assertEqual(app._recovery_guardian.pid, 2)


# --------------------------------------------------------------------------- #
# R05 - an exception after the move keeps the recovery state
# --------------------------------------------------------------------------- #
class PostParkStateTests(unittest.TestCase):
    def app(self, **overrides):
        values = {
            "_shutting_down": False,
            "_executor": executor("a"),
            "_await_session_executor": Mock(return_value=True),
            "_journal_record_intent": Mock(return_value=make_record(4242)),
            "_journal_commit_park": Mock(),
            "_journal_drop": Mock(),
            "_recover_abandoned_park": Mock(),
            "_finish_park_source": Mock(),
            "_ensure_recovery_supervisor": Mock(),
            "_notify_park_refused": Mock(),
            "logger": Mock(),
            "defer": lambda fn, *args: fn(*args),
            "cards": [],
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def card(self):
        return SimpleNamespace(
            hwnd=1, src_hwnd=4242, _title="a.exe", _source_action_id=1,
            _source_action_pending="park", _invalidate=Mock(),
            _parked_source=None, _parked_seen_not_foreground=False,
            _minimized=False, _active=False, _placeholder="",
            set_changed=Mock(), update_thumb_geometry=Mock(),
        )

    def park_worker(self):
        return AppMethod("_park_source_worker", {"winapi": winapi, "logger": Mock(), "time": time})

    @unittest.skipIf(winapi is None, "winapi requires Windows")
    def test_an_exception_after_the_move_keeps_the_state_and_registers_recovery(self):
        state = SimpleNamespace(pid=1, class_name="C", process_name="p",
                                process_created=1, placement=None)

        def park(hwnd, *, before_park=None):
            before_park(state)
            raise OSError("SetWindowPos failed after the move")

        app = self.app()
        card = self.card()
        with patch.object(winapi, "park_window_offscreen_sync", side_effect=park):
            self.park_worker()(app, card, 4242, 1)
        # The abandoned window is handed to the recovery path instead of being
        # reported as "never parked".
        self.assertTrue(app._recover_abandoned_park.called)
        self.assertIs(app._recover_abandoned_park.call_args.args[1], state)
        self.assertIs(app._finish_park_source.call_args.args[3], state)

    @unittest.skipIf(winapi is None, "winapi requires Windows")
    def test_a_park_without_a_confirmed_executor_never_moves_the_window(self):
        app = self.app(_await_session_executor=Mock(return_value=False))
        card = self.card()
        with patch.object(winapi, "park_window_offscreen_sync") as park:
            self.park_worker()(app, card, 4242, 1)
        park.assert_not_called()
        app._notify_park_refused.assert_called_once()

    @unittest.skipIf(winapi is None, "winapi requires Windows")
    def test_a_successful_park_is_not_immediately_restored(self):
        state = SimpleNamespace(pid=1)
        app = self.app()
        card = self.card()
        app.cards.append(card)

        def park(hwnd, *, before_park):
            self.assertTrue(before_park(state))
            return state

        with patch.object(winapi, "park_window_offscreen_sync", side_effect=park):
            self.park_worker()(app, card, 4242, 1)
        app._recover_abandoned_park.assert_not_called()
        self.assertIs(app._finish_park_source.call_args.args[3], state)

    @unittest.skipIf(winapi is None, "winapi requires Windows")
    def test_a_commit_exception_keeps_the_captured_state(self):
        state = SimpleNamespace(pid=1)
        app = self.app(_journal_commit_park=Mock(side_effect=OSError("disk failure")))
        card = self.card()
        app.cards.append(card)

        def park(hwnd, *, before_park):
            self.assertTrue(before_park(state))
            return state

        with patch.object(winapi, "park_window_offscreen_sync", side_effect=park):
            self.park_worker()(app, card, 4242, 1)
        self.assertIs(app._recover_abandoned_park.call_args.args[1], state)
        self.assertIs(app._finish_park_source.call_args.args[3], state)
        app._journal_drop.assert_not_called()

    def test_the_state_is_captured_by_the_recovery_hook_itself(self):
        # The hook is the only place that knows the obligation exists, so it is the
        # only place that may capture the state - not the caller after the fact.
        source = APP_SOURCE.split("    def _park_source_worker", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("def before_park", source)
        self.assertIn("state = parked", source)
        self.assertIn("intent_recorded", source)
        # ... and the general handler must not erase it again.
        handler = source.split("except Exception:", 1)[1]
        self.assertNotIn("state = None", handler)


# --------------------------------------------------------------------------- #
# R06 - no blocking recovery work on the UI thread
# --------------------------------------------------------------------------- #
class ShutdownBoundaryTests(unittest.TestCase):
    def test_quit_only_publishes_the_request(self):
        quit_block = APP_SOURCE.split("    def quit(self)", 1)[1].split("\n    def ", 1)[0]
        for forbidden in (
            "_handover_unfinished_restores(",
            "_teardown_services(",
            "_restore_sources_for_shutdown(",
            "detector.close(",
            "_config_saver.flush(",
            "self._finish_shutdown(",
            "wait_for_handover(",
        ):
            self.assertNotIn(forbidden, quit_block, f"{forbidden} runs on the UI thread")
        self.assertIn("self._shutdown_requested.set()", quit_block)
        self.assertIn("_start_shutdown_worker(wait=False)", quit_block)

    def test_the_shutdown_worker_exists_before_anyone_asks_to_exit(self):
        start = APP_SOURCE.split("    def start(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_start_shutdown_worker(wait=False)", start)
        launcher = APP_SOURCE.split("    def _start_shutdown_worker", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("LookUpWindows-Shutdown", launcher)

    def test_a_missing_worker_refuses_the_exit_instead_of_working_inline(self):
        quit_block = APP_SOURCE.split("    def quit(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_notify_shutdown_unavailable()", quit_block)
        # The refusal happens before the application is marked as closing, so the
        # obligations stay owned by a live, working process.
        self.assertLess(
            quit_block.index("_notify_shutdown_unavailable()"),
            quit_block.index("self._shutting_down = True"),
        )

    def test_the_shutdown_worker_does_the_blocking_halves(self):
        worker = APP_SOURCE.split("    def _shutdown_worker(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_teardown_services()", worker)
        self.assertIn("_handover_unfinished_restores()", worker)
        self.assertIn("self.defer(self._finish_shutdown)", worker)
        teardown = APP_SOURCE.split("    def _teardown_services(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_restore_sources_for_shutdown", teardown)
        self.assertIn("detector.close", teardown)

    def test_the_ui_keeps_pumping_while_the_handover_runs(self):
        quit_block = APP_SOURCE.split("    def quit(self)", 1)[1].split("\n    def ", 1)[0]
        # Only the timers that produce new work are stopped; the panel is not
        # destroyed, and it is invalidated so it repaints while the worker waits.
        self.assertIn("winui.invalidate(self.panel.hwnd)", quit_block)
        self.assertNotIn("destroy_window", quit_block)
        self.assertNotIn("root.destroy", quit_block)
        self.assertNotIn("_stop_deferred_pump", quit_block)


# --------------------------------------------------------------------------- #
# R07 - an unanswered identity query is UNKNOWN
# --------------------------------------------------------------------------- #
@unittest.skipIf(winapi is None, "winapi requires Windows")
class IdentityProbeTests(unittest.TestCase):
    STATE = SimpleNamespace(
        pid=1234,
        class_name="SampleWindow",
        process_name="a.exe",
        process_created=999,
        placement=None,
    )

    def verdict(self, *, pid, class_name, created=999, alive=True):
        with patch.object(winapi, "is_window", lambda h: alive), patch.object(
            winapi, "get_pid", lambda h: pid
        ), patch.object(winapi, "get_class_name", lambda h: class_name), patch.object(
            winapi, "_query_process_identity",
            lambda p, query_name=True: SimpleNamespace(created=created, name="a.exe"),
        ):
            return winapi.classify_parked_window(4242, self.STATE)

    def test_a_live_window_whose_pid_query_failed_is_unknown(self):
        self.assertEqual(self.verdict(pid=0, class_name="SampleWindow"), winapi.VERIFY_UNKNOWN)

    def test_a_live_window_whose_class_query_failed_is_unknown(self):
        self.assertEqual(self.verdict(pid=1234, class_name=""), winapi.VERIFY_UNKNOWN)

    def test_a_successful_mismatch_is_still_reused(self):
        self.assertEqual(self.verdict(pid=999999, class_name="SampleWindow"), winapi.VERIFY_REUSED)
        self.assertEqual(self.verdict(pid=1234, class_name="Other"), winapi.VERIFY_REUSED)
        self.assertEqual(self.verdict(pid=1234, class_name="SampleWindow", created=1), winapi.VERIFY_REUSED)

    def test_a_gone_window_is_gone(self):
        self.assertEqual(self.verdict(pid=1234, class_name="SampleWindow", alive=False), winapi.VERIFY_GONE)

    def test_the_executor_uses_the_same_verdicts(self):
        record = make_record()
        layer = SimpleNamespace(
            is_window=lambda h: True,
            get_pid=lambda h: 0,
            get_class_name=lambda h: "SampleWindow",
            _query_process_identity=lambda pid, query_name=True: SimpleNamespace(created=999, name="a.exe"),
            is_stranded_park=lambda h, **kwargs: False,
            is_effectively_onscreen=lambda h, min_visible=24: False,
        )
        self.assertEqual(restoreguard.assess(layer, record), restoreguard.STATUS_UNVERIFIED)
        layer.get_pid = lambda h: 1234
        layer.get_class_name = lambda h: ""
        self.assertEqual(restoreguard.assess(layer, record), restoreguard.STATUS_UNVERIFIED)
        layer.get_class_name = lambda h: "SampleWindow"
        self.assertEqual(restoreguard.assess(layer, record), restoreguard.STATUS_OFFSCREEN)

    def test_the_probe_helpers_report_a_missing_answer(self):
        self.assertIsNone(winapi.probe_pid(0))
        self.assertIsNone(winapi.probe_class_name(0))


# --------------------------------------------------------------------------- #
# R08 - a parked window stays identifiable after the monitor layout changes
# --------------------------------------------------------------------------- #
@unittest.skipIf(winapi is None, "winapi requires Windows")
class ParkIdentityTests(unittest.TestCase):
    def setUp(self):
        winapi.user32.CreateWindowExW.argtypes = [
            ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        winapi.user32.CreateWindowExW.restype = ctypes.c_void_p
        self.hwnd = int(winapi.user32.CreateWindowExW(
            0, "STATIC", "luw sample probe", 0x00CF0000 | 0x10000000,
            40, 40, 300, 200, None, None, None, 0,
        ))
        self.assertTrue(self.hwnd, "could not create the probe window")
        self.addCleanup(self.destroy)

    def destroy(self):
        try:
            winapi.user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
        except Exception:  # pragma: no cover - defensive
            pass

    def shift_virtual_origin(self, offset: int = -1920):
        """Pretend a monitor was added on the left, as a second screen would."""
        original = winapi.user32.GetSystemMetrics
        indices = {
            winapi.SM_XVIRTUALSCREEN: offset,
            winapi.SM_YVIRTUALSCREEN: 0,
            winapi.SM_CXVIRTUALSCREEN: 3840,
            winapi.SM_CYVIRTUALSCREEN: 1080,
        }
        winapi.user32.GetSystemMetrics = lambda index: indices.get(index, original(index))
        self.addCleanup(setattr, winapi.user32, "GetSystemMetrics", original)

    def test_the_park_carries_a_mark_and_a_recorded_rectangle(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.assertIsNotNone(state)
        self.assertTrue(state.operation_id)
        self.assertEqual(winapi.park_operation_id(self.hwnd), state.operation_id)
        self.assertTrue(state.park_rect)
        self.assertEqual(state.park_origin, winapi.virtual_screen_origin())
        record = recovery.record_from_state(self.hwnd, state, owner=executor("a"))
        self.assertEqual(record.operation_id, state.operation_id)
        self.assertEqual(record.park_rect, state.park_rect)
        self.assertEqual(record.park_origin, state.park_origin)

    def test_our_window_is_still_recognised_after_a_monitor_change(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.assertIsNotNone(state)
        self.shift_virtual_origin()
        # The signature derived from the current layout no longer matches...
        self.assertFalse(winapi.looks_like_lookup_parked(self.hwnd))
        # ... but our own mark and the recorded rectangle still do.
        self.assertTrue(winapi.is_stranded_park(self.hwnd))
        self.assertTrue(
            winapi.is_stranded_park(
                self.hwnd, operation_id=state.operation_id, park_rect=state.park_rect
            )
        )
        self.assertTrue(winapi.looks_like_parked_at(self.hwnd, state.park_rect))

    def test_a_stranger_off_screen_window_is_never_claimed(self):
        winapi.user32.SetWindowPos(
            self.hwnd, None, -4000, -4000, 0, 0,
            winapi.SWP_NOZORDER | winapi.SWP_NOACTIVATE | winapi.SWP_NOSIZE,
        )
        self.assertFalse(winapi.is_stranded_park(self.hwnd))
        self.assertFalse(winapi.recover_orphaned_lookup_park(self.hwnd))

    def test_the_orphan_path_restores_our_window_and_drops_the_mark(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.assertIsNotNone(state)
        self.shift_virtual_origin()
        self.assertTrue(
            winapi.recover_orphaned_lookup_park(
                self.hwnd, operation_id=state.operation_id, park_rect=state.park_rect
            )
        )
        self.assertEqual(winapi.park_operation_id(self.hwnd), "")

    def test_a_visible_window_is_never_treated_as_stranded(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.assertIsNotNone(state)
        winapi.user32.SetWindowPos(self.hwnd, None, 40, 40, 300, 200, winapi.SWP_NOZORDER)
        self.assertTrue(winapi.is_effectively_onscreen(self.hwnd))
        self.assertFalse(winapi.is_stranded_park(self.hwnd))
        self.assertFalse(winapi.recover_orphaned_lookup_park(self.hwnd, operation_id=state.operation_id))

    def test_a_verified_restore_drops_the_mark(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.assertIsNotNone(state)
        self.assertTrue(winapi.restore_parked_window_sync(self.hwnd, state))
        self.assertEqual(winapi.park_operation_id(self.hwnd), "")

    def test_the_executor_accepts_the_record_as_parking_evidence(self):
        state = winapi.park_window_offscreen_sync(self.hwnd)
        self.shift_virtual_origin()
        record = recovery.record_from_state(self.hwnd, state, owner=executor("a"))
        self.assertTrue(restoreguard.parking_evidence(winapi, record))
        self.assertEqual(restoreguard.assess(winapi, record), restoreguard.STATUS_PARKED)

    def test_the_quarantined_document_is_read_back_as_an_obligation(self):
        # The recordless damage case: the preserved document still names the window.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json.park.json"
            document = make_record(4242).to_dict()
            document.pop("normalPosition")
            path.write_text(json.dumps({"version": JOURNAL_VERSION + 100, "records": [document]}), "utf-8")
            journal = RecoveryJournal(path)
            journal.reload()
            self.assertTrue(Path(str(path) + ".invalid").exists())
            entries = recovery.quarantined_entries(path)
            self.assertEqual(len(entries), 1)
            salvaged = recovery.salvage_record(entries[0])
            self.assertEqual(salvaged.hwnd, 4242)
            self.assertEqual(salvaged.park_rect, make_record(4242).park_rect)


# --------------------------------------------------------------------------- #
# R09/R10/R11 - release-side regressions
# --------------------------------------------------------------------------- #
class ReleaseContractTests(unittest.TestCase):
    def test_the_pre_park_gate_waits_for_a_confirmed_executor(self):
        source = APP_SOURCE.split("    def _await_session_executor", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_supervisor_has_live_executor()", source)
        self.assertIn("_start_session_guardian()", source)
        self.assertIn("SESSION_GUARDIAN_CONFIRM_TIMEOUT_SEC", source)
        # It retries rather than parking anyway.
        self.assertIn("time.sleep", source)
        # ... and it is called before the move, not after it.
        worker = APP_SOURCE.split("    def _park_source_worker", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(
            worker.index("_await_session_executor()"),
            worker.index("park_window_offscreen_sync"),
        )

    def test_the_hardkill_scenario_proves_recovery_without_a_new_process(self):
        smoke = (ROOT / "tools" / "runtime_smoke.py").read_text(encoding="utf-8")
        scenario = smoke.split("def scenario_hardkill(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("assert_restored", scenario)
        # The restore has to be observed before any new application is started.
        self.assertLess(
            scenario.index("assert_restored"),
            scenario.index("AppRun("),
            "the scenario restarts the application instead of proving the guardian did it",
        )
        # ... and the capture helpers still must not survive.
        self.assertIn("capture helpers survived", scenario)
        self.assertIn("guardian to exit", smoke.split("def wait_for_executor_exit(", 1)[1])
        self.assertIn(
            "wait_for_executor_exit(args)",
            smoke.split("def run_tail(", 1)[1].split("\ndef ", 1)[0],
            "the tail of a passing scenario still has to wait the guardian out",
        )

    def test_the_release_gate_runs_the_registry_itself(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("--all", release)
        self.assertNotIn("$scenarios", release)

    def test_the_published_checksum_describes_the_published_bytes(self):
        manifest = (ROOT / "tools" / "release_manifest.py").read_text(encoding="utf-8")
        self.assertIn("def verify(", manifest)
        self.assertIn("sha256_of(candidate)", manifest)
        self.assertIn("def invalidate(", manifest)


if __name__ == "__main__":
    unittest.main()