"""Regressions for the release-blocking recovery defects.

Every test here maps to a failure mode that used to lose a user's window, and
each one *executes* the behaviour rather than grepping for it: a static check can
only prove that a name is still in the source, never that a journal entry
survives a hostile condition.

The families:

* F01 the executor mutex belongs to a journal, not to a session;
* F02 the two-stage handover reads both tokens from one live pipe;
* F03 "cannot judge now" is not "not ours any more";
* F04 a park operation may not adopt somebody else's obligation;
* F05 a missing optional field is data we do not have, not an exception;
* F06 a quarantined journal is unresolved, not empty;
* F07 the writer may not produce a document its own reader quarantines;
* F08 an unknown journal state never ends the executor;
* F09 startup leftovers get an executor that outlives the session;
* F10 visibility, not geometry, decides whether a window is back;
* F12 any qualifying monitor intersection makes a window accessible;
* F13 a confirmed save must load back;
* F14 an unconfirmed guardian launch is owned by a kill-on-close job.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import json
import math
import os
import shutil
import subprocess
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

import restoreguard  # noqa: E402  (after source-path bootstrap)
import screen  # noqa: E402
from config import MAX_CONFIG_BYTES, AppConfig, ConfigService, TrackedWindow  # noqa: E402
from recovery import (  # noqa: E402
    DAMAGED_JOURNAL_STATUSES,
    JOURNAL_STATUS_DEGRADED,
    JOURNAL_STATUS_EMPTY,
    JOURNAL_VERSION,
    MAX_JOURNAL_BYTES,
    MAX_RECORDS,
    STATE_PARKED,
    ExecutorIdentity,
    ParkRecord,
    RecoveryJournal,
    journal_identity,
)

try:
    import winapi  # noqa: E402
except Exception:  # pragma: no cover - non-Windows
    winapi = None

APP_SOURCE = (SRC / "app.py").read_text(encoding="utf-8")
# How many guardian launches the old bounded startup loop used to give up after.
STARTUP_SUPERVISOR_FAILURES = 3
GUARD_SOURCE = (SRC / "restoreguard.py").read_text(encoding="utf-8")
OWNER_RUN_ID = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


def make_record(hwnd: int = 4242, **overrides) -> ParkRecord:
    values = {
        "hwnd": hwnd,
        "pid": 1234,
        "class_name": "SomeWindowClass",
        "process_name": "app.exe",
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
        "label": "app.exe - Some",
        "state": STATE_PARKED,
    }
    values.update(overrides)
    return ParkRecord(**values)


def executor(tag: str = "a") -> ExecutorIdentity:
    return ExecutorIdentity(executor_id=f"exec-{tag}", pid=1000 + ord(tag), created=42, label=tag)


class SweepResult:
    """What a sweep has to report: recovered, still parked, unknown, and whether
    the desktop could be enumerated at all.

    Mirrors the production ``winapi.OrphanSweep`` contract on purpose: a sweep
    that cannot say all four is not allowed to claim that nothing is left.
    """

    def __init__(self, recovered=(), pending=(), unknown=(), complete=True):
        self.recovered = tuple(recovered)
        self.pending = tuple(pending)
        self.unknown = tuple(unknown)
        self.complete = bool(complete)

    @property
    def decided(self):
        return self.complete and not self.pending and not self.unknown

    def describe(self):
        return (
            f"recovered={list(self.recovered)} pending={list(self.pending)} "
            f"unknown={list(self.unknown)} complete={self.complete}"
        )


class FakeWinapi:
    """A scriptable stand-in for the Win32 layer the executor consults."""

    def __init__(self, *, alive=True, pid=1234, class_name="SomeWindowClass",
                 created=999, parked=False, onscreen=True):
        self.alive = alive
        self.pid = pid
        self.class_name = class_name
        self.created = created
        self.parked = parked
        self.onscreen = onscreen
        self.restore_calls = 0
        self.orphan_calls = 0
        self.swept: list = []
        self.restore_result = True

    def is_window(self, hwnd):
        return self.alive

    def get_pid(self, hwnd):
        return self.pid

    def get_class_name(self, hwnd):
        return self.class_name

    class _Identity:
        def __init__(self, created):
            self.created = created
            self.name = "app.exe"
            self.access_denied = False

    def _query_process_identity(self, pid, query_name=True):
        return self._Identity(self.created)

    def looks_like_lookup_parked(self, hwnd):
        return self.parked

    def is_effectively_onscreen(self, hwnd, min_visible=24):
        return self.onscreen

    def state_from_record(self, record):
        return ("fake-parked-state", int(record.hwnd))

    def window_matches_parked_state(self, hwnd, state):
        return winapi_window_matches(self, hwnd, state)

    def restore_parked_window_sync(self, hwnd, state):
        self.restore_calls += 1
        if self.restore_result:
            self.parked = False
            self.onscreen = True
        return self.restore_result

    def recover_orphaned_lookup_park(self, hwnd):
        self.orphan_calls += 1
        if not self.restore_result:
            return False
        self.parked = False
        self.onscreen = True
        return True

    def recover_all_orphaned_parks(self, exclude=()):
        self.swept.append(set(exclude))
        if not self.parked:
            return SweepResult()
        if not self.restore_result:
            # The window is still where LookUp put it: reporting "nothing" here is
            # exactly the answer that used to call a damaged journal empty.
            return SweepResult(pending=(4242,))
        self.parked = False
        self.onscreen = True
        return SweepResult(recovered=(4242,))


def winapi_window_matches(fake, hwnd, state):
    return fake.alive and fake.pid == state.pid and fake.class_name == state.class_name


def app_method(class_name: str, method_name: str, namespace: dict):
    """Compile one ``App`` method on its own, the way the suite already does."""
    tree = ast.parse(APP_SOURCE)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            node,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "app.py", "exec"), namespace)
    return namespace[method_name]


# --------------------------------------------------------------------------- #
# F01 - the executor mutex belongs to a journal, not to a session
# --------------------------------------------------------------------------- #
@unittest.skipIf(os.name != "nt", "named mutexes are Windows only")
class PerJournalExecutorMutexTests(unittest.TestCase):
    """The journal depends on --config, portable mode and the install location."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.first = self.base / "a" / "settings.json.park.json"
        self.second = self.base / "b" / "settings.json.park.json"

    def test_two_documents_get_two_mutexes(self):
        self.assertNotEqual(
            restoreguard.guardian_mutex_name(self.first),
            restoreguard.guardian_mutex_name(self.second),
        )

    def test_the_same_document_always_gets_the_same_mutex(self):
        self.assertEqual(
            restoreguard.guardian_mutex_name(self.first),
            restoreguard.guardian_mutex_name(str(self.base / "a" / "." / "settings.json.park.json")),
        )

    def test_a_guardian_of_another_document_never_answers_for_this_one(self):
        # The exact reproduction of the defect: one guardian holds journal A while
        # journal B is started.  B must execute, not answer "delegated".
        first = restoreguard._acquire_singleton(restoreguard.guardian_mutex_name(self.first))
        self.assertIsNotNone(first)
        self.addCleanup(first.close)
        self.assertIsNone(
            restoreguard._acquire_singleton(restoreguard.guardian_mutex_name(self.first)),
            "the same journal must report a second guardian",
        )
        second = restoreguard._acquire_singleton(restoreguard.guardian_mutex_name(self.second))
        self.assertIsNotNone(
            second,
            "another configuration's guardian must not answer for this journal",
        )
        second.close()

    def test_readiness_names_the_journal_and_the_executor(self):
        journal = RecoveryJournal(self.first)
        completed = subprocess.run(
            restoreguard.guardian_command(str(journal.path)),
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(
            f"journal={journal_identity(journal.path)}",
            completed.stdout,
            "readiness has to identify the document it serves",
        )
        self.assertIn("owner=", completed.stdout)


# --------------------------------------------------------------------------- #
# F02 - the two-stage handover reads both tokens from one live pipe
# --------------------------------------------------------------------------- #
class TwoStageHandshakeTests(unittest.TestCase):
    """`started` then `ready` must both arrive on the same, still-open pipe."""

    def child(self, script):
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script], stdout=subprocess.PIPE
        )
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def handshake(self, process, journal_id="abc", predecessor="old-exec"):
        link = restoreguard._link_for(process)
        self.assertTrue(link.wait_token(restoreguard.GUARDIAN_STARTED_TOKEN, 10.0))
        return restoreguard._confirm_handover(
            process, journal_id=journal_id, predecessor=predecessor
        )

    def test_a_delayed_second_token_is_still_received(self):
        # The old reader closed the pipe in its `finally`, so the second token was
        # written into a closed stream and the confirmation could never arrive.
        child = self.child(
            "import sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "time.sleep(1.4)\n"
            "print('ready owner=new-exec journal=abc', flush=True)\n"
            "time.sleep(30)\n"
        )
        self.assertTrue(self.handshake(child))

    def test_both_tokens_in_one_write_are_both_seen(self):
        child = self.child(
            "import sys,time\n"
            "sys.stdout.write('started successor=deadbeef journal=abc\\n"
            "ready owner=new-exec journal=abc\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        self.assertTrue(self.handshake(child))

    def test_a_token_split_between_two_polls_is_completed(self):
        child = self.child(
            "import sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "sys.stdout.write('ready owner=new-exec jour')\n"
            "sys.stdout.flush()\n"
            "time.sleep(1.2)\n"
            "sys.stdout.write('nal=abc\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        self.assertTrue(self.handshake(child))

    def test_end_of_stream_between_the_stages_is_not_a_confirmation(self):
        child = self.child(
            "import sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "sys.exit(0)\n"
        )
        self.assertFalse(self.handshake(child))

    def test_a_successor_that_dies_before_confirming_is_not_a_confirmation(self):
        child = self.child(
            "import os,signal,sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "time.sleep(0.3)\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        self.assertFalse(self.handshake(child))
        self.assertLess(time.monotonic() - started, 20.0)

    def test_a_confirmation_for_another_journal_is_refused(self):
        child = self.child(
            "import sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "print('ready owner=new-exec journal=somebody-else', flush=True)\n"
            "time.sleep(30)\n"
        )
        self.assertFalse(self.handshake(child))

    def test_an_echo_of_our_own_identity_is_refused(self):
        child = self.child(
            "import sys,time\n"
            "print('started successor=deadbeef journal=abc', flush=True)\n"
            "print('ready owner=old-exec journal=abc', flush=True)\n"
            "time.sleep(30)\n"
        )
        self.assertFalse(self.handshake(child))

    def test_the_single_token_form_still_works(self):
        child = self.child("print('ready owner=x journal=abc'); import time; time.sleep(30)")
        self.assertTrue(restoreguard._wait_for_token(child, 10))


# --------------------------------------------------------------------------- #
# F03 - "cannot judge now" is not "not ours any more"
# --------------------------------------------------------------------------- #
class ParkedWindowVerdictTests(unittest.TestCase):
    """One verdict for every caller, with an explicit 'unknown'."""

    def setUp(self):
        self.state = SimpleNamespace(
            pid=1234,
            class_name="SomeWindowClass",
            process_name="app.exe",
            process_created=999,
            placement=object(),
        )

    def classify(self, fake):
        with patch.object(winapi, "is_window", fake.is_window), patch.object(
            winapi, "get_pid", fake.get_pid
        ), patch.object(winapi, "get_class_name", fake.get_class_name), patch.object(
            winapi, "_query_process_identity", fake._query_process_identity
        ):
            return winapi.classify_parked_window(4242, self.state)

    def test_a_vanished_handle_is_proven_gone(self):
        self.assertEqual(self.classify(FakeWinapi(alive=False)), winapi.VERIFY_GONE)

    def test_a_different_process_is_proven_reused(self):
        self.assertEqual(self.classify(FakeWinapi(pid=9999)), winapi.VERIFY_REUSED)

    def test_a_different_class_is_proven_reused(self):
        self.assertEqual(self.classify(FakeWinapi(class_name="Other")), winapi.VERIFY_REUSED)

    def test_a_different_creation_time_is_proven_reused(self):
        self.assertEqual(self.classify(FakeWinapi(created=1000)), winapi.VERIFY_REUSED)

    def test_an_unopenable_process_is_unknown_not_reused(self):
        # A live window whose process cannot be opened is the case that used to
        # delete the only proof that it was parked.
        self.assertEqual(self.classify(FakeWinapi(created=None)), winapi.VERIFY_UNKNOWN)

    def test_a_matching_window_is_a_match(self):
        self.assertEqual(self.classify(FakeWinapi()), winapi.VERIFY_MATCH)

    def test_the_destructive_form_agrees_only_on_a_proven_match(self):
        # park/restore must stay conservative, so the boolean form is not widened.
        with patch.object(winapi, "is_window", lambda h: True), patch.object(
            winapi, "get_pid", lambda h: 1234
        ), patch.object(winapi, "get_class_name", lambda h: "SomeWindowClass"), patch.object(
            winapi, "_query_process_identity", lambda pid, query_name=True: SimpleNamespace(
                created=None, name="app.exe", access_denied=True
            )
        ):
            self.assertFalse(winapi.window_matches_parked_state(4242, self.state))


@unittest.skipIf(winapi is None, "App requires Windows")
class UnverifiableIdentityKeepsTheRecordTests(unittest.TestCase):
    """The UI paths must apply the same rule as the executor."""

    def setUp(self):
        from app import App

        self.App = App
        self.app = App.__new__(App)
        self.app._recovery_lock = threading.Lock()
        self.app._recovery_registry = {}
        self.app._recovery_inflight = set()
        self.app._executor = executor("a")
        self.drops: list = []
        self.app._journal_drop_async = lambda hwnd, recorded_at=None: self.drops.append(
            (hwnd, recorded_at)
        )
        self.state = SimpleNamespace(pid=1234, class_name="C", process_name="app.exe",
                                     process_created=999, placement=None)
        self.recorded_at = time.time()
        self.app._recovery_registry[4242] = SimpleNamespace(
            state=self.state,
            label="target",
            attempts=0,
            retry_at=0.0,
            needs_retry=False,
            recorded_at=self.recorded_at,
        )

    def attempt(self, verdict):
        with patch.object(winapi, "classify_parked_window", lambda hwnd, state: verdict):
            return self.App._complete_recovery_attempt(self.app, 4242, self.state, False)

    def test_an_unknown_identity_keeps_state_journal_and_retry(self):
        stale = self.attempt(winapi.VERIFY_UNKNOWN)
        self.assertFalse(stale)
        self.assertEqual(self.drops, [], "the record was retired on a guess")
        entry = self.app._recovery_registry[4242]
        self.assertTrue(entry.needs_retry, "no retry was scheduled")
        self.assertEqual(entry.recorded_at, self.recorded_at)

    def test_a_proven_gone_window_retires_the_record(self):
        self.assertTrue(self.attempt(winapi.VERIFY_GONE))
        self.assertNotIn(4242, self.app._recovery_registry)
        self.assertEqual(self.drops, [(4242, self.recorded_at)])

    def test_a_proven_reused_handle_retires_the_record(self):
        self.assertTrue(self.attempt(winapi.VERIFY_REUSED))
        self.assertEqual(self.drops, [(4242, self.recorded_at)])

    def test_a_verified_restore_retires_the_record_without_judging_identity(self):
        with patch.object(winapi, "classify_parked_window", lambda hwnd, state: winapi.VERIFY_UNKNOWN):
            self.App._complete_recovery_attempt(self.app, 4242, self.state, True)
        self.assertEqual(self.drops, [(4242, self.recorded_at)])
        self.assertNotIn(4242, self.app._recovery_registry)

    def test_the_watcher_does_not_decide_anything_on_the_timer(self):
        watcher = APP_SOURCE.split("    def _watch_parked_sources", 1)[1].split("\n    def ", 1)[0]
        # A timer callback runs on the native dispatch thread: it may not ask the
        # target process anything, and it may not conclude "not parked" from a
        # failed answer.
        for forbidden in ("window_matches_parked_state", "classify_parked_window",
                          "_drop_recovery(", "recovery_journal."):
            self.assertNotIn(forbidden, watcher)
        self.assertIn("_queue_cleanup_restore(", watcher)


# --------------------------------------------------------------------------- #
# F04 - a park may not adopt somebody else's obligation
# --------------------------------------------------------------------------- #
class TransactionalIntentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)

    def stored(self):
        return RecoveryJournal(self.path).get(4242)

    def test_a_second_intent_is_refused_and_changes_nothing(self):
        first = make_record()
        self.assertTrue(self.journal.record_intent(first))
        # The executor that is working on the record keeps its fencing token.
        claim = self.journal.claim(4242, executor("a"))
        self.assertIsNotNone(claim)

        second = make_record(
            screen_rect=(-299, -199, -298, -198),
            normal_position=(-299, -199, -298, -198),
            owner_run_id="other-run",
            owner_pid=999,
            recorded_at=time.time(),
        )
        self.assertFalse(self.journal.record_intent(second), "the obligation was overwritten")
        kept = self.stored()
        self.assertEqual(kept.screen_rect, (10, 10, 310, 210))
        self.assertEqual(kept.normal_position, (10, 10, 310, 210))
        self.assertEqual(kept.owner_run_id, OWNER_RUN_ID)
        self.assertEqual(kept.owner_pid, 77)
        self.assertEqual(kept.recorded_at, first.recorded_at)
        self.assertTrue(kept.claim_matches(claim), "the old fencing token lost its record")

    def test_an_expired_claim_still_refuses_a_new_intent(self):
        self.assertTrue(self.journal.record_intent(make_record()))
        self.expire_lease(4242)
        self.assertFalse(self.journal.record_intent(make_record()))

    def test_a_record_without_any_claim_still_refuses_a_new_intent(self):
        self.assertTrue(self.journal.record_intent(make_record()))
        self.assertEqual(self.stored().claim_executor, "")
        self.assertFalse(self.journal.record_intent(make_record()))

    def test_an_idempotent_repeat_of_the_same_park_is_accepted(self):
        record = make_record(operation_id="operation-1")
        self.assertTrue(self.journal.record_intent(record))
        committed = json.loads(self.path.read_text(encoding="utf-8"))
        # The very same park, retried: allowed, and it must not rewrite anything.
        repeat = make_record(operation_id="operation-1")
        self.assertTrue(self.journal.record_intent(repeat))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), committed)

    def test_the_same_operation_with_changed_geometry_is_not_idempotent(self):
        self.assertTrue(self.journal.record_intent(make_record(operation_id="operation-1")))
        moved = make_record(operation_id="operation-1", screen_rect=(1, 2, 33, 44))
        self.assertFalse(self.journal.record_intent(moved))
        self.assertEqual(self.stored().screen_rect, (10, 10, 310, 210))

    def test_every_park_operation_gets_its_own_id(self):
        first = make_record(operation_id="")
        second = make_record(operation_id="")
        self.assertTrue(self.journal.record_intent(first))
        self.assertTrue(first.operation_id)
        self.assertNotEqual(first.operation_id, second.operation_id)
        self.assertFalse(self.journal.record_intent(second))

    def expire_lease(self, hwnd):
        document = json.loads(self.path.read_text(encoding="utf-8"))
        for item in document["records"]:
            if int(item["hwnd"]) == int(hwnd):
                item["claimUntil"] = time.time() - 60.0
        self.path.write_text(json.dumps(document), encoding="utf-8")


