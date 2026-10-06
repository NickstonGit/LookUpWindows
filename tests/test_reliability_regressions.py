"""Failure paths and process boundaries behind the production recovery fixes."""

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_production_safety_gates import (
    AppMethod, FakeWinapi, JournalCase, ParkIdentityTests, SweepResult, executor, make_record,
)

import recovery
import restoreguard
import winapi

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import release_manifest  # noqa: E402


class DamageConcurrencyTests(JournalCase):
    def test_a_new_park_cannot_replace_a_damaged_obligation_for_the_same_window(self):
        self.plant_broken_entry()
        journal = recovery.RecoveryJournal(self.path)
        self.assertFalse(journal.record_intent(make_record(102, operation_id="new-operation")))
        self.assertEqual(journal.snapshot().outstanding_hwnds(), (101, 102))

    def test_a_malformed_non_object_survives_unrelated_mutations(self):
        self.plant_broken_entry()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["records"].append("unrecognizable obligation")
        self.path.write_text(json.dumps(document), encoding="utf-8")
        journal = recovery.RecoveryJournal(self.path)
        claim = journal.claim(101, executor())
        self.assertTrue(journal.clear_claimed(claim))
        self.assertTrue(recovery.RecoveryJournal(self.path).snapshot().damaged)
        durable = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn({"unparsed": "unrecognizable obligation"}, durable["damaged"])

    def test_another_reader_observes_a_committed_acknowledgement(self):
        self.plant_broken_entry()
        reader = recovery.RecoveryJournal(self.path)
        self.assertTrue(reader.snapshot().damaged)
        writer = recovery.RecoveryJournal(self.path)
        writer.snapshot()
        self.assertTrue(writer.acknowledge_damage())
        self.assertFalse(reader.snapshot().damaged)

    def test_a_sweep_cannot_acknowledge_damage_added_after_its_snapshot(self):
        self.path.write_text("broken", encoding="utf-8")
        self.journal.reload()
        layer = FakeWinapi(parked=False)

        def sweep(exclude=()):
            self.plant_broken_entry()
            return SweepResult()

        layer.recover_all_orphaned_parks = sweep
        result = restoreguard.resolve_journal_damage(self.journal, module=layer)
        self.assertFalse(result.decided)
        self.assertTrue(recovery.RecoveryJournal(self.path).snapshot().damaged)
        self.assertEqual(self.journal.snapshot().outstanding_hwnds(), (101, 102))

    def test_a_legacy_list_answer_cannot_prove_an_empty_desktop(self):
        self.path.write_text("broken", encoding="utf-8")
        self.journal.reload()
        layer = FakeWinapi(parked=False)
        layer.recover_all_orphaned_parks = lambda exclude=(): []
        self.assertFalse(restoreguard.resolve_journal_damage(self.journal, module=layer).decided)
        self.assertTrue(recovery.RecoveryJournal(self.path).snapshot().damaged)


