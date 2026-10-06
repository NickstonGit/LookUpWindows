import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from config import AppConfig, AsyncConfigSaver, ConfigService
from recovery import RecoverySnapshot
from restoreguard import DamageResolution

ROOT = Path(__file__).resolve().parent.parent


def method(path, class_name, method_name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method_name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace[method_name]


def snapshot(records=(), status="valid", damage=""):
    """The one answer a shutdown decision may be taken on."""
    return RecoverySnapshot(records=tuple(records), status=status, damage=damage)


class ProductionRegressionTests(unittest.TestCase):
    def test_config_worker_survives_a_failed_write_and_can_save_again(self):
        service = SimpleNamespace(save_dict=Mock(side_effect=[RuntimeError("disk filter"), True]))
        saver = AsyncConfigSaver(service)
        self.addCleanup(saver.close)
        saver.submit(AppConfig())
        self.assertFalse(saver.flush(timeout=2))
        saver.submit(AppConfig(opacity=0.5))
        self.assertTrue(saver.flush(timeout=2))
        self.assertEqual(service.save_dict.call_count, 2)

    def test_custom_config_does_not_import_unrelated_legacy_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            legacy = base / "config" / "settings.json"
            self.assertTrue(ConfigService(legacy).save(AppConfig(opacity=0.42)))
            explicit = base / "isolated" / "settings.json"
            with patch("config.app_dir", return_value=base):
                self.assertEqual(ConfigService(explicit).load().opacity, 0.95)
            self.assertFalse(explicit.exists())

    def test_tray_menu_is_queued_until_the_safe_bridge_runs(self):
        callback = Mock()
        pending = []
        tray = SimpleNamespace(_taskbar_created=0, _show_menu=callback,
                               defer=lambda fn: pending.append(fn))
        namespace = {"WM_TRAYICON": 1, "WM_LBUTTONDBLCLK": 2, "WM_RBUTTONUP": 3}
        handle = method("src/trayicon.py", "TrayIcon", "_handle_message", namespace)
        self.assertEqual(handle(tray, 99, 1, 0, 3), 0)
        callback.assert_not_called()
        pending.pop()()
        callback.assert_called_once()

    def test_filter_typing_uses_cached_candidates(self):
        finder = SimpleNamespace(list_windows=Mock(side_effect=AssertionError("enumeration during typing")))
        candidate = SimpleNamespace(process_name="code.exe", title="project")
        dialog = SimpleNamespace(finder=finder, candidates=[candidate], shown=[],
                                 filter_var=SimpleNamespace(get=lambda: "proj"),
                                 listbox=SimpleNamespace(delete=Mock(), insert=Mock()))
        filtering = method("src/app.py", "WindowSelectorDialog", "_filter", {})
        filtering(dialog)
        self.assertEqual(dialog.shown, [candidate])
        finder.list_windows.assert_not_called()

    def test_worker_is_registered_before_os_thread_start(self):
        begin, end = Mock(), Mock()
        app = SimpleNamespace(_begin_source_action=begin, _end_source_action=end)
        thread = Mock()
        factory = Mock(return_value=thread)
        thread.start.side_effect = lambda: begin.assert_called_once()
        namespace = {"threading": SimpleNamespace(Thread=factory), "logger": Mock()}
        launch = method("src/app.py", "App", "_start_daemon_worker", namespace)
        target = Mock()
        self.assertTrue(launch(app, "park", target, (123,)))
        end.assert_not_called()
        factory.call_args.kwargs["target"]()
        target.assert_called_once_with(123)
        end.assert_called_once()

    def test_failed_thread_start_releases_shutdown_barrier(self):
        begin, end = Mock(), Mock()
        thread = SimpleNamespace(start=Mock(side_effect=RuntimeError("no thread")))
        app = SimpleNamespace(_begin_source_action=begin, _end_source_action=end)
        namespace = {"threading": SimpleNamespace(Thread=Mock(return_value=thread)), "logger": Mock()}
        launch = method("src/app.py", "App", "_start_daemon_worker", namespace)
        self.assertFalse(launch(app, "park", Mock(), ()))
        begin.assert_called_once()
        end.assert_called_once()

    def test_failed_guardian_launch_does_not_authorize_exit_with_parked_windows(self):
        journal = SimpleNamespace(
            release_all=Mock(),
            snapshot=Mock(return_value=snapshot([SimpleNamespace(hwnd=123, label="target")])),
            path="journal",
            identity="journal-id",
            status="valid",
            damage_reason="",
            damaged=False,
        )
        # The handover passes this run's full identity to the guardian: a PID alone
        # would let a recycled PID convince the guardian that its partner is alive.
        executor = SimpleNamespace(executor_id="run-id", pid=1234, created=999)
        app = SimpleNamespace(
            recovery_journal=journal,
            tray=None,
            _executor=executor,
            defer=lambda *args: None,
            _recovery_guardian=None,
            _supervisor_has_live_executor=lambda: False,
        )
        spawn = Mock(return_value=None)
        namespace = {"os": SimpleNamespace(getpid=lambda: 1234), "logger": Mock(),
                     "RESTORE_HANDOVER_TIMEOUT_SEC": 1, "RESTORE_FALLBACK_WAIT_SEC": 1,
                     "restoreguard": SimpleNamespace(
                         spawn_guardian=spawn,
                         wait_for_handover=Mock(return_value=False),
                         resolve_journal_damage=Mock(return_value=DamageResolution(decided=True)))}
        handover = method("src/app.py", "App", "_handover_unfinished_restores", namespace)
        self.assertFalse(handover(app))
        # The guardian is started with the run id and the creation time, not a bare PID.
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.kwargs["owner_run_id"], "run-id")
        self.assertEqual(spawn.call_args.kwargs["owner_created"], 999)
        self.assertEqual(spawn.call_args.kwargs["owner_pid"], 1234)

    def test_a_damaged_journal_is_swept_before_the_shutdown_decides(self):
        # Quarantine keeps the file, not the obligation.  The handover must ask
        # the shared executor to resolve the damage first, or it would read an
        # emptied journal as "nothing outstanding" and let the process exit.
        journal = SimpleNamespace(
            release_all=Mock(),
            snapshot=Mock(return_value=snapshot(status="valid")),
            path="journal",
            identity="journal-id",
            status="unresolved",
            damage_reason="document is not valid JSON",
            damaged=True,
        )
        app = SimpleNamespace(
            recovery_journal=journal,
            tray=None,
            _executor=SimpleNamespace(executor_id="run-id", pid=1234, created=999),
            defer=lambda *args: None,
            _notify_damaged_journal=Mock(),
            _recovery_guardian=None,
            _supervisor_has_live_executor=lambda: False,
        )
        sweep = Mock(return_value=DamageResolution(recovered=(4242,), decided=True))
        namespace = {"os": SimpleNamespace(getpid=lambda: 1234), "logger": Mock(),
                     "RESTORE_HANDOVER_TIMEOUT_SEC": 1, "RESTORE_FALLBACK_WAIT_SEC": 1,
                     "restoreguard": SimpleNamespace(spawn_guardian=Mock(), wait_for_handover=Mock(),
                                                     resolve_journal_damage=sweep)}
        handover = method("src/app.py", "App", "_handover_unfinished_restores", namespace)
        self.assertTrue(handover(app), "the shutdown was blocked by damage it had already resolved")
        sweep.assert_called_once_with(journal)
        # The decision to exit is taken from the snapshot that happened after the sweep.
        self.assertEqual(journal.snapshot.call_count, 1)

    def test_unresolved_damage_is_handed_over_instead_of_being_dropped(self):
        # A sweep that restored nothing is not an answer: the successor has to keep
        # retrying, so the handover gives it the work instead of exiting quietly.
        journal = SimpleNamespace(
            release_all=Mock(),
            snapshot=Mock(return_value=snapshot(status="degraded", damage="2 unusable record(s)")),
            path="journal",
            identity="journal-id",
            status="degraded",
            damage_reason="2 unusable record(s)",
            damaged=True,
        )
        app = SimpleNamespace(
            recovery_journal=journal,
            tray=None,
            _executor=SimpleNamespace(executor_id="run-id", pid=1234, created=999),
            defer=lambda *args: None,
            _recovery_guardian=None,
            _supervisor_has_live_executor=lambda: False,
        )
        guardian = SimpleNamespace(pid=4321)
        spawn = Mock(return_value=guardian)
        namespace = {"os": SimpleNamespace(getpid=lambda: 1234), "logger": Mock(),
                     "RESTORE_HANDOVER_TIMEOUT_SEC": 1, "RESTORE_FALLBACK_WAIT_SEC": 1,
                     "restoreguard": SimpleNamespace(
                         spawn_guardian=spawn,
                         wait_for_handover=Mock(),
                         resolve_journal_damage=Mock(return_value=DamageResolution(pending=(123,))))}
        handover = method("src/app.py", "App", "_handover_unfinished_restores", namespace)
        self.assertTrue(handover(app))
        spawn.assert_called_once()
        self.assertEqual(app._recovery_guardian, guardian)

    def test_guardian_watcher_can_be_joined(self):
        # Thread.join() calls its own _stop() method; a same-named Event breaks it.
        tree = ast.parse((ROOT / "tools/runtime_smoke.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GuardianWatcher")
        import threading
        namespace = {"threading": threading, "guardian_pids": lambda: []}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), "watcher", "exec"), namespace)
        watcher = namespace["GuardianWatcher"]()
        watcher.start()
        self.assertEqual(watcher.stop(), set())

    def test_startup_keeps_management_accessible_without_tray(self):
        panel = SimpleNamespace(hwnd=99)
        root = SimpleNamespace(after=Mock(return_value="timer"))
        import threading

        app = SimpleNamespace(tray=None, panel=panel, root=root, show_panel=Mock(),
                              apply_config=Mock(), _recover_journaled_sources=Mock(),
                              _pump_deferred=Mock(),
                              _recovery_supervisor_wanted=threading.Event(),
                              _ensure_recovery_supervisor=Mock(),
                              _start_shutdown_worker=Mock(return_value=True))
        namespace = {"background_mode": lambda: True, "winui": SimpleNamespace(set_timer=Mock()),
                     "TIMER_STARTUP": 5}
        start = method("src/app.py", "App", "start", namespace)
        start(app)
        app.show_panel.assert_called_once()
        # A start that skipped the supervisor or the pre-existing shutdown worker
        # would leave obligations without an executor, or make a later exit perform
        # recovery work on the UI thread.
        app._ensure_recovery_supervisor.assert_called_once()
        app._start_shutdown_worker.assert_called_once_with(wait=False)

    def test_forgotten_hwnd_tokens_are_bounded_and_reject_late_results(self):
        import queue
        import threading
        from collections import deque
        from dataclasses import make_dataclass

        request = make_dataclass("Request", ["generation", "reset_token", "hwnd", "submitted_at"])
        detector = SimpleNamespace(_state_lock=threading.Lock(), _closed=False, _generation=0,
                                   _reset_tokens={}, _reset_sequence=0, _pending_since={},
                                   _requests=deque(), _resets=set(), _wake=Mock(), _results=queue.Queue())
        namespace = {"time": SimpleNamespace(monotonic=lambda: 1), "_CaptureRequest": request,
                     "queue": queue}
        schedule = method("src/winapi.py", "AsyncChangeDetector", "schedule", namespace)
        forget = method("src/winapi.py", "AsyncChangeDetector", "forget", namespace)
        poll = method("src/winapi.py", "AsyncChangeDetector", "poll_results", namespace)
        schedule(detector, [123])
        old = detector._requests[-1]
        forget(detector, 123)
        detector._pending_since.clear()
        schedule(detector, [123])
        current = detector._requests[-1]
        self.assertGreater(current.reset_token, old.reset_token)
        detector._results.put((0, old.reset_token, 123, "old frame"))
        detector._results.put((0, current.reset_token, 123, "new frame"))
        self.assertEqual(poll(detector), [(123, "new frame")])
        for hwnd in range(1, 10000):
            forget(detector, hwnd)
        self.assertEqual(detector._reset_tokens, {})
