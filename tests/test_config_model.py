import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import config
from config import AppConfig, AsyncConfigSaver, Autostart, ConfigService, CropRect, TrackedWindow


class CropRectTests(unittest.TestCase):
    def test_absolute_crop_clamps(self):
        crop = CropRect(x=90, y=40, width=30, height=20)
        resolved = crop.clamped(100, 50)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.as_rect(), (90, 40, 100, 50))

    def test_relative_crop_resolves_after_resize(self):
        crop = CropRect(x=0.1, y=0.2, width=0.5, height=0.4, mode="relative")
        first = crop.clamped(1000, 500)
        second = crop.clamped(2000, 1000)
        self.assertEqual(first.as_rect(), (100, 100, 600, 300))
        self.assertEqual(second.as_rect(), (200, 200, 1200, 600))

    def test_relative_roundtrip(self):
        crop = CropRect(x=0.125, y=0.25, width=0.5, height=0.5, mode="relative")
        loaded = CropRect.from_dict(crop.to_dict())
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.is_relative())
        self.assertAlmostEqual(loaded.x, 0.125)

    def test_nonfinite_crop_is_rejected(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            crop = CropRect(x=0, y=0, width=value, height=10)
            self.assertFalse(crop.is_valid())
            self.assertIsNone(crop.clamped(100, 100))

    def test_huge_crop_number_does_not_overflow(self):
        loaded = CropRect.from_dict({"x": 0, "y": 0, "width": 10**1000, "height": 10})
        self.assertIsNone(loaded)


class ConfigTests(unittest.TestCase):
    def test_tracked_window_roundtrip(self):
        tracked = TrackedWindow(
            process="1cv8c.exe",
            title_contains="Nomination",
            crop=CropRect(x=0.1, y=0.1, width=0.8, height=0.8, mode="relative"),
            x=20,
            y=30,
            width=420,
            collapsed=True,
            click_through=True,
            detect_changes=False,
            pip_enabled=False,
            title_hint="Nomination - 1C",
            class_hint="V8TopLevelFrame",
        )
        loaded = TrackedWindow.from_dict(tracked.to_dict())
        self.assertEqual(loaded.process, tracked.process)
        self.assertEqual(loaded.width, 420)
        self.assertTrue(loaded.collapsed)
        self.assertTrue(loaded.click_through)
        self.assertFalse(loaded.detect_changes)
        self.assertFalse(loaded.pip_enabled)
        self.assertEqual(loaded.title_hint, "Nomination - 1C")
        self.assertEqual(loaded.class_hint, "V8TopLevelFrame")
        self.assertTrue(loaded.crop.is_relative())

    def test_old_card_config_defaults_detector_on(self):
        loaded = TrackedWindow.from_dict({"process": "mstsc.exe", "titleContains": "srv"})
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.detect_changes)
        self.assertTrue(loaded.pip_enabled)

    def test_disabled_pip_roundtrips_in_profiles(self):
        cfg = AppConfig(
            windows=[TrackedWindow(process="mstsc.exe", pip_enabled=False)],
            profiles={"RDP": [TrackedWindow(process="mstsc.exe", pip_enabled=False)]},
        )
        loaded = AppConfig.from_dict(cfg.to_dict())
        self.assertFalse(loaded.windows[0].pip_enabled)
        self.assertFalse(loaded.profiles["RDP"][0].pip_enabled)

    def test_large_card_width_is_preserved(self):
        loaded = TrackedWindow.from_dict({"process": "mstsc.exe", "width": 2400})
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.width, 2400)

    def test_profiles_roundtrip(self):
        cfg = AppConfig(
            windows=[TrackedWindow(process="mstsc.exe")],
            profiles={"RDP": [TrackedWindow(process="mstsc.exe", width=420)]},
        )
        loaded = AppConfig.from_dict(cfg.to_dict())
        self.assertIn("RDP", loaded.profiles)
        self.assertEqual(loaded.profiles["RDP"][0].width, 420)

    def test_backup_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            backup = path.with_suffix(path.suffix + ".bak")
            path.write_text("{broken", encoding="utf-8")
            backup.write_text(json.dumps(AppConfig(opacity=0.73).to_dict()), encoding="utf-8")
            loaded = ConfigService(path).load()
            self.assertAlmostEqual(loaded.opacity, 0.73)
            reparsed = json.loads(path.read_text(encoding="utf-8"))
            self.assertAlmostEqual(reparsed["opacity"], 0.73)

    def test_huge_numeric_config_falls_back(self):
        loaded = AppConfig.from_dict({"opacity": 10**1000, "changeIntervalSec": 10**1000})
        self.assertAlmostEqual(loaded.opacity, 0.95)
        self.assertAlmostEqual(loaded.change_interval_sec, 3.0)

    def test_nonfinite_json_is_rejected_and_backup_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            backup = path.with_suffix(path.suffix + ".bak")
            path.write_text('{"opacity": Infinity}', encoding="utf-8")
            backup.write_text(json.dumps(AppConfig(opacity=0.71).to_dict()), encoding="utf-8")
            loaded = ConfigService(path).load()
            self.assertAlmostEqual(loaded.opacity, 0.71)

    def test_save_rejects_nonfinite_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            service = ConfigService(path)
            self.assertFalse(service.save_dict({"opacity": math.nan}))
            self.assertFalse(path.exists())

    def test_async_saver_coalesces_and_flushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            saver = AsyncConfigSaver(ConfigService(path))
            try:
                self.assertTrue(saver.submit(AppConfig(opacity=0.61)))
                self.assertTrue(saver.submit(AppConfig(opacity=0.84)))
                self.assertTrue(saver.flush(timeout=2.0))
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertAlmostEqual(stored["opacity"], 0.84)
            finally:
                self.assertTrue(saver.close(timeout=2.0))

    def test_autostart_command_is_background(self):
        command = Autostart().run_command()
        self.assertIn("--background", command)