class ProcessMarkTests(unittest.TestCase):
    setUp = ParkIdentityTests.setUp
    destroy = ParkIdentityTests.destroy
    def test_a_mark_written_by_a_process_is_readable_after_that_process_exits(self):
        operation = "00112233445566778899aabbccddeeff"
        script = (
            "import sys; sys.path.insert(0, 'src'); import winapi; "
            f"assert winapi.apply_park_operation({self.hwnd}, '{operation}')"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(winapi.park_operation_id(self.hwnd), operation)
        winapi.clear_park_operation(self.hwnd)
        self.assertEqual(winapi.park_operation_id(self.hwnd), "")

    def test_failure_to_stamp_the_mark_refuses_the_move(self):
        before = winapi.get_window_rect(self.hwnd)
        with patch.object(winapi, "apply_park_operation", return_value=False):
            self.assertIsNone(winapi.park_window_offscreen_sync(self.hwnd))
        self.assertEqual(winapi.get_window_rect(self.hwnd), before)


class SweepProbeFailureTests(unittest.TestCase):
    def test_a_failed_geometry_probe_keeps_the_sweep_undecided(self):
        with patch.object(winapi, "enumerate_recovery_windows", return_value=((77,), True)), patch.object(
            winapi, "is_window", return_value=True
        ), patch.object(winapi, "get_pid", return_value=1234), patch.object(
            winapi, "query_window_rect", return_value=None
        ):
            result = winapi.recover_all_orphaned_parks()
        self.assertEqual(result.unknown, (77,))
        self.assertFalse(result.decided)

    def test_a_failed_monitor_query_cannot_invent_visible_pixels(self):
        with patch.object(winapi, "_monitor_cache", (0.0, ())), patch.object(
            winapi.user32, "EnumDisplayMonitors", return_value=False
        ):
            self.assertEqual(winapi.display_monitor_rects(), ())


class ShutdownFailureTests(unittest.TestCase):
    def test_a_failed_service_teardown_defers_retry_and_never_exits(self):
        finish, retry = Mock(), Mock()
        app = SimpleNamespace(
            _shutdown_worker_ready=threading.Event(), _shutdown_worker_started=threading.Event(),
            _shutdown_requested=threading.Event(), _defer_closed=False,
            _teardown_services=Mock(side_effect=OSError("busy journal")),
            _handover_unfinished_restores=Mock(),
            _finish_shutdown=finish, _retry_shutdown=retry,
            defer=lambda fn, *args: fn(*args),
        )
        app._shutdown_requested.set()
        AppMethod("_shutdown_worker", {"logger": Mock()})(app)
        finish.assert_not_called()
        retry.assert_called_once()
        self.assertFalse(app._shutdown_worker_ready.is_set())

    def test_two_requests_before_readiness_start_only_one_worker(self):
        app = SimpleNamespace(
            _shutdown_lock=threading.Lock(), _shutdown_worker_ready=threading.Event(),
            _shutdown_worker_started=threading.Event(),
            _start_daemon_worker=Mock(return_value=True), _shutdown_worker=Mock(),
        )
        start = AppMethod("_start_shutdown_worker", {"logger": Mock()})
        self.assertTrue(start(app, wait=False))
        self.assertTrue(start(app, wait=False))
        self.assertEqual(app._start_daemon_worker.call_count, 1)

    def test_a_failed_thread_start_does_not_run_recovery_on_ui(self):
        app = SimpleNamespace(
            _shutting_down=False, _start_shutdown_worker=Mock(return_value=False),
            _notify_shutdown_unavailable=Mock(), _request_recovery_supervisor_from_timer=Mock(),
            _handover_unfinished_restores=Mock(), _teardown_services=Mock(),
        )
        AppMethod("quit", {})(app)
        app._start_shutdown_worker.assert_called_once_with(wait=False)
        app._notify_shutdown_unavailable.assert_called_once()
        app._handover_unfinished_restores.assert_not_called()
        app._teardown_services.assert_not_called()


class ManifestIntegrityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.joinpath("dist").mkdir()
        self.root.joinpath("src").mkdir()
        self.root.joinpath("src/app.py").write_text("# source\n", encoding="utf-8")
        for name in release_manifest.ARTIFACTS:
            self.root.joinpath(name).write_bytes(b"validated bytes")
        self.gates = self.root / "gates.json"
        self.gates.write_text(json.dumps(dict.fromkeys(release_manifest.REQUIRED_GATES, True)))
        patched = patch.object(release_manifest, "ROOT", self.root)
        patched.start()
        self.addCleanup(patched.stop)
        # The sandbox is not a checkout, so the clean-revision precondition has to
        # be stood in for here; test_release_gates.py covers the real git wiring.
        # The fingerprint stays real, over the sandbox sources only, so that
        # changing a source still invalidates the attestation.
        original_tree_problem = release_manifest.git_tree_problem
        original_revision = release_manifest.git_revision
        original_fingerprint = release_manifest.source_fingerprint
        release_manifest.git_tree_problem = lambda: None
        release_manifest.git_revision = lambda: "deadbeef"
        release_manifest.source_fingerprint = self._sandbox_fingerprint
        self.addCleanup(setattr, release_manifest, "git_tree_problem", original_tree_problem)
        self.addCleanup(setattr, release_manifest, "git_revision", original_revision)
        self.addCleanup(setattr, release_manifest, "source_fingerprint", original_fingerprint)
        self.assertEqual(release_manifest.record_gates(self.gates), 0)
        self.assertEqual(release_manifest.write(self.gates), 0)

    def _sandbox_fingerprint(self):
        files = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.joinpath("src").rglob("*")
            if path.is_file()
        )
        combined = hashlib.sha256(
            "\n".join(
                f"{name} {release_manifest.sha256_of(self.root / name)}" for name in files
            ).encode("utf-8")
        ).hexdigest()
        return {"digest": combined, "fileCount": len(files)}

    def test_a_valid_release_pair_verifies(self):
        self.assertEqual(release_manifest.verify(), 0)

    def test_changed_artifact_cannot_reuse_old_gate_evidence(self):
        self.root.joinpath(release_manifest.EXE_NAME).write_bytes(b"rebuilt")
        self.assertEqual(release_manifest.write(self.gates), 1)
        self.assertEqual(release_manifest.verify(), 1)

    def test_an_empty_artifact_list_cannot_verify(self):
        path = release_manifest.manifest_path()
        document = json.loads(path.read_text())
        document["artifacts"] = []
        path.write_text(json.dumps(document))
        self.assertEqual(release_manifest.verify(), 1)

    def test_changed_sources_invalidate_the_attestation(self):
        self.root.joinpath("src/app.py").write_text("# changed\n", encoding="utf-8")
        self.assertEqual(release_manifest.verify(), 1)

    def test_rebuild_invalidation_removes_both_sidecars_and_manifest(self):
        self.assertEqual(release_manifest.invalidate(), 0)
        self.assertFalse(release_manifest.manifest_path().exists())
        for name in release_manifest.ARTIFACTS:
            self.assertFalse(self.root.joinpath(name + release_manifest.CHECKSUM_SUFFIX).exists())


del ParkIdentityTests  # The original suite already collects these tests.
