import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


class RuntimeArchitectureTests(unittest.TestCase):
    def test_normal_start_does_not_show_control_panel(self):
        tree = ast.parse((SRC / "app.py").read_text(encoding="utf-8"))
        start = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "start"
        )
        calls = [
            node for node in ast.walk(start)
            if isinstance(node, ast.Call)
        ]
        self.assertFalse(any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "show_panel"
            for call in calls
        ))

    def test_cards_are_not_gated_by_panel_visibility_at_startup(self):
        source = (SRC / "app.py").read_text(encoding="utf-8")
        self.assertIn("if not self._cards_hidden:", source)
        self.assertNotIn("if not self._hidden and not self._cards_hidden:", source)

    def test_tray_primary_action_is_double_click(self):
        source = (SRC / "trayicon.py").read_text(encoding="utf-8")
        self.assertIn("WM_LBUTTONDBLCLK = 0x0203", source)
        self.assertIn("if event == WM_LBUTTONDBLCLK:", source)
        self.assertNotIn("if event == WM_LBUTTONUP:\n                    self.on_click()", source)

    def test_restore_is_verified_before_state_is_discarded(self):
        source = (SRC / "winapi.py").read_text(encoding="utf-8")
        self.assertIn("screen_rect: tuple[int, int, int, int]", source)
        self.assertIn("is_effectively_onscreen(hwnd)", source)
        self.assertIn("_clamp_screen_rect_to_monitor(state.screen_rect)", source)

    def test_orphaned_park_recovery_exists(self):
        winapi = (SRC / "winapi.py").read_text(encoding="utf-8")
        app = (SRC / "app.py").read_text(encoding="utf-8")
        self.assertIn("def recover_orphaned_lookup_park", winapi)
        self.assertIn("looks_like_lookup_parked(candidate.hwnd)", app)

    def test_parked_taskbar_watch_is_fast_and_lightweight(self):
        source = (SRC / "app.py").read_text(encoding="utf-8")
        self.assertIn("TIMER_PARKED_WATCH = 6", source)
        self.assertIn("winui.set_timer(self.panel.hwnd, TIMER_PARKED_WATCH, 100)", source)
        self.assertIn("def _watch_parked_sources", source)


if __name__ == "__main__":
    unittest.main()
