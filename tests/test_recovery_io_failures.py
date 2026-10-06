"""Failed reads must never authorize forgetting or replacing park obligations."""
import ctypes
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import restoreguard  # noqa: E402 (after source-path bootstrap)
from recovery import Claim, ExecutorIdentity, JournalReadError, RecoveryJournal  # noqa: E402
from test_recovery_journal import make_record  # noqa: E402


def executor(tag: str = "a") -> ExecutorIdentity:
    return ExecutorIdentity(executor_id=f"exec-{tag}", pid=1000 + ord(tag), created=42, label=tag)


class JournalIOFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.assertTrue(self.journal.record_intent(make_record(1001, state="parked")))

    def test_every_mutation_refuses_an_unknown_disk_state(self):
        claim = ExecutorIdentity(executor_id="exec-a", pid=1001, created=42)
        mutations = (
            lambda: self.journal.record_intent(make_record(2002)),
            lambda: self.journal.mark_parked(1001, claim),
            lambda: self.journal.claim(1001, claim),
            lambda: self.journal.renew(Claim(1001, claim, 1, "token"), 30.0),
            lambda: self.journal.release(Claim(1001, claim, 1, "token")),
            lambda: self.journal.release_all(claim),
            lambda: self.journal.clear(1001),
            lambda: self.journal.clear_if(1001, 0),
            lambda: self.journal.clear_unless_claimed(1001, claim),
            lambda: self.journal.clear_claimed(Claim(1001, claim, 1, "token")),
        )
        before = self.path.read_bytes()
        for mutate in mutations:
            with self.subTest(mutate=mutate), patch.object(
                Path, "read_bytes", side_effect=PermissionError("transient read failure")
            ), patch("recovery.os.replace") as replace:
                self.assertFalse(mutate())
                replace.assert_not_called()
                self.assertEqual([r.hwnd for r in self.journal._snapshot()], [1001])
            self.assertEqual(self.path.read_bytes(), before)

    def test_strict_read_does_not_report_an_unreadable_journal_as_empty(self):
        fresh = RecoveryJournal(self.path)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")):
            for journal in (self.journal, fresh):
                with self.assertRaises(JournalReadError):
                    journal.reload()
                with self.assertRaises(JournalReadError):
                    journal.has_pending()
            with self.assertRaises(JournalReadError):
                self.journal.records()
            with self.assertRaises(JournalReadError):
                fresh.get(1001)
            self.assertEqual([r.hwnd for r in self.journal._snapshot()], [1001])
            self.assertFalse(restoreguard.wait_for_handover(fresh, timeout=0))

    def test_lock_failure_is_also_unknown_to_shutdown(self):
        with patch("recovery._InterProcessJournalLock.acquire", return_value=False):
            with self.assertRaises(JournalReadError):
                self.journal.reload()
            self.assertFalse(restoreguard.wait_for_handover(self.journal, timeout=0))

    def test_failed_quarantine_never_allows_overwriting_the_damaged_file(self):
        damaged = b"{not json"
        self.path.write_bytes(damaged)
        with patch("recovery.os.replace", side_effect=PermissionError("rename denied")):
            self.assertFalse(self.journal.record_intent(make_record(2002)))
            with self.assertRaises(JournalReadError):
                self.journal.reload()
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_startup_recovery_retries_a_failed_read(self):
        real_read = Path.read_bytes
        failed = False

        def transient_read(candidate):
            nonlocal failed
            if candidate == self.path and not failed:
                failed = True
                raise PermissionError("temporary outage")
            return real_read(candidate)

        with patch.object(Path, "read_bytes", transient_read), patch.object(
            restoreguard, "resolve", return_value=restoreguard.Outcome(1001, "restored", True)
        ) as resolve:
            result = restoreguard.resolve_all(self.journal, 123)
        self.assertEqual(result["restored"], 1)
        self.assertTrue(failed)
        resolve.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "real Win32 sharing violation")
    def test_transient_native_sharing_violation_cannot_erase_an_old_record(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.CreateFileW(str(self.path), 0x80000000, 6, None, 3, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        real_read = Path.read_bytes
        saw_failure = False

        def transient_read(candidate):
            nonlocal handle, saw_failure
            try:
                return real_read(candidate)
            except PermissionError:
                saw_failure = True
                raise
            finally:
                # Model a scanner releasing its handle after the failed read
                # and before the transaction could attempt its replacement.
                if candidate == self.path and handle:
                    kernel.CloseHandle(handle)
                    handle = None

        try:
            with patch.object(Path, "read_bytes", transient_read):
                self.assertFalse(self.journal.record_intent(make_record(2002)))
        finally:
            if handle:
                kernel.CloseHandle(handle)
        self.assertTrue(saw_failure)
        self.assertEqual([r.hwnd for r in self.journal.reload()], [1001])
        self.assertTrue(self.journal.record_intent(make_record(2002)))
        self.assertEqual(sorted(r.hwnd for r in self.journal.reload()), [1001, 2002])

    @unittest.skipUnless(os.name == "nt", "App requires Windows")
    def test_shutdown_keeps_the_main_alive_if_the_journal_cannot_be_read(self):
        from app import App

        instance = SimpleNamespace(
            recovery_journal=self.journal,
            _executor=executor("a"),
            tray=None,
        )
        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")), patch.object(
            restoreguard, "spawn_guardian"
        ) as spawn:
            self.assertFalse(App._handover_unfinished_restores(instance))
        spawn.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "App requires Windows")
    def test_a_refused_park_does_not_delete_an_older_obligation(self):
        from app import App

        instance = App.__new__(App)
        instance._shutting_down = False
        instance._journal_record_intent = lambda *args: False
        instance.defer = lambda *args: None
        with patch("app.winapi.park_window_offscreen_sync", side_effect=lambda hwnd, before_park: (
            None if not before_park(object()) else object()
        )), patch.object(App, "_journal_drop") as drop:
            App._park_source_worker(instance, SimpleNamespace(_title="test"), 1001, 1)
        drop.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "guardian singleton requires Windows")
    def test_an_unreadable_journal_never_ends_the_executor(self):
        # "I cannot read the journal" must never be reported as "there is nothing
        # to do": the application may already have exited on the handover, so an
        # executor that leaves here is one that will never come back.  A saved
        # JSON file is not a live executor.
        clock = iter(float(value) for value in range(100000))
        exits: list = []
        handovers: list[bool] = []
        stop = threading.Event()

        def hand_off(_self) -> bool:
            # The only remaining way out for this journal state; reaching it is
            # what makes the observed exit legitimate.
            handovers.append(True)
            return True

        def target() -> None:
            exits.append(restoreguard.run_guardian(self.path, announce=False, stop=stop))

        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")), patch.object(
            restoreguard.time, "monotonic", side_effect=lambda: next(clock)
        ), patch.object(restoreguard.time, "sleep"), patch.object(
            restoreguard, "POLL_SEC", 0.01
        ), patch.object(restoreguard._Guardian, "_hand_off_on_stop", hand_off):
            worker = threading.Thread(target=target, name="unreadable-guardian", daemon=True)
            worker.start()
            # Comfortably past the 30s that used to be the exit condition.
            time.sleep(2.0)
            self.assertTrue(worker.is_alive(), "the executor walked away from an unknown journal")
            self.assertEqual(exits, [], "the executor reported a result for an unreadable journal")
            self.assertEqual(handovers, [])
            stop.set()
            worker.join(timeout=30)
        self.assertFalse(worker.is_alive())
        self.assertEqual(exits, [0])
        self.assertEqual(len(handovers), 1, "the exit was not a confirmed handover")


if __name__ == "__main__":
    unittest.main()
