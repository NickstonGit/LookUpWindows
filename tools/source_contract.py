"""Single source of truth for the public source-archive file set.

Both packaging and release attestation import/use this contract so a file cannot be
added to the ZIP without also changing the source fingerprint (or vice versa).
Local config, build output and ad-hoc developer files are deliberately absent.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

FIXED_FILES = (
    "conftest.py",
    "config/settings.example.json",
    "README.md",
    "ROADMAP.md",
    "LICENSE",
    "NOTICE",
    ".gitignore",
    ".gitattributes",
    "requirements-build.txt",
    "ruff.toml",
    "app.ico",
    "splash.png",
    "build-onefile.bat",
    "arch.bat",
    "run.bat",
)

DYNAMIC_SPECS = (
    ("src", "*.py"),
    ("tests", "*.py"),
    ("tools", "*.py"),
    ("tools", "*.ps1"),
    ("docs", "*.md"),
    (".github/workflows", "*.y*ml"),
)

REQUIRED_RUNTIME_MODULES = (
    "src/change_logic.py",
    "src/windowmatch.py",
    "src/screen.py",
    "src/recovery.py",
    "src/restoreguard.py",
)


def source_relative_paths(root: Path, *, strict: bool = True) -> tuple[str, ...]:
    root = Path(root).resolve()
    names: set[str] = set()
    for relative in FIXED_FILES:
        path = root / relative
        if strict and not path.is_file():
            raise FileNotFoundError(f"Required source file is missing: {relative}")
        if path.is_file():
            names.add(Path(relative).as_posix())

    for directory, pattern in DYNAMIC_SPECS:
        base = root / directory
        if strict and not base.is_dir():
            raise FileNotFoundError(f"Required source directory is missing: {directory}")
        if not base.is_dir():
            continue
        for path in base.glob(pattern):
            if path.is_file():
                names.add(path.relative_to(root).as_posix())

    if strict:
        for relative in REQUIRED_RUNTIME_MODULES:
            if not (root / relative).is_file():
                raise FileNotFoundError(f"Required runtime module is missing: {relative}")

    return tuple(sorted(names, key=str.casefold))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        paths = source_relative_paths(Path(args.root), strict=True)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(paths, ensure_ascii=False))
    else:
        print("\n".join(paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
