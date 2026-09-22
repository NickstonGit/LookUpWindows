import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
APP_PATH = SRC / "app.py"
CONFIG_PATH = SRC / "config.py"


class UiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(APP_PATH))

    def _method_source(self, class_name: str, method_name: str) -> str:
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == method_name:
                        return ast.get_source_segment(self.source, item) or ""
        self.fail(f"{class_name}.{method_name} not found")

    def test_panel_hide_does_not_hide_or_destroy_pips(self):
        body = self._method_source("App", "hide_panel")
        self.assertNotIn("for card in self.cards", body)
        self.assertNotIn("close_fullscreen", body)
        self.assertIn("hide_window(self.panel.hwnd)", body)

    def test_explicit_hide_all_command_keeps_old_tray_capability(self):
        body = self._method_source("App", "hide_all_to_tray")
        self.assertIn("for card in self.cards", body)
        self.assertIn("close_fullscreen", body)

    def test_card_size_is_not_limited_by_old_640x520_constants(self):
        assigned = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned.add(target.id)
        self.assertNotIn("CARD_MAX_W", assigned)
        self.assertNotIn("CARD_MAX_H", assigned)
        self.assertNotIn("min(640", self.source)

    def test_control_panel_is_wider(self):
        value = None
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CTRL_W":
                        value = ast.literal_eval(node.value)
        self.assertIsNotNone(value)
        self.assertGreaterEqual(value, 320)

    def test_card_has_rounded_blue_outline(self):
        paint = self._method_source("CardWnd", "_paint")
        shape = self._method_source("CardWnd", "_apply_shape")
        self.assertIn("round_rect_outline", paint)
        self.assertIn("ACCENT", paint)
        self.assertIn("set_round_region", shape)

    def test_cards_are_not_owned_by_control_panel(self):
        init = self._method_source("CardWnd", "__init__")
        self.assertIn("owner=0", init)
        self.assertNotIn("owner=app.panel.hwnd", init)


if __name__ == "__main__":
    unittest.main()
