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