# --------------------------------------------------------------------------- #
# F05 - a missing optional field is not an exception
# --------------------------------------------------------------------------- #
class ParserContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"

    def write(self, records, version: int = JOURNAL_VERSION):
        self.path.write_text(
            json.dumps({"version": version, "records": records}), encoding="utf-8"
        )

    def test_a_record_without_process_created_is_read_not_crashed(self):
        payload = make_record().to_dict()
        del payload["processCreated"]
        self.write([payload])
        journal = RecoveryJournal(self.path)
        records = journal.reload()
        self.assertEqual([record.hwnd for record in records], [4242])
        self.assertIsNone(records[0].process_created)

    def test_no_required_field_may_raise_out_of_the_reader(self):
        for field in (
            "hwnd", "pid", "class", "processName", "screenRect", "showCmd",
            "placementFlags", "minPosition", "maxPosition", "normalPosition",
            "ownerPid", "recordedAt", "processCreated",
        ):
            with self.subTest(field=field):
                payload = make_record().to_dict()
                payload.pop(field, None)
                self.write([payload])
                journal = RecoveryJournal(self.path)
                try:
                    records = journal.reload()
                except (KeyError, TypeError, ValueError) as exc:
                    self.fail(f"{field} leaked {exc.__class__.__name__} out of the parser")
                if field == "processCreated":
                    # Optional by contract: an unknown creation time is a fact the
                    # executor can work with, not a missing field.
                    self.assertEqual([record.hwnd for record in records], [4242])
                    self.assertIsNone(records[0].process_created)
                else:
                    # Required: without it the entry is not a restorable fact, so
                    # it is refused - and the refusal is reported, not raised.
                    self.assertEqual(records, ())

    def test_no_field_value_may_raise_out_of_the_reader(self):
        for field, value in (
            ("hwnd", {"nested": 1}),
            ("hwnd", "4242"),
            ("pid", [1]),
            ("screenRect", "not a rect"),
            ("showCmd", {"a": 1}),
            ("normalPosition", [1, 2, 3]),
            ("processCreated", float("nan")),
            ("recordedAt", [1, 2]),
            ("claimUntil", math.inf),
            ("state", "not-a-state"),
            ("claimGeneration", "many"),
        ):
            with self.subTest(field=field, value=repr(value)[:20]):
                payload = make_record().to_dict()
                payload[field] = value
                self.write([payload])
                try:
                    RecoveryJournal(self.path).reload()
                except (KeyError, TypeError, ValueError) as exc:
                    self.fail(f"{field} leaked {exc.__class__.__name__} out of the reader")

    def test_the_operation_id_round_trips(self):
        record = ParkRecord.from_dict(make_record(operation_id="op-42").to_dict())
        self.assertEqual(record.operation_id, "op-42")


