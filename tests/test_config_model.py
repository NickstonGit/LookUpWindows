import json
import tempfile
import unittest
from pathlib import Path

from config import AppConfig, Autostart, ConfigService, CropRect, TrackedWindow


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
        )
        loaded = TrackedWindow.from_dict(tracked.to_dict())
        self.assertEqual(loaded.process, tracked.process)
        self.assertEqual(loaded.width, 420)
        self.assertTrue(loaded.collapsed)
        self.assertTrue(loaded.click_through)
        self.assertFalse(loaded.detect_changes)
        self.assertTrue(loaded.crop.is_relative())

    def test_old_card_config_defaults_detector_on(self):
        loaded = TrackedWindow.from_dict({"process": "mstsc.exe", "titleContains": "srv"})
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.detect_changes)

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

    def test_autostart_command_is_background(self):
        command = Autostart().run_command()
        self.assertIn("--background", command)


if __name__ == "__main__":
    unittest.main()
