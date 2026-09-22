import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found in {path.name}")


class SourceParkingRegressionTests(unittest.TestCase):
    def test_refresh_requires_real_foreground_transition_before_restore(self):
        body = function_source(SRC / "app.py", "_do_refresh")
        self.assertIn("_parked_seen_not_foreground = True", body)
        self.assertIn("elif card._parked_seen_not_foreground", body)
        self.assertNotIn("card._parked_source is not None and card.src_hwnd == foreground", body)

    def test_park_marks_initial_foreground_state(self):
        body = function_source(SRC / "app.py", "_finish_park_source")
        self.assertIn("_parked_seen_not_foreground = winapi.get_foreground_hwnd() != hwnd", body)

    def test_parking_keeps_tiny_virtual_desktop_intersection(self):
        body = function_source(SRC / "winapi.py", "_parking_position")
        self.assertIn("width", body)
        self.assertIn("height", body)
        self.assertIn("+ 1", body)


if __name__ == "__main__":
    unittest.main()
