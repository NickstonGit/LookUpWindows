from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
OPTIONAL_EXTERNAL = {"pyi_splash"}


def imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def main() -> int:
    local_modules = {path.stem for path in SRC.glob("*.py") if path.is_file()}
    stdlib_modules = set(sys.stdlib_module_names)
    missing: dict[str, list[str]] = defaultdict(list)

    for path in sorted(SRC.glob("*.py")):
        for module in sorted(imported_roots(path)):
            if module in stdlib_modules or module in OPTIONAL_EXTERNAL or module in local_modules:
                continue
            missing[module].append(path.name)

    if not missing:
        print("Source import closure: OK")
        return 0

    print("Source import closure: FAILED", file=sys.stderr)
    for module, importers in sorted(missing.items()):
        print(f"  missing module {module!r}; imported by: {', '.join(importers)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
