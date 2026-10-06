import ast
import unittest
from pathlib import Path

APP_PATH = Path(__file__).resolve().parent.parent / "src" / "app.py"
SOURCE = APP_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE, filename=str(APP_PATH))


def method_source(class_name: str, method_name: str) -> str:
    for node in TREE.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return ast.get_source_segment(SOURCE, item) or ""
    raise AssertionError(f"{class_name}.{method_name} not found in app.py")


class MultiWindowIdentityTests(unittest.TestCase):
    """Several windows of one process must be selectable and bind separately.

    Regression cover for the case where a single tracked VS Code entry hid every
    VS Code window in the selector, so the second window could not be added.
    """

    def test_selector_hides_only_windows_already_bound(self):
        body = method_source("WindowSelectorDialog", "_claimed_hwnds")
        self.assertIn("self.bound_hwnds", body)
        self.assertIn("tracked.source_hwnd", body)
        self.assertIn("find_preferred", body)

    def test_selector_no_longer_filters_by_process_wide_match(self):
        body = method_source("WindowSelectorDialog", "_load")
        self.assertIn("self._claimed_hwnds(self.windows)", body)
        self.assertNotIn("self.finder.matches(", body)

    def test_title_discriminator_counts_windows_hidden_from_the_list(self):
        body = method_source("WindowSelectorDialog", "_add")
        self.assertIn("for candidate in self.windows:", body)

    def test_selector_receives_the_already_displayed_windows(self):
        body = method_source("App", "show_selector")
        self.assertIn("card.src_hwnd for card in self.cards if card.src_hwnd", body)
        self.assertIn("source_hwnd=int(selected_hwnd or 0) or None", body)

    def test_add_foreground_window_pins_the_entry_to_that_window(self):
        body = method_source("App", "add_foreground_window")
        self.assertIn("source_hwnd=candidate.hwnd or None", body)

    def test_runtime_binding_uses_the_tracked_field_not_a_dynamic_attribute(self):
        body = method_source("App", "_create_card_for_tracked")
        self.assertIn("tracked.source_hwnd", body)
        self.assertNotIn("_selected_hwnd", body)
        self.assertNotIn("_selected_hwnd", SOURCE)

    def test_card_keeps_the_tracked_entry_pinned_to_the_shown_window(self):
        body = method_source("CardWnd", "set_candidate")
        self.assertIn("self.tracked.source_hwnd = candidate.hwnd", body)
        self.assertIn("self.tracked.source_hwnd = None", body)

    def test_autorefind_never_rebinds_a_window_already_shown_by_a_card(self):
        body = method_source("App", "_do_refresh")
        self.assertIn("claimed_hwnds", body)
        self.assertIn("exclude_hwnds=claimed_hwnds", body)
        self.assertIn("claimed_hwnds.add(card.src_hwnd)", body)


if __name__ == "__main__":
    unittest.main()
