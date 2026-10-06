"""Regressions for the second wave of release-integrity fixes."""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TOOLS = ROOT / "tools"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import config  # noqa: E402
import restoreguard  # noqa: E402
import source_contract  # noqa: E402

APP = (SRC / "app.py").read_text(encoding="utf-8")
GUARD = (SRC / "restoreguard.py").read_text(encoding="utf-8")
PACKAGE = (TOOLS / "package_source.ps1").read_text(encoding="utf-8")
MANIFEST = (TOOLS / "release_manifest.py").read_text(encoding="utf-8")
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def method_source(source: str, class_name: str, name: str) -> str:
    tree = ast.parse(source)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    fn = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    return ast.get_source_segment(source, fn) or ""


class RuntimeSafetyRegressions(unittest.TestCase):
    def test_supervisor_waits_after_successful_guardian_spawn(self):
        body = method_source(APP, "App", "_recovery_supervisor_worker")
        branch = body.split("if self._start_session_guardian():", 1)[1].split("attempt += 1", 1)[0]
        self.assertIn("stop.wait(SUPERVISOR_IDLE_POLL_SEC)", branch)

    def test_parked_watch_respects_retry_backoff(self):
        body = method_source(APP, "App", "_watch_parked_sources")
        self.assertIn("entry.needs_retry and entry.retry_at > now", body)
        self.assertIn("_notify_restore_failure_throttled", APP)

    def test_auto_refind_does_not_enumerate_desktop_on_ui_refresh(self):
        refresh = method_source(APP, "App", "_do_refresh")
        request = method_source(APP, "App", "_request_refind_candidates")
        self.assertNotIn("self.finder.list_windows()", refresh)
        self.assertIn("threading.Thread", request)
        self.assertIn("self.finder.list_windows()", request)

    def test_damage_resolver_does_not_raise_when_second_read_fails(self):
        class Journal:
            path = Path("broken.park.json")

            def snapshot(self):
                raise restoreguard.JournalReadError("second read failed")

        result = restoreguard.resolve_journal_damage(Journal(), module=object())
        self.assertFalse(result.decided)
        self.assertIn("could not be re-read", result.reason)

    def test_handover_confirmation_requires_named_owner(self):
        confirm = GUARD.split("def _confirm_handover(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if not owner:", confirm)
        run = GUARD.split("def run_guardian(", 1)[1]
        self.assertIn("waited_for_handover", run)
        timeout_branch = run.split("if waited_for_handover:", 1)[1].split("# Normal startup only", 1)[0]
        self.assertNotIn("delegate journal=", timeout_branch)


class PersistenceRegressions(unittest.TestCase):
    def test_backup_replacement_is_atomic_and_failure_keeps_previous_backup(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            service = config.ConfigService(path)
            one = config.AppConfig(opacity=0.41)
            two = config.AppConfig(opacity=0.52)
            three = config.AppConfig(opacity=0.63)
            self.assertTrue(service.save(one))
            self.assertTrue(service.save(two))
            backup = service.backup_path()
            before = backup.read_bytes()
            real_replace = config.os.replace

            def fail_backup_replace(src, dst):
                if Path(dst) == backup:
                    raise OSError("injected backup replace failure")
                return real_replace(src, dst)

            with patch.object(config.os, "replace", side_effect=fail_backup_replace):
                self.assertFalse(service.save(three))
            self.assertEqual(backup.read_bytes(), before)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(float(loaded["opacity"]), 0.52)


class ReleaseIntegrityRegressions(unittest.TestCase):
    def test_packaging_and_manifest_share_one_source_contract(self):
        self.assertIn("source_contract.py", PACKAGE)
        self.assertIn("source_relative_paths", MANIFEST)
        paths = source_contract.source_relative_paths(ROOT)
        self.assertIn("LICENSE", paths)
        self.assertIn("NOTICE", paths)
        self.assertIn(".gitattributes", paths)
        self.assertNotIn("config/settings.json", paths)
        self.assertNotIn("_cleanup_after_patch.bat", paths)

    def test_source_zip_has_fixed_metadata(self):
        self.assertIn("date_time=(1980, 1, 1, 0, 0, 0)", PACKAGE)
        self.assertIn("ZIP_STORED", PACKAGE)
        self.assertNotIn("handle.write(path,", PACKAGE)

    def test_manifest_refuses_dirty_tracked_tree(self):
        self.assertIn("git status", MANIFEST)
        self.assertIn("--untracked-files=no", MANIFEST)
        self.assertIn("tracked files differ from HEAD", MANIFEST)
        self.assertIn('evidence.get("revision") != current_revision', MANIFEST)

    def test_release_gate_evidence_comes_from_success_markers(self):
        self.assertIn('Get-ChildItem -LiteralPath "build/release-gates" -Filter "*.ok"', RELEASE)
        self.assertNotIn('$gates = [ordered]@{', RELEASE)
        for name in (
            "ruff",
            "pytest",
            "check_source_imports",
            "check_build_env_release",
            "verify_frozen_runtime",
            "runtime_smoke_all",
            "verify_source_archive",
        ):
            self.assertIn(f"build/release-gates/{name}.ok", RELEASE)


if __name__ == "__main__":
    unittest.main()