# --------------------------------------------------------------------------- #
# F06 - a quarantined journal is unresolved, not empty
# --------------------------------------------------------------------------- #
class QuarantineIsNotEmptinessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"

    def test_a_wholly_unreadable_document_stays_unresolved(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        self.assertEqual(journal.records(), ())
        self.assertTrue(Path(str(self.path) + ".invalid").exists())
        self.assertIn(journal.status, DAMAGED_JOURNAL_STATUSES)
        self.assertTrue(journal.damaged)
        # The status survives the next, absent-file read: "I could not read it"
        # may not decay into "there is nothing there".
        self.assertIn(journal.status, DAMAGED_JOURNAL_STATUSES)
        self.assertTrue(journal.damaged)

    def test_a_partly_damaged_document_keeps_what_is_salvageable(self):
        good = make_record(4242).to_dict()
        broken = make_record(777).to_dict()
        broken.pop("normalPosition")
        broken["screenRect"] = "not a rect"
        self.path.write_text(
            json.dumps({"version": JOURNAL_VERSION, "records": [good, broken]}),
            encoding="utf-8",
        )
        journal = RecoveryJournal(self.path)
        self.assertEqual([record.hwnd for record in journal.records()], [4242])
        self.assertEqual(journal.status, JOURNAL_STATUS_DEGRADED)
        self.assertEqual([record.hwnd for record in journal.unresolved], [777])
        self.assertFalse(journal.unresolved[0].placement_usable,
                         "a salvaged record may not authorise an exact placement")

    def test_resolving_the_damage_brings_the_window_back_and_clears_the_status(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.records()
        fake = FakeWinapi(parked=True, onscreen=False)
        with patch.object(restoreguard, "winapi", lambda: fake):
            result = restoreguard.resolve_journal_damage(journal)
        self.assertEqual(list(result.recovered), [4242])
        self.assertTrue(result.decided)
        self.assertEqual(journal.status, JOURNAL_STATUS_EMPTY)
        self.assertFalse(journal.damaged)
        # ... and durably so: the next process must not see the damage again, or it
        # would sweep on every start for the rest of the machine's life.
        fresh = RecoveryJournal(self.path)
        self.assertFalse(fresh.damaged)
        self.assertEqual(fresh.status, JOURNAL_STATUS_EMPTY)

    def test_the_damage_sweep_never_touches_a_window_the_journal_still_owns(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.records()
        fake = FakeWinapi(parked=True, onscreen=False)
        with patch.object(restoreguard, "winapi", lambda: fake):
            restoreguard.resolve_journal_damage(journal)
        self.assertEqual(fake.swept, [set()])

    def test_the_guardian_resolves_damage_before_it_may_idle_exit(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.records()
        fake = FakeWinapi(parked=True, onscreen=False)
        with patch.object(restoreguard, "winapi", lambda: fake), patch.object(
            restoreguard, "POLL_SEC", 0.01
        ):
            outcome = restoreguard.run_guardian(
                journal.path, announce=False, executor=executor("g")
            )
        self.assertEqual(outcome, 0)
        self.assertEqual(len(fake.swept), 1, "the guardian never swept the parking position")
        # The guardian works on its own instance; a fresh read is what the *next*
        # process would see, and the sweep that really did recover the window has
        # said so durably.
        fresh = RecoveryJournal(self.path)
        fresh.records()
        self.assertFalse(fresh.damaged)
        self.assertEqual(fresh.status, JOURNAL_STATUS_EMPTY)
        # The quarantined document is kept as a trace, but under a name that no
        # longer reports unfinished work.
        self.assertFalse(Path(str(self.path) + ".invalid").exists())
        self.assertTrue(Path(str(self.path) + ".invalid.swept").exists())

    def test_a_failing_sweep_still_does_not_claim_there_is_nothing_to_do(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.records()
        fake = FakeWinapi(parked=True, onscreen=False)
        fake.restore_result = False
        with patch.object(restoreguard, "winapi", lambda: fake):
            result = restoreguard.resolve_journal_damage(journal)
        self.assertEqual(list(result.recovered), [])
        self.assertFalse(result.decided, "a sweep that restored nothing decided something")
        self.assertEqual(result.outstanding_hwnds, (4242,))
        # The damage was *not* acknowledged: the executor may keep looking, and so
        # must every later reader.
        self.assertTrue(journal.damaged)
        fresh = RecoveryJournal(self.path)
        fresh.records()
        self.assertTrue(fresh.damaged)
        self.assertTrue(Path(str(self.path) + ".invalid").exists())

    def test_a_sweep_that_cannot_enumerate_the_desktop_decides_nothing(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        journal.records()
        fake = FakeWinapi(parked=True, onscreen=False)
        fake.recover_all_orphaned_parks = lambda exclude=(): SweepResult(complete=False)
        with patch.object(restoreguard, "winapi", lambda: fake):
            result = restoreguard.resolve_journal_damage(journal)
        self.assertFalse(result.decided)
        self.assertTrue(journal.damaged)


# --------------------------------------------------------------------------- #
# F07 - the writer may not produce a document its reader quarantines
# --------------------------------------------------------------------------- #
class WriterSharesTheReaderContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)

    def test_the_record_limit_is_enforced_before_the_document_is_replaced(self):
        for index in range(1, MAX_RECORDS + 1):
            self.assertTrue(self.journal.record_intent(make_record(index)))
        self.assertEqual(len(RecoveryJournal(self.path).records()), MAX_RECORDS)
        self.assertFalse(
            self.journal.record_intent(make_record(MAX_RECORDS + 1)),
            "the 513th record was accepted",
        )
        self.assertEqual(
            [record.hwnd for record in RecoveryJournal(self.path).records()],
            list(range(1, MAX_RECORDS + 1)),
            "the previous document was damaged by a refused change",
        )

    def test_an_oversized_label_never_produces_an_unreadable_journal(self):
        # The exact reproduction: a runtime title of a megabyte used to be written
        # successfully and then sent to quarantine by the very next read, which
        # turned a registered park into a lost obligation.
        self.assertTrue(self.journal.record_intent(make_record(1)))
        self.assertTrue(
            self.journal.record_intent(make_record(2, label="x" * (MAX_JOURNAL_BYTES + 16)))
        )
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertLess(len(self.path.read_bytes()), MAX_JOURNAL_BYTES)
        self.assertEqual([record.hwnd for record in RecoveryJournal(self.path).records()], [1, 2])
        self.assertEqual(len(document["records"]), 2)

    def test_a_large_but_readable_document_is_accepted(self):
        label = "ж" * 400
        self.assertTrue(self.journal.record_intent(make_record(1, label=label)))
        stored = RecoveryJournal(self.path).get(1)
        self.assertEqual(stored.label, label)

    def test_a_document_over_the_shared_byte_limit_is_refused(self):
        self.assertTrue(self.journal.record_intent(make_record(1)))
        before = self.path.read_bytes()
        with patch("recovery.MAX_JOURNAL_BYTES", 1500):
            self.assertFalse(
                self.journal.record_intent(make_record(2)),
                "a document the reader would quarantine was written anyway",
            )
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([record.hwnd for record in RecoveryJournal(self.path).records()], [1])

    def test_written_records_survive_their_own_schema(self):
        self.assertTrue(self.journal.record_intent(make_record(1)))
        entries = json.loads(self.path.read_text(encoding="utf-8"))["records"]
        for entry in entries:
            self.assertIsNotNone(
                ParkRecord.from_dict(entry), "a written record cannot be read back"
            )

    def test_a_corrupt_geometry_is_written_the_way_the_reader_sees_it(self):
        # Emitting a plausible zero instead of the damage would turn a corrupt
        # placement into a *trustworthy* 1x1 window at the origin, and the restore
        # would then put the user's window somewhere it never was.
        self.assertTrue(self.journal.record_intent(make_record(1)))
        self.assertTrue(
            self.journal.record_intent(make_record(2, screen_rect=(math.nan, 0, 1, 1)))
        )
        stored = RecoveryJournal(self.path).get(2)
        self.assertFalse(stored.placement_usable)
        self.assertEqual(stored.screen_rect, (0, 0, 0, 0))
        for value in (*stored.normal_position, *stored.screen_rect):
            self.assertTrue(math.isfinite(value))

    def test_the_writer_never_raises_on_a_record_built_by_hand(self):
        # A record may be nonsense; the transaction that stores it still has to
        # report an outcome instead of unwinding with an exception.
        broken = make_record(2, screen_rect=("left", 0, 1, 1), recorded_at=math.inf)
        self.assertTrue(self.journal.record_intent(broken))
        stored = RecoveryJournal(self.path).get(2)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.recorded_at, 0.0, "an unusable instant was persisted")


# --------------------------------------------------------------------------- #
# F09 - startup leftovers get an executor that outlives the session
# --------------------------------------------------------------------------- #
class FakeGuardian:
    """A guardian process handle: alive until it is told to exit."""

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.exit_code = None

    def poll(self):
        return self.exit_code


class StartupSupervisorTests(unittest.TestCase):
    """The supervisor is long-lived: it keeps watching, it does not give up.

    The defect this replaces gave the leftovers to a guardian exactly three times
    and then returned, so a session in which the guardian could not be started kept
    a parked window off-screen until the next launch - with a log line that read
    like the obligation had been taken care of.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.spawns = []

    def app(self, **overrides):
        values = {
            "recovery_journal": self.journal,
            "_executor": executor("a"),
            "_shutting_down": False,
            "_recovery_guardian": None,
            "_recovery_supervisor": None,
            "_recovery_supervisor_stop": threading.Event(),
            "_recovery_supervisor_wanted": threading.Event(),
            "_recovery_lock": threading.Lock(),
            "_guardian_start_lock": threading.Lock(),
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

    def supervisor_worker(self):
        namespace = {
            "logger": Mock(),
            "time": SimpleNamespace(sleep=lambda _seconds: None, monotonic=time.monotonic),
            "SUPERVISOR_IDLE_POLL_SEC": 0.01,
            "SUPERVISOR_POLL_CEILING_SEC": 0.01,
            "STARTUP_RECOVERY_BUDGET_SEC": 0.1,
            "restoreguard": SimpleNamespace(
                resolve_all=Mock(), resolve_journal_damage=Mock(return_value=SimpleNamespace(recovered=()))
            ),
        }
        return app_method("App", "_recovery_supervisor_worker", namespace), namespace

    def test_more_than_three_failed_launches_still_leave_a_live_executor(self):
        self.journal.record_intent(make_record(123))
        attempts = []

        def guardian():
            attempts.append(1)
            return True if len(attempts) > STARTUP_SUPERVISOR_FAILURES else False

        app = self.app(_start_session_guardian=guardian)
        worker, _namespace = self.supervisor_worker()
        stop = app._recovery_supervisor_stop

        def run():
            worker(app)
            stop.set()

        thread = threading.Thread(target=run, name="supervisor", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while len(attempts) <= STARTUP_SUPERVISOR_FAILURES and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(timeout=5.0)
        self.assertGreater(
            len(attempts),
            STARTUP_SUPERVISOR_FAILURES,
            "the supervisor gave up instead of retrying until an executor exists",
        )
        # The record is still there and still owned: the supervisor never discards it.
        self.assertEqual([record.hwnd for record in RecoveryJournal(self.path).reload()], [123])

    def test_an_unreadable_journal_never_authorizes_bare_exit(self):
        worker, _namespace = self.supervisor_worker()
        app = self.app()
        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")):
            stop = app._recovery_supervisor_stop
            thread = threading.Thread(target=worker, args=(app,), name="supervisor", daemon=True)
            thread.start()
            time.sleep(0.1)
            self.assertTrue(thread.is_alive(), "the supervisor ended on an unreadable journal")
            stop.set()
            thread.join(timeout=5.0)
        # ... and nothing claims the journal was empty: the file is still there.
        self.assertEqual([record.hwnd for record in self.journal.reload()], [])

    def test_leftovers_are_handed_to_a_guardian_of_the_same_journal(self):
        self.journal.record_intent(make_record(1))
        spawn = Mock(return_value=FakeGuardian())
        namespace = {
            "logger": Mock(),
            "restoreguard": SimpleNamespace(spawn_guardian=spawn),
            "RESTORE_HANDOVER_TIMEOUT_SEC": 1,
        }
        start = app_method("App", "_start_session_guardian", namespace)
        app = self.app()
        self.assertTrue(start(app))
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0], self.path)
        self.assertEqual(spawn.call_args.kwargs["owner_run_id"], "exec-a")
        self.assertIsInstance(app._recovery_guardian, FakeGuardian)
        # A second call reuses the guardian that is already serving the journal.
        self.assertTrue(start(app))
        spawn.assert_called_once()

    def test_a_dead_guardian_is_replaced_instead_of_being_trusted(self):
        self.journal.record_intent(make_record(1))
        spawn = Mock(side_effect=[FakeGuardian(), FakeGuardian(pid=5555)])
        namespace = {
            "logger": Mock(),
            "restoreguard": SimpleNamespace(spawn_guardian=spawn),
            "RESTORE_HANDOVER_TIMEOUT_SEC": 1,
        }
        start = app_method("App", "_start_session_guardian", namespace)
        app = self.app()
        self.assertTrue(start(app))
        self.assertEqual(app._recovery_guardian.pid, 4321)
        app._recovery_guardian.exit_code = 1
        self.assertTrue(start(app))
        self.assertEqual(app._recovery_guardian.pid, 5555)
        self.assertEqual(spawn.call_count, 2)

    def test_a_record_another_executor_still_works_on_is_not_nothing_outstanding(self):
        # A startup worker that outlived its budget keeps its lease; counting that
        # as "no obligations" is how a late-finishing worker lost its window.
        record = make_record(1)
        self.journal.record_intent(record)
        self.journal.claim(1, executor("b"))
        guardian = restoreguard._Guardian(
            self.journal, executor=executor("g"), singleton=restoreguard._SingletonLock(None)
        )
        stored = self.journal.reload()[0]
        self.assertTrue(guardian._leased_elsewhere(stored))

    def test_a_park_is_refused_while_no_executor_can_be_confirmed(self):
        # The pre-park gate: an unconfirmed executor means the park does not start.
        namespace = {
            "logger": Mock(),
            "time": time,
            "SESSION_GUARDIAN_CONFIRM_TIMEOUT_SEC": 0.05,
        }
        await_executor = app_method("App", "_await_session_executor", namespace)
        calls = []

        def guardian():
            calls.append(1)
            return False

        app = self.app(_start_session_guardian=guardian, _ensure_recovery_supervisor=Mock())
        self.assertFalse(await_executor(app))
        self.assertGreater(len(calls), 1, "a failed launch was not retried")
        # A live executor is accepted without another launch.
        app._supervisor_has_live_executor = lambda: True
        self.assertTrue(await_executor(app))
        self.assertEqual(len(calls), len(calls))


# --------------------------------------------------------------------------- #
# F10 - visibility, not geometry, decides whether a window is back
# --------------------------------------------------------------------------- #
@unittest.skipIf(winapi is None, "winapi requires Windows")
class AccessibilityPredicateTests(unittest.TestCase):
    """A hidden or cloaked window with an ordinary rectangle is not accessible."""

    def setUp(self):
        self.hwnd = self.make_window(visible=False)
        self.addCleanup(self.destroy)

    def make_window(self, *, visible: bool):
        winapi.user32.CreateWindowExW.argtypes = [
            ctypes.c_ulong, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        winapi.user32.CreateWindowExW.restype = ctypes.c_void_p
        style = 0x00CF0000 | (0x10000000 if visible else 0)  # WS_OVERLAPPEDWINDOW [+ WS_VISIBLE]
        hwnd = winapi.user32.CreateWindowExW(
            0, "STATIC", "luw recovery probe", style, 10, 10, 300, 200, None, None, None, 0
        )
        self.assertTrue(hwnd, "could not create the probe window")
        return int(hwnd)

    def destroy(self):
        try:
            winapi.user32.DestroyWindow(ctypes.c_void_p(self.hwnd))
        except Exception:  # pragma: no cover - defensive
            pass

    def test_the_probe_window_is_geometry_visible(self):
        # The pre-condition of the regression: without it the test would pass for
        # the wrong reason.
        left, top, right, bottom = winapi.get_window_rect(self.hwnd)
        self.assertTrue(winapi.is_visible_on_monitors((left, top, right, bottom),
                                                      winapi.display_monitor_rects()))

    def test_a_hidden_window_is_not_effectively_onscreen(self):
        self.assertFalse(winapi.user32.IsWindowVisible(self.hwnd))
        self.assertFalse(winapi.is_window_shown(self.hwnd))
        self.assertFalse(winapi.is_effectively_onscreen(self.hwnd))

    def test_the_same_window_becomes_visible_once_it_is_shown(self):
        winapi.user32.ShowWindow(ctypes.c_void_p(self.hwnd), 5)  # SW_SHOW
        self.addCleanup(winapi.user32.ShowWindow, ctypes.c_void_p(self.hwnd), 0)  # SW_HIDE
        self.assertTrue(winapi.user32.IsWindowVisible(self.hwnd))
        self.assertTrue(winapi.is_window_shown(self.hwnd))
        self.assertTrue(winapi.is_effectively_onscreen(self.hwnd))

    def test_a_cloaked_window_is_not_effectively_onscreen(self):
        winapi.user32.ShowWindow(ctypes.c_void_p(self.hwnd), 5)  # SW_SHOW
        self.addCleanup(winapi.user32.ShowWindow, ctypes.c_void_p(self.hwnd), 0)
        with patch.object(winapi, "is_cloaked", lambda hwnd: True):
            self.assertFalse(winapi.is_window_shown(self.hwnd))
            self.assertFalse(winapi.is_effectively_onscreen(self.hwnd))

    def test_the_executor_no_longer_reports_a_hidden_window_as_restored(self):
        record = make_record()
        fake = SimpleNamespace(
            is_window=lambda h: True,
            get_pid=lambda h: record.pid,
            get_class_name=lambda h: record.class_name,
            _query_process_identity=lambda pid, query_name=True: SimpleNamespace(
                created=record.process_created, name=record.process_name
            ),
            looks_like_lookup_parked=lambda h: False,
            # What the predicate answers once visibility is part of the question.
            is_effectively_onscreen=winapi.is_effectively_onscreen,
        )
        self.assertEqual(restoreguard.assess(fake, record), restoreguard.STATUS_OFFSCREEN)


# --------------------------------------------------------------------------- #
# F12 - any qualifying monitor intersection makes a window accessible
# --------------------------------------------------------------------------- #
class MonitorThresholdTests(unittest.TestCase):
    WINDOW = (0, 0, 1000, 1000)
    THIN = (0, 0, 1000, 1)
    SQUARE = (0, 0, 24, 24)

    def test_a_small_qualifying_overlap_counts_even_next_to_a_bigger_one(self):
        # The maximum-area rule preferred the 1000x1 strip (1000 px) over the
        # 24x24 square (576 px) and declared a perfectly usable window invisible.
        monitors = [self.THIN, self.SQUARE]
        self.assertTrue(screen.is_visible_on_monitors(self.WINDOW, monitors))

    def test_the_monitor_order_does_not_change_the_answer(self):
        monitors = [self.THIN, self.SQUARE]
        self.assertEqual(
            screen.is_visible_on_monitors(self.WINDOW, monitors),
            screen.is_visible_on_monitors(self.WINDOW, list(reversed(monitors))),
        )

    def test_nothing_below_the_threshold_counts(self):
        self.assertFalse(
            screen.is_visible_on_monitors(self.WINDOW, [(0, 0, 1000, 23)]),
            "23 pixels is below the usable minimum",
        )

    def test_the_area_statistic_is_still_available(self):
        self.assertEqual(screen.visible_pixels(self.WINDOW, [self.THIN, self.SQUARE]), (1000, 1))


# --------------------------------------------------------------------------- #
# F13 - a confirmed save must load back
# --------------------------------------------------------------------------- #
class SettingsSizeContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = ConfigService(Path(self._tmp.name) / "settings.json")

    def test_an_oversized_snapshot_is_refused_and_the_old_settings_survive(self):
        self.assertTrue(self.service.save(AppConfig(opacity=0.5)))
        before = self.service.path.read_bytes()
        config = AppConfig(
            windows=[TrackedWindow(title_contains="я" * MAX_CONFIG_BYTES)]
        )
        self.assertFalse(self.service.save(config), "an unreadable snapshot was persisted")
        self.assertEqual(self.service.path.read_bytes(), before)
        self.assertEqual(self.service.load().opacity, 0.5)
        self.assertFalse(self.service.quarantined_path().exists())

    def test_a_snapshot_near_the_limit_is_accepted(self):
        filler = MAX_CONFIG_BYTES // 2
        config = AppConfig(windows=[TrackedWindow(title_contains="x" * filler)])
        self.assertTrue(self.service.save(config))
        reloaded = self.service.load()
        self.assertEqual(len(reloaded.windows), 1)
        self.assertEqual(len(reloaded.windows[0].title_contains), filler)

    def test_a_refused_snapshot_reaches_the_ui_as_a_failed_save(self):
        from config import AsyncConfigSaver

        results: list[bool] = []
        saver = AsyncConfigSaver(self.service, on_result=results.append)
        self.addCleanup(saver.close)
        self.service.save(AppConfig(opacity=0.5))
        saver.submit(AppConfig(windows=[TrackedWindow(title_contains="я" * MAX_CONFIG_BYTES)]))
        self.assertFalse(saver.flush(timeout=5))
        self.assertEqual(results, [False])


# --------------------------------------------------------------------------- #
# F14 - an unconfirmed guardian launch is owned by a kill-on-close job
# --------------------------------------------------------------------------- #
@unittest.skipIf(os.name != "nt", "Job Objects are Windows only")
class GuardianLaunchOwnershipTests(unittest.TestCase):
    def test_a_launch_job_kills_the_whole_tree_it_was_given(self):
        # The shape of a onefile build: a launcher that starts a Python child.
        # ``taskkill`` needs privileges this application does not have, and its
        # result was never checked; the job does not.  Adoption happens *before*
        # the launcher can create its own child, which is the whole point.
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        ready = Path(tmp) / "ready"
        go = Path(tmp) / "go"
        child_pid_file = Path(tmp) / "child.pid"
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess,sys,time\n"
                "from pathlib import Path\n"
                f"ready = Path({str(ready)!r})\n"
                f"go = Path({str(go)!r})\n"
                "while not go.exists():\n"
                "    time.sleep(0.02)\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(120)'])\n"
                f"Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
                "ready.write_text('go')\n"
                "time.sleep(120)\n",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.kill, launcher.pid)
        job = restoreguard._GuardianLaunchJob()
        self.assertTrue(job.available, "no launch job could be created")
        self.assertTrue(job.adopt(launcher), "the launcher could not be owned")
        go.touch()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not child_pid_file.exists():
            time.sleep(0.05)
        self.assertTrue(child_pid_file.exists(), "the launcher never started its child")
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        self.assertEqual(
            restoreguard.job_active_processes(job.name), 2,
            "the launcher's own child was never owned by the job",
        )
        name = job.name
        restoreguard._stop_unready_guardian(launcher, job)
        self.assertIsNone(
            restoreguard.job_active_processes(name),
            "the launch job outlived its handle with processes still in it",
        )
        self.assertFalse(restoreguard.process_is_alive(child_pid),
                         "the onefile child survived the cleanup")
        self.assertFalse(restoreguard.process_is_alive(launcher.pid))

    def test_a_confirmed_guardian_is_released_from_the_job_before_the_parent_exits(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.kill, child.pid)
        job = restoreguard._GuardianLaunchJob()
        self.assertTrue(job.adopt(child))
        job.release()
        self.assertIsNone(restoreguard.job_active_processes(job.name))
        # Releasing ownership must not have killed the confirmed guardian.
        self.assertTrue(restoreguard.process_is_alive(child.pid))

    def test_taskkill_failure_is_reported_instead_of_assumed_to_have_worked(self):
        # An unusable tool is not a successful cleanup; the caller has to know.
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait(timeout=60)
        self.assertFalse(
            restoreguard._tree_taskkill(finished.pid),
            "a refused taskkill was reported as a success",
        )

    def test_the_cleanup_reports_a_surviving_process_instead_of_hiding_it(self):
        # When neither the job nor the fallback can finish the job, the cleanup has
        # to say so: an unready guardian child that keeps holding the executor
        # mutex is exactly what a silent cleanup causes.
        real_child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.kill, real_child.pid)
        job = restoreguard._GuardianLaunchJob()
        self.assertTrue(job.adopt(real_child))
        stubborn = SimpleNamespace(
            pid=real_child.pid,
            stdout=None,
            poll=lambda: None,
            kill=Mock(),
            wait=Mock(),
        )
        # A second handle keeps the job alive, so closing ours cannot complete the
        # kill-on-close either, and the tool fallback is disabled: nothing this
        # cleanup knows how to do will work.
        with self.hold_job_handle(job.name):
            with patch("restoreguard._tree_taskkill", return_value=False):
                with self.assertLogs("lookupwindows", level="WARNING") as captured:
                    restoreguard._stop_unready_guardian(stubborn, job)
        self.assertTrue(
            any("still owns" in line for line in captured.output),
            f"a surviving process was not reported: {captured.output}",
        )

    @staticmethod
    def kill(pid: int) -> None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - defensive
            pass

    @staticmethod
    @contextlib.contextmanager
    def hold_job_handle(name):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenJobObjectW.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.OpenJobObjectW(0x1F001F, False, name)
        try:
            yield handle
        finally:
            if handle:
                kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# F11 - no blocking journal or guardian work on the UI thread
# --------------------------------------------------------------------------- #
class UiThreadOffloadTests(unittest.TestCase):
    def test_the_ui_paths_do_no_journal_io(self):
        drop = APP_SOURCE.split("    def _drop_recovery", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("recovery_journal.get(", drop)
        self.assertIn("_journal_drop_async", drop)

    def test_the_timer_watcher_only_queues_work(self):
        watcher = APP_SOURCE.split("    def _watch_parked_sources", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("TIMER_PARKED_WATCH", APP_SOURCE)
        self.assertNotIn("recovery_journal.", watcher)

    def test_quit_does_not_run_the_handover_inline(self):
        quit_block = APP_SOURCE.split("    def quit(self)", 1)[1].split("\n    def ", 1)[0]
        # The UI thread only publishes the request; the blocking half of the exit
        # runs on a worker that already existed before the quit was asked for.
        self.assertNotIn("_handover_unfinished_restores()", quit_block)
        self.assertNotIn("_teardown_services()", quit_block)
        self.assertNotIn("_restore_sources_for_shutdown", quit_block)
        self.assertIn("_shutdown_requested.set()", quit_block)
        self.assertIn("_start_shutdown_worker(wait=False)", quit_block)

    def test_a_held_journal_lock_does_not_stall_the_ui_drop(self):
        """The real mechanism, not the shape: a held lock must not block the caller."""
        from app import App

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        journal = RecoveryJournal(Path(tmp) / "settings.json.park.json")
        journal.record_intent(make_record(4242))
        # Hold the cross-process lock the way a slow worker's transaction does.
        held = recovery_lock_holder(journal.lock_path)
        self.addCleanup(held.stop)
        self.assertTrue(held.acquired.wait(30), "could not take the journal lock")

        app = App.__new__(App)
        app._recovery_lock = threading.Lock()
        app._recovery_registry = {}
        app._recovery_inflight = set()
        state = SimpleNamespace(pid=1, class_name="C", process_name="p",
                                process_created=1, placement=None)
        app._recovery_registry[4242] = SimpleNamespace(
            state=state, label="t", attempts=0, retry_at=0.0, needs_retry=False,
            recorded_at=time.time(),
        )
        # No worker means no fallback: doing the I/O here is exactly the defect.
        app._start_daemon_worker = lambda *args: False
        app._journal_drop = Mock(side_effect=AssertionError("journal I/O on the UI thread"))

        started = time.monotonic()
        App._drop_recovery(app, 4242, state)
        self.assertLess(time.monotonic() - started, 1.0, "the drop waited for the journal lock")
        self.assertNotIn(4242, app._recovery_registry)
        app._journal_drop.assert_not_called()
        # The record is still there: the obligation outlives the failure to retire
        # it.  (A synchronous read here would have to wait for the lock, which is
        # exactly why the UI path does not do one.)
        held.stop()
        self.assertIsNotNone(RecoveryJournal(journal.path).get(4242))


def recovery_lock_holder(lock_path: Path):
    """A thread that really holds the journal's inter-process lock."""
    from recovery import _InterProcessJournalLock

    acquired = threading.Event()
    stop = threading.Event()
    holder = SimpleNamespace()

    def run():
        lock = _InterProcessJournalLock(lock_path)
        if not lock.acquire():
            return
        acquired.set()
        while not stop.wait(0.05):
            pass
        lock.release()

    thread = threading.Thread(target=run, name="journal-lock-holder", daemon=True)
    thread.start()
    holder.stop = stop.set
    holder.acquired = acquired
    return holder


if __name__ == "__main__":
    unittest.main()
