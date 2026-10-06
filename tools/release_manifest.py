"""Write and verify the release manifest that binds a published pair to its bytes.

A release publishes two things: an artifact and a checksum that claims to describe
it.  When those two are produced by different steps, nothing links them: a rebuild
leaves the previous checksum next to a *new* EXE, and the pair a user downloads
then fails verification - or, worse, verifies against the validation of a build that
was never published.

So the manifest is the single place where the release facts live:

* the SHA-256 of the EXE and of the source archive, of the bytes that are actually
  on disk right now;
* the revision and a source fingerprint of the tree they were produced from;
* the gates that were run against those exact bytes.

``--write`` refuses to run unless the gates have been recorded, and ``--verify``
recomputes every hash, so a mismatching pair can never be published.  A rebuild
invalidates the manifest: ``build-onefile.bat`` deletes it before it builds, so a
stale attestation cannot survive a new artifact.

Usage::

    python tools/release_manifest.py --write --gates tools/release_gates.json
    python tools/release_manifest.py --verify
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from source_contract import source_relative_paths

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_NAME = "release-manifest.json"
EXE_NAME = "dist/LookUpWindows.exe"
ARCHIVE_NAME = "LookUpWindows-src.zip"
CHECKSUM_SUFFIX = ".sha256.txt"
# Every artifact whose bytes the manifest attests to.
ARTIFACTS = (EXE_NAME, ARCHIVE_NAME)
# The source file set is shared with package_source.ps1 through source_contract.py.
# The gates a release must not be published without.  A manifest that does not name
# them is refused: it would attest to bytes nobody proved anything about.
REQUIRED_GATES = (
    "ruff",
    "pytest",
    "check_source_imports",
    "check_build_env_release",
    "verify_frozen_runtime",
    "runtime_smoke_all",
    "verify_source_archive",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_fingerprint() -> dict:
    """Stable fingerprint of exactly the files the public source ZIP contains."""
    files = [
        (name, sha256_of(ROOT / Path(name)))
        for name in source_relative_paths(ROOT, strict=True)
    ]
    combined = hashlib.sha256(
        "\n".join(f"{name} {digest}" for name, digest in files).encode("utf-8")
    ).hexdigest()
    return {"digest": combined, "fileCount": len(files)}


def git_revision() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if completed.returncode:
        return "unknown"
    return completed.stdout.strip() or "unknown"


def git_tree_problem() -> str | None:
    """Return why the manifest cannot truthfully name HEAD, or ``None``."""
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=no"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git status could not be checked: {exc}"
    if completed.returncode:
        return "git status failed; the release revision cannot be verified"
    if completed.stdout.strip():
        return "tracked files differ from HEAD; refusing to attest a dirty working tree"
    if git_revision() == "unknown":
        return "git revision is unavailable"
    return None


def artifact_entry(path: Path) -> dict:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": sha256_of(path),
    }


def load_gates(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SystemExit(f"the recorded gates could not be read: {error}") from error
    if not isinstance(data, dict):
        raise SystemExit("the recorded gates must be a JSON object of gate name -> result")
    return data


def check_gates(gates: dict) -> list[str]:
    problems = []
    for name in REQUIRED_GATES:
        result = gates.get(name)
        if result is None:
            problems.append(f"gate {name} was not recorded")
        elif result is not True:
            problems.append(f"gate {name} did not pass")
    return problems


def manifest_path() -> Path:
    return ROOT / MANIFEST_NAME


def write(gates_path: Path) -> int:
    missing = [
        artifact for artifact in ARTIFACTS if not (ROOT / artifact).is_file()
    ]
    if missing:
        print(
            "Release manifest FAILED: missing artifacts " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1
    evidence = load_gates(gates_path)
    gates = evidence.get("gates", {})
    if not isinstance(gates, dict):
        print("Release manifest FAILED: invalid gate evidence", file=sys.stderr)
        return 1
    problems = check_gates(gates)
    tree_problem = git_tree_problem()
    if tree_problem:
        problems.append(tree_problem)
    current_revision = git_revision()
    if evidence.get("revision") != current_revision:
        problems.append("gate evidence was recorded for a different git revision")
    if evidence.get("artifacts") != [artifact_entry(ROOT / name) for name in ARTIFACTS]:
        problems.append("gate evidence describes different artifact bytes")
    if evidence.get("source") != source_fingerprint():
        problems.append("sources changed after the gates were recorded")
    if problems:
        print("Release manifest FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    manifest = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": current_revision,
        "source": source_fingerprint(),
        "artifacts": [artifact_entry(ROOT / artifact) for artifact in ARTIFACTS],
        "gates": {name: True for name in REQUIRED_GATES},
    }
    path = manifest_path()
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for artifact in ARTIFACTS:
        entry = next(
            item for item in manifest["artifacts"] if item["path"] == artifact
        )
        target = ROOT / f"{artifact}{CHECKSUM_SUFFIX}"
        target.write_text(
            f"{entry['sha256']}  {Path(artifact).name}\n", encoding="ascii"
        )
    print(f"Release manifest written: {path}")
    for artifact in manifest["artifacts"]:
        print(f"  {artifact['path']}  {artifact['sha256']}  {artifact['bytes']} bytes")
    return 0


def record_gates(gates_path: Path) -> int:
    """Bind successful checks to the exact clean revision and artifact bytes."""
    gates = load_gates(gates_path)
    problems = check_gates(gates)
    tree_problem = git_tree_problem()
    if tree_problem:
        problems.append(tree_problem)
    if problems:
        print("; ".join(problems), file=sys.stderr)
        return 1
    evidence = {
        "gates": gates,
        "revision": git_revision(),
        "artifacts": [artifact_entry(ROOT / name) for name in ARTIFACTS],
        "source": source_fingerprint(),
    }
    gates_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


def invalidate() -> int:
    """Drop the attestation of bytes that are about to be replaced."""
    removed = []
    for stale in (manifest_path(), ROOT / "build/release-gates.json",
                  *(ROOT / f"{name}{CHECKSUM_SUFFIX}" for name in ARTIFACTS)):
        if stale.exists():
            try:
                stale.unlink()
                removed.append(stale.name)
            except OSError as error:
                print(
                    f"Release manifest could not be invalidated: {stale}: {error}",
                    file=sys.stderr,
                )
                return 1
    print(
        "Release attestation invalidated: " + (", ".join(removed) if removed else "nothing to remove")
    )
    return 0


def verify() -> int:
    path = manifest_path()
    if not path.is_file():
        print(
            f"Release manifest FAILED: {MANIFEST_NAME} is missing, so nothing attests "
            "to the bytes that would be published",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        print(f"Release manifest FAILED: {error}", file=sys.stderr)
        return 1
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        print("Release manifest FAILED: invalid schema", file=sys.stderr)
        return 1
    artifacts = manifest.get("artifacts")
    if (not isinstance(artifacts, list) or len(artifacts) != len(ARTIFACTS)
            or any(not isinstance(item, dict) or not isinstance(item.get("path"), str)
                   for item in artifacts)
            or {item.get("path") for item in artifacts} != set(ARTIFACTS)):
        print("Release manifest FAILED: required artifact set is missing or invalid", file=sys.stderr)
        return 1
    problems: list[str] = []
    tree_problem = git_tree_problem()
    if tree_problem:
        problems.append(tree_problem)
    current_revision = git_revision()
    if manifest.get("revision") != current_revision:
        problems.append("manifest revision does not match the current checkout")
    if manifest.get("source") != source_fingerprint():
        problems.append("sources changed after validation")
    if manifest.get("gates") != {name: True for name in REQUIRED_GATES}:
        problems.append("the manifest does not carry every release gate as passed")
    for artifact in manifest.get("artifacts", []):
        candidate = ROOT / artifact["path"]
        if not candidate.is_file():
            problems.append(f"{artifact['path']} does not exist any more")
            continue
        actual = sha256_of(candidate)
        if actual != artifact.get("sha256") or candidate.stat().st_size != artifact.get("bytes"):
            problems.append(
                f"{artifact['path']} hashes to {actual}, the manifest attests "
                f"{artifact['sha256']}"
            )
            continue
        sidecar = ROOT / f"{artifact['path']}{CHECKSUM_SUFFIX}"
        if not sidecar.is_file():
            problems.append(f"{sidecar.name} is missing")
            continue
        expected = f"{artifact['sha256']}  {Path(artifact['path']).name}"
        if sidecar.read_text(encoding="ascii", errors="replace").strip() != expected:
            problems.append(
                f"{sidecar.name} does not describe the current {artifact['path']}"
            )
    if problems:
        print("Release manifest FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(
        f"Release manifest OK: {len(manifest.get('artifacts', []))} artifact(s) match "
        f"revision {manifest.get('revision', 'unknown')[:12]}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the manifest and sidecars")
    parser.add_argument("--record-gates", action="store_true", help="bind passed gates to artifacts")
    parser.add_argument("--verify", action="store_true", help="verify the published pair")
    parser.add_argument(
        "--invalidate",
        action="store_true",
        help="remove the manifest and sidecars before a rebuild",
    )
    parser.add_argument(
        "--gates",
        default=str(Path("build") / "release-gates.json"),
        help="JSON file recording which gates passed for these bytes",
    )
    args = parser.parse_args()
    actions = [args.write, args.verify, args.invalidate, args.record_gates]
    if sum(1 for action in actions if action) != 1:
        parser.error("choose exactly one of --write, --verify or --invalidate")
    if args.record_gates:
        return record_gates(Path(args.gates))
    if args.invalidate:
        return invalidate()
    if args.verify:
        return verify()
    return write(Path(args.gates))


if __name__ == "__main__":
    mp_freeze = getattr(sys.modules.get("multiprocessing"), "freeze_support", None)
    if mp_freeze is not None:
        mp_freeze()
    raise SystemExit(main())