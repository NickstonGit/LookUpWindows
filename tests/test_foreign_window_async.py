"""R01: foreign-window calls made from the UI dispatch path must not block it.

A synchronous ``ShowWindow`` sends a message to the owning thread and waits for
it, so a hung target freezes LookUp's whole UI.  These tests use a real foreign
window whose window procedure stalls on ``WM_SHOWWINDOW`` and assert that the
paths used by the UI return immediately.
"""

import ast
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import winapi
except Exception as exc:  # pragma: no cover - non-Windows
    winapi = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

APP = (SRC / "app.py").read_text(encoding="utf-8")
WINAPI = (SRC / "winapi.py").read_text(encoding="utf-8")
HANG_SEC = 3.0


def start_stalled_target(*extra: str):
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "smoke_target.py"),
            "--title",
            "LUW r01 probe",
            "--hang",
            str(HANG_SEC),
            "--stall-on",
            "show",
            *extra,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = process.stdout.readline()
    try:
        payload = json.loads(line)
    except ValueError as exc:  # pragma: no cover - diagnostics
        process.kill()
        raise AssertionError(f"stalled target did not start: {line!r} ({exc})") from exc
    return process, int(payload["hwnd"])


def stop_target(process, hwnd) -> None:
    try:
        winapi.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        process.wait(timeout=15)
    except Exception:
        process.kill()
    finally:
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


class BlockingForeignCallTests(unittest.TestCase):
    """Behavioural: the UI-facing helpers return while the target is stalled."""

    def _target(self, *extra: str):
        if winapi is None:  # pragma: no cover - non-Windows
            self.skipTest(f"winapi unavailable: {_IMPORT_ERROR}")
        process, hwnd = start_stalled_target(*extra)
        self.addCleanup(stop_target, process, hwnd)
        # Let the startup stall finish before measuring anything.
        time.sleep(HANG_SEC + 1.0)
        return hwnd

    @staticmethod
    def _elapsed(action) -> float:
        start = time.monotonic()
        action()
        return time.monotonic() - start

    def test_show_window_noactivate_does_not_wait_for_the_target(self):
        hwnd = self._target()
        # Sanity check: the stall is real, a synchronous cross-process show on
        # this window takes at least the configured hang.  Hiding first is what
        # makes a later show generate WM_SHOWWINDOW at all.
        self.assertGreater(self._elapsed(lambda: winapi.user32.ShowWindow(hwnd, 0)), HANG_SEC)
        time.sleep(0.5)
        elapsed = self._elapsed(lambda: winapi.show_window_noactivate(hwnd))
        self.assertLess(elapsed, 1.0, f"async show blocked for {elapsed:.2f}s")

    def test_async_foreground_does_not_wait_for_the_target(self):
        hwnd = self._target("--minimized")
        self.assertTrue(winapi.is_minimized(hwnd))
        elapsed = self._elapsed(lambda: winapi.set_foreground(hwnd, async_restore=True))
        self.assertLess(elapsed, 1.0, f"async foreground blocked for {elapsed:.2f}s")


class UiPathContractTests(unittest.TestCase):
    """Static: every UI-thread foreign-window activation is async."""

    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(APP)

    def test_set_foreground_exposes_the_async_option(self):
        self.assertIn("async_restore", WINAPI)
        show_window = WINAPI.split("def show_window_noactivate", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("ShowWindowAsync", show_window)
        self.assertNotIn("user32.ShowWindow(", show_window)

    def test_refresh_path_uses_the_async_helper(self):
        refresh = APP.split("def _do_refresh", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("winapi.show_window_noactivate(", refresh)
        self.assertNotIn("winapi.set_foreground(", refresh)

    def test_ui_actions_request_async_restore(self):
        for method in ("toggle_card_source", "activate_card", "restore_parked_source"):
            body = APP.split(f"def {method}", 1)[1].split("\n    def ", 1)[0]
            for call in [line for line in body.splitlines() if "winapi.set_foreground(" in line]:
                self.assertIn("async_restore=True", call, msg=f"{method}: {call.strip()}")

    def test_worker_paths_keep_the_synchronous_restore(self):
        # Park/restore workers must observe completion, so they keep the
        # synchronous variant and the verified placement restore.
        for worker in ("_cleanup_restore_worker", "_restore_source_worker"):
            body = APP.split(f"def {worker}", 1)[1].split("\n    def ", 1)[0]
            self.assertIn("winapi.restore_parked_window_sync(", body)


if __name__ == "__main__":
    unittest.main()