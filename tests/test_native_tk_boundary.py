import ast
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
APP_PATH = SRC / "app.py"


class NativeTkBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))

    def test_root_after_is_restricted_to_tk_bridge_and_startup(self):
        allowed_methods = {"_pump_deferred", "_stop_deferred_pump", "start"}
        offenders = []

        class Visitor(ast.NodeVisitor):
            def __init__(self):
                self.method = None

            def visit_FunctionDef(self, node):
                previous = self.method
                self.method = node.name
                self.generic_visit(node)
                self.method = previous

            def visit_Call(self, node):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"after", "after_cancel"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "root"
                ):
                    if self.method not in allowed_methods:
                        offenders.append((node.lineno, self.method, func.attr))
                self.generic_visit(node)

        Visitor().visit(self.tree)
        self.assertEqual(offenders, [], f"Unsafe direct Tk timer calls: {offenders}")


    def test_native_handlers_do_not_open_modal_menus_or_tk(self):
        forbidden = {"track_popup_menu", "TrackPopupMenu", "wait_window", "mainloop",
                     "show_selector", "show_settings", "show_profiles", "askstring"}
        offenders = []
        for path in (APP_PATH, SRC / "trayicon.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for method in ast.walk(tree):
                if not isinstance(method, ast.FunctionDef) or method.name not in {"on_message", "_handle_message"}:
                    continue
                for call in ast.walk(method):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        if call.func.attr in forbidden:
                            offenders.append((path.name, call.lineno, call.func.attr))
        self.assertEqual(offenders, [])

    def test_class_methods_are_not_silently_overridden(self):
        duplicates = []
        for path in SRC.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in ast.walk(tree):
                if not isinstance(cls, ast.ClassDef):
                    continue
                names = set()
                for method in cls.body:
                    if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if method.name in names:
                            duplicates.append((path.name, cls.name, method.name))
                        names.add(method.name)
        self.assertEqual(duplicates, [])

    def test_schedule_save_config_does_not_touch_tk(self):
        target = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "schedule_save_config":
                target = node
                break
        self.assertIsNotNone(target)
        calls = []
        for node in ast.walk(target):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
        self.assertNotIn("after", calls)
        self.assertNotIn("after_cancel", calls)


if __name__ == "__main__":
    unittest.main()
