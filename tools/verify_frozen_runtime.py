"""Fail closed when the frozen EXE is not the interpreter we release.

``build-onefile.bat`` refuses to run on anything but CPython 3.14 x64, but a
release is produced by a *machine*, not by the script: the runner, a manual
invocation or a stale ``dist/`` can still hand the publishing job an EXE whose
embedded runtime is not the one the release contract names.  A green build step
is therefore not evidence, so the archive the EXE actually carries is inspected
here.

The check is semantic: it reads the PyInstaller archive table of the produced
onefile and requires the pinned interpreter DLL plus the Tcl/Tk data that Python
3.14 mounts through zipfs.  It never trusts file names on disk or the exit code
of the build.

Usage::

    python tools/verify_frozen_runtime.py --exe dist/LookUpWindows.exe
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The released artifact must embed exactly this interpreter.
RELEASE_PYTHON = (3, 14)
RELEASE_PYTHON_DLL = "python314.dll"
# CPython 3.14 does not ship Tcl/Tk as loose files: the data lives inside the
# Tcl/Tk shared libraries (Tcl 9.0 / Tk 9.0).  An artifact missing these cannot
# create a Tk dialog, which is the first thing the application does.
TCL_DLL = "tcl90.dll"
TK_DLL = "tcl9tk90.dll"
REQUIRED_ENTRIES = ("base_library.zip", "_tkinter.pyd")


def archive_entry_names(exe: Path) -> set[str]:
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(exe))
    try:
        # The TOC maps the entry name to (offset, length, ..., typecode).
        return {name.replace("\\", "/") for name in reader.toc}
    finally:
        try:
            reader.close()
        except Exception:
            pass


def check(exe: Path) -> list[str]:
    if not exe.is_file():
        return [f"artifact does not exist: {exe}"]

    try:
        names = archive_entry_names(exe)
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return [f"cannot read the PyInstaller archive of {exe.name}: {error}"]

    expected_python = ".".join(str(part) for part in RELEASE_PYTHON)
    problems: list[str] = []
    if RELEASE_PYTHON_DLL not in names:
        problems.append(
            f"{exe.name} does not embed {RELEASE_PYTHON_DLL}; release artifacts must "
            f"carry the Python {expected_python} runtime, but the archive contains "
            f"{sorted(n for n in names if n.endswith('.dll'))}"
        )
    for required in REQUIRED_ENTRIES:
        if required not in names:
            problems.append(f"{exe.name} does not embed {required}")
    for marker, label in ((TCL_DLL, "Tcl 9.0"), (TK_DLL, "Tk 9.0")):
        if marker not in names:
            problems.append(
                f"{exe.name} does not embed the Python {expected_python} {label} runtime "
                f"({marker}); this is the known failure mode of a PyInstaller that "
                "cannot read the 3.14 Tcl/Tk data"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", default="dist/LookUpWindows.exe")
    args = parser.parse_args()
    exe = Path(args.exe)
    problems = check(exe)
    if problems:
        print("Frozen runtime check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"Frozen runtime check OK: {exe} carries Python {RELEASE_PYTHON[0]}.{RELEASE_PYTHON[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())