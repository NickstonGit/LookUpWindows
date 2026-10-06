"""Fail closed when a build would not reproduce the released artifact.

The release toolchain is not "any Python": it is CPython 3.14 x64 with the
pinned PyInstaller release, because that is the combination whose frozen Tcl/Tk
and ``multiprocessing`` behaviour has actually been verified.  A build script
that only checks ``>= 3.10`` silently produces an artifact nobody tested, so the
release-grade scripts call this check before they touch ``dist/``.

Usage::

    python tools/check_build_env.py --mode release
    python tools/check_build_env.py --mode dev
"""

from __future__ import annotations

import argparse
import platform
import struct
import sys
from pathlib import Path

# What the published artifact is built and smoke-tested with.
RELEASE_PYTHON = (3, 14)
RELEASE_ARCHITECTURE = "64bit"
RELEASE_PYINSTALLER = "6.22.3"
# What the sources are expected to keep working on.
SOURCE_PYTHON_MINIMUM = (3, 10)


def pointer_bits() -> int:
    return struct.calcsize("P") * 8


def check_release_environment() -> list[str]:
    problems: list[str] = []
    if sys.version_info[:2] != RELEASE_PYTHON:
        problems.append(
            f"release artifacts require Python {RELEASE_PYTHON[0]}.{RELEASE_PYTHON[1]}.x, "
            f"but this is {platform.python_version()}"
        )
    if pointer_bits() != 64 or platform.architecture()[0] != RELEASE_ARCHITECTURE:
        problems.append(
            f"release artifacts require {RELEASE_ARCHITECTURE} CPython, "
            f"but this interpreter is {pointer_bits()}-bit ({platform.architecture()[0]})"
        )
    try:
        import PyInstaller
    except ImportError:
        problems.append(f"PyInstaller {RELEASE_PYINSTALLER} is not installed")
    else:
        if str(PyInstaller.__version__) != RELEASE_PYINSTALLER:
            problems.append(
                f"PyInstaller must be pinned to {RELEASE_PYINSTALLER}, "
                f"but {PyInstaller.__version__} is installed"
            )
    return problems


def check_source_environment() -> list[str]:
    if sys.version_info[:2] < SOURCE_PYTHON_MINIMUM:
        return [
            f"the sources require Python {SOURCE_PYTHON_MINIMUM[0]}.{SOURCE_PYTHON_MINIMUM[1]}+, "
            f"but this is {platform.python_version()}"
        ]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("dev", "release"),
        default="dev",
        help="dev: sources must import; release: the release toolchain is required",
    )
    args = parser.parse_args()
    problems = (
        check_release_environment() if args.mode == "release" else check_source_environment()
    )
    if problems:
        print(f"Build environment check FAILED ({args.mode}):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    target = "release toolchain" if args.mode == "release" else "source environment"
    print(
        f"Build environment check OK ({args.mode}): {platform.python_version()} "
        f"{pointer_bits()}-bit, {Path(sys.executable).name} -> {target}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
