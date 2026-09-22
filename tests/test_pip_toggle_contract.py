import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


class PiPToggleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_text = (SRC / "app.py").read_text(encoding="utf-8")
        cls.winapi_text = (SRC / "winapi.py").read_text(encoding="utf-8")
        cls.app_tree = ast.parse(cls.app_text)

    def test_card_click_uses_toggle(self):
        self.assertIn("self.app.defer(self.app.toggle_card_source, self)", self.app_text)

    def test_visible_source_is_parked_not_minimized(self):
        self.assertIn("winapi.park_window_offscreen_sync(hwnd)", self.app_text)
        self.assertNotIn("winapi.minimize_window(hwnd)", self.app_text)
        self.assertIn("SWP_ASYNCWINDOWPOS", self.winapi_text)

    def test_parked_source_restores_exact_placement(self):
        self.assertIn("winapi.restore_parked_window_sync(hwnd, state)", self.app_text)
        self.assertIn("GetWindowPlacement", self.winapi_text)
        self.assertIn("SetWindowPlacement", self.winapi_text)
        self.assertIn("WPF_ASYNCWINDOWPLACEMENT", self.winapi_text)

    def test_minimized_source_is_restored(self):
        self.assertIn("if winapi.is_minimized(hwnd):", self.app_text)
        self.assertIn("winapi.set_foreground(hwnd)", self.app_text)

    def test_live_thumbnail_not_replaced_by_minimized_placeholder_on_park(self):
        toggle = self.app_text.split("def toggle_card_source", 1)[1].split("def activate_card", 1)[0]
        self.assertNotIn('card._placeholder = "Окно свернуто"', toggle)
        self.assertIn("card.update_thumb_geometry()", toggle)

    def test_parked_window_is_never_orphaned(self):
        self.assertIn("self.restore_parked_source(card, activate=False)", self.app_text)
        self.assertIn("self.app.restore_parked_source(self, activate=False)", self.app_text)

    def test_context_menu_open_remains_open_only(self):
        self.assertIn("if command == MENU_OPEN:\n            self.activate_card(card)", self.app_text)
        self.assertIn("def activate_card(self, card: CardWnd)", self.app_text)


if __name__ == "__main__":
    unittest.main()
