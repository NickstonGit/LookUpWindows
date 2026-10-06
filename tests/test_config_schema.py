"""Settings validation: JSON-valid but schema-invalid settings must not break startup or the backup."""

import json
import tempfile
import unittest
from pathlib import Path

from config import (
    AppConfig,
    ConfigSchemaError,
    ConfigService,
    TrackedWindow,
    settings_schema_problem,
    validate_settings_data,
)


class SchemaValidationTests(unittest.TestCase):
    def test_roundtripped_config_is_always_valid(self):
        config = AppConfig(
            opacity=0.5,
            windows=[TrackedWindow(process="mstsc.exe", pip_enabled=False)],
            profiles={"RDP": [TrackedWindow(process="mstsc.exe")]},
        )
        validate_settings_data(config.to_dict())

    def test_non_object_root_is_rejected(self):
        for payload in ([], "windows", 5, 1.5, True, None):
            with self.assertRaises(ConfigSchemaError):
                validate_settings_data(payload)

    def test_wrong_container_types_are_rejected(self):
        cases = [
            {"windows": 1},
            {"windows": "one.exe"},
            {"windows": {"process": "a.exe"}},
            {"profiles": []},
            {"profiles": {"RDP": 5}},
            {"windows": [1]},
            {"windows": [{"process": 5}]},
            {"windows": [{"process": "a.exe", "crop": 7}]},
            {"windows": [{"process": "a.exe", "crop": {"x": "left"}}]},
        ]
        for payload in cases:
            with self.assertRaises(ConfigSchemaError, msg=repr(payload)):
                validate_settings_data(payload)

    def test_wrong_scalar_types_are_rejected(self):
        cases = [
            {"opacity": "0.9"},
            {"opacity": True},
            {"changeIntervalSec": "3"},
            {"changeThreshold": None},
            {"alwaysOnTop": "true"},
            {"autostart": 1},
            {"ctrlX": 1.5},
            {"ctrlY": "1"},
            {"windows": [{"process": "a.exe", "collapsed": "false"}]},
            {"windows": [{"process": "a.exe", "width": "280"}]},
        ]
        for payload in cases:
            with self.assertRaises(ConfigSchemaError, msg=repr(payload)):
                validate_settings_data(payload)

    def test_unknown_keys_stay_tolerated(self):
        self.assertIsNone(settings_schema_problem({"someFutureKey": {"a": 1}}))

    def test_null_optional_scalars_are_valid(self):
        self.assertIsNone(settings_schema_problem({"ctrlX": None, "ctrlY": None, "windows": None}))

    def test_from_dict_never_raises_for_bad_containers(self):
        # AppConfig.from_dict stays tolerant for direct callers; only the file
        # loading path is strict.
        for payload in ({"windows": 1}, {"windows": "x"}, {"windows": [1, 2, 3]}):
            config = AppConfig.from_dict(payload)
            self.assertEqual(config.windows, [])


class StrictBooleanTests(unittest.TestCase):
    def test_truthy_strings_do_not_enable_flags(self):
        tracked = TrackedWindow.from_dict(
            {
                "process": "a.exe",
                "collapsed": "false",
                "clickThrough": "0",
                "detectChanges": "",
                "pipEnabled": "true",
            }
        )
        self.assertIsNotNone(tracked)
        self.assertFalse(tracked.collapsed)
        self.assertFalse(tracked.click_through)
        self.assertTrue(tracked.detect_changes)
        self.assertTrue(tracked.pip_enabled)

    def test_real_booleans_are_preserved(self):
        tracked = TrackedWindow.from_dict(
            {"process": "a.exe", "collapsed": True, "pipEnabled": False}
        )
        self.assertTrue(tracked.collapsed)
        self.assertFalse(tracked.pip_enabled)

    def test_config_level_strings_fall_back_to_defaults(self):
        config = AppConfig.from_dict({"alwaysOnTop": "false", "hotkeysEnabled": "0"})
        self.assertTrue(config.always_on_top)
        self.assertTrue(config.hotkeys_enabled)


class BackupPreservationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "settings.json"
        self.backup = self.root / "settings.json.bak"
        self.service = ConfigService(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_backup(self):
        good = AppConfig(opacity=0.42, windows=[TrackedWindow(process="good.exe")])
        self.backup.write_text(json.dumps(good.to_dict()), encoding="utf-8")

    def _assert_backup_preserved(self):
        stored = json.loads(self.backup.read_text(encoding="utf-8"))
        self.assertAlmostEqual(stored["opacity"], 0.42)
        self.assertEqual(stored["windows"][0]["process"], "good.exe")

    def test_scalar_windows_container_falls_back_and_keeps_backup(self):
        self._write_backup()
        self.path.write_text(json.dumps({"windows": 1}), encoding="utf-8")
        loaded = ConfigService(self.path).load()
        self.assertAlmostEqual(loaded.opacity, 0.42)
        self.assertEqual([w.process for w in loaded.windows], ["good.exe"])
        self._assert_backup_preserved()

    def test_invalid_primary_is_quarantined(self):
        self._write_backup()
        # A bare JSON array is syntactically valid but cannot be a settings
        # document; it used to be accepted as "defaults" and then overwrite the
        # valid backup on the next save.
        self.path.write_text("[]", encoding="utf-8")
        ConfigService(self.path).load()
        quarantined = self.root / "settings.json.invalid"
        self.assertTrue(quarantined.exists())
        self.assertEqual(json.loads(quarantined.read_text(encoding="utf-8")), [])

    def test_empty_window_list_is_a_valid_configuration(self):
        self.path.write_text(json.dumps({"windows": []}), encoding="utf-8")
        loaded = ConfigService(self.path).load()
        self.assertEqual(loaded.windows, [])
        self.assertFalse((self.root / "settings.json.invalid").exists())

    def test_saving_after_a_rejected_primary_does_not_overwrite_backup(self):
        self._write_backup()
        self.path.write_text(json.dumps({"profiles": {"a": 5}}), encoding="utf-8")
        service = ConfigService(self.path)
        loaded = service.load()
        self.assertTrue(service.save(loaded))
        self._assert_backup_preserved()

    def test_invalid_payload_is_never_written(self):
        self.assertFalse(self.service.save_dict({"windows": "nope"}))
        self.assertFalse(self.path.exists())

    def test_valid_save_still_rotates_backup(self):
        first = AppConfig(opacity=0.5, windows=[TrackedWindow(process="a.exe")])
        self.assertTrue(ConfigService(self.path).save(first))
        self.assertFalse(self.backup.exists())
        second = AppConfig(opacity=0.6, windows=[TrackedWindow(process="b.exe")])
        self.assertTrue(ConfigService(self.path).save(second))
        rotated = json.loads(self.backup.read_text(encoding="utf-8"))
        self.assertAlmostEqual(rotated["opacity"], 0.5)
        self.assertEqual(rotated["windows"][0]["process"], "a.exe")

    def test_nothing_usable_falls_back_to_defaults(self):
        self.path.write_text(json.dumps({"windows": 5}), encoding="utf-8")
        loaded = ConfigService(self.path).load()
        self.assertEqual(loaded.windows, [])
        self.assertAlmostEqual(loaded.opacity, 0.95)


if __name__ == "__main__":
    unittest.main()