class SameTargetTests(unittest.TestCase):
    """Two windows of one process must be trackable side by side."""

    def test_same_window_is_still_rejected_as_duplicate(self):
        first = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1001)
        second = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1001)
        self.assertTrue(first.same_target(second))

    def test_two_identically_titled_windows_are_distinct_targets(self):
        first = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1001)
        second = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1002)
        self.assertFalse(first.same_target(second))

    def test_entries_without_live_binding_keep_legacy_dedup(self):
        legacy = TrackedWindow(process="Code.exe", title_contains="myproj")
        fresh = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1002)
        self.assertTrue(legacy.same_target(fresh))

    def test_different_process_or_title_is_never_the_same_target(self):
        base = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=1)
        self.assertFalse(base.same_target(TrackedWindow(process="devenv.exe", title_contains="myproj", source_hwnd=1)))
        self.assertFalse(base.same_target(TrackedWindow(process="Code.exe", title_contains="other", source_hwnd=1)))

    def test_live_binding_is_never_persisted(self):
        tracked = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=4242)
        stored = tracked.to_dict()
        self.assertNotIn("sourceHwnd", stored)
        self.assertNotIn("source_hwnd", stored)
        loaded = TrackedWindow.from_dict(stored)
        self.assertIsNotNone(loaded)
        self.assertIsNone(loaded.source_hwnd)

    def test_clone_keeps_the_live_binding(self):
        tracked = TrackedWindow(process="Code.exe", title_contains="myproj", source_hwnd=77)
        self.assertEqual(tracked.clone().source_hwnd, 77)


class SettingsPathArgumentTests(unittest.TestCase):
    """--config without a value is a mistake, not a request for the default."""

    def setUp(self):
        self._argv = sys.argv
        self.addCleanup(setattr, sys, "argv", self._argv)

    def test_an_empty_config_argument_is_refused(self):
        sys.argv = ["app.py", "--config"]
        with self.assertRaises(SystemExit):
            config.default_settings_path()

    def test_a_blank_config_argument_is_refused(self):
        sys.argv = ["app.py", "--config", "   "]
        with self.assertRaises(SystemExit):
            config.default_settings_path()

    def test_an_explicit_path_still_wins(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "custom.json"
            sys.argv = ["app.py", "--config", str(target)]
            self.assertEqual(config.default_settings_path(), target.resolve())


if __name__ == "__main__":
    unittest.main()
