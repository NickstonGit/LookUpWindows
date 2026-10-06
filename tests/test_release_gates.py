"""Release and CI configuration must match the release toolchain."""

import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(encoding="utf-8")
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
PACKAGE_SOURCE = (ROOT / "tools" / "package_source.ps1").read_text(encoding="utf-8")
ARCH_BAT = (ROOT / "arch.bat").read_text(encoding="utf-8")
REQUIREMENTS = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
BUILD_ONEFILE_BAT = (ROOT / "build-onefile.bat").read_text(encoding="utf-8")
BUILD_ENV_TOOL = (ROOT / "tools" / "check_build_env.py").read_text(encoding="utf-8")
SMOKE = (ROOT / "tools" / "runtime_smoke.py").read_text(encoding="utf-8")
FROZEN_RUNTIME_TOOL = (ROOT / "tools" / "verify_frozen_runtime.py").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
BUILD_DOC = (ROOT / "docs" / "build.md").read_text(encoding="utf-8")
# The single production artifact. There is no onedir variant to publish.
PRODUCTION_ARTIFACT = "dist/LookUpWindows.exe"

# Every failure mode that the frozen artifact has to prove it cannot do.
REQUIRED_SCENARIOS = (
    "responsive",
    "slow",
    "inflight",
    "hardkill",
    "aged",
    "badjournal",
    "jobfail",
    "journalrace",
    "journalio",
    "guardianfail",
    # Recovery invariants that used to lose a parked window.
    "guardianlimit",
    "ownerpidreuse",
    "badlease",
    "claimaba",
    "monitorgap",
    "parkmark",
    "outerjob",
)


def scenario_registry() -> set[str]:
    """The scenarios the smoke tool itself can run, from its single registry."""
    match = re.search(r"^SCENARIOS = \{\n(.*?)^\}\n", SMOKE, re.MULTILINE | re.DOTALL)
    assert match, "tools/runtime_smoke.py has no SCENARIOS registry"
    return set(re.findall(r'"(\w+)":\s*scenario_', match.group(1)))


class ScenarioRegistryTests(unittest.TestCase):
    """One registry, one interface: they cannot drift apart silently."""

    def test_every_implemented_scenario_is_registered(self):
        implemented = set(re.findall(r"^def (scenario_\w+)\(", SMOKE, re.MULTILINE))
        registered = scenario_registry()
        self.assertEqual(
            {name[len("scenario_"):] for name in implemented},
            registered,
            "a scenario is implemented but not reachable through --scenario",
        )

    def test_the_release_gate_runs_the_registry_itself(self):
        # The workflow used to repeat the registry by hand, which is how a new
        # scenario could ship without ever being executed on the artifact.
        self.assertIn("runtime_smoke.py", RELEASE)
        self.assertIn("--all", RELEASE)
        self.assertNotIn("$scenarios", RELEASE)

    def test_run_all_executes_every_registered_scenario(self):
        run_all = SMOKE.split("def run_all(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("for name in SCENARIOS:", run_all)
        self.assertIn("run(scenario_args)", run_all)
        # A failing scenario fails the gate instead of being skipped.
        self.assertIn("failures.append", run_all)
        self.assertIn("raise SmokeError(", run_all)

    def test_the_registry_covers_every_required_failure_mode(self):
        self.assertTrue(set(REQUIRED_SCENARIOS).issubset(scenario_registry()))

    def test_scenarios_that_own_their_app_run_are_declared_in_the_registry(self):
        # The driver used to special-case one scenario by name, which is how the
        # registry and the workflow stopped agreeing about what would run.
        self.assertNotIn('args.scenario == "badjournal"', SMOKE)
        self.assertIn("OWNS_APP_RUN", SMOKE)


def job_section(text: str, job: str) -> str:
    match = re.search(rf"^  {job}:\n(.*?)(?=^  [a-zA-Z_-]+:\n|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"job {job} not found"
    return match.group(1)


def job_steps(text: str, job: str) -> list[tuple[str, str]]:
    """Return the ``(name, body)`` of every step of a job, in execution order.

    The workflows are read as an ordered step sequence rather than as a bag of
    strings: what has to be guaranteed is that one step runs before another, so
    the test has to see the same ordering the runner does.
    """
    section = job_section(text, job)
    lines = section.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if re.match(r"^      - (name|uses|run|shell):", line)
    ]
    if not starts:
        return []
    steps: list[tuple[str, str]] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block = lines[start:end]
        name = next(
            (line.split(":", 1)[1].strip() for line in block if line.lstrip().startswith("- name:")),
            block[0].strip(),
        )
        steps.append((name, "\n".join(block)))
    return steps


def step_running(steps: list[tuple[str, str]], needle: str) -> int | None:
    for index, (_name, body) in enumerate(steps):
        if needle in body:
            return index
    return None


class ReleaseInterpreterTests(unittest.TestCase):
    """Python version: production is defined around Python 3.14, so the release builders must use it."""

    def test_release_build_uses_python_314(self):
        versions = re.findall(r'python-version:\s*"([^"]+)"', RELEASE)
        self.assertTrue(versions)
        self.assertEqual(set(versions), {"3.14"})

    def test_source_artifact_job_uses_python_314(self):
        section = job_section(CI, "source-artifact")
        versions = re.findall(r'python-version:\s*"([^"]+)"', section)
        self.assertEqual(set(versions), {"3.14"})

    def test_compatibility_matrix_may_stay_broader(self):
        section = job_section(CI, "test")
        versions = re.findall(r'"(\d+\.\d+)"', section)
        self.assertIn("3.14", versions)
        self.assertNotIn("3.12", versions)

    def test_pyinstaller_pin_is_preserved(self):
        # The pin exists for the Python 3.14 Tcl/Tk zipfs data; downgrading it
        # would reintroduce the frozen startup failure.
        self.assertIn("pyinstaller==6.22.3", REQUIREMENTS)
        for text in (CI, RELEASE):
            self.assertNotIn("pyinstaller==6.22.2", text.lower())


class SourceClosureGateTests(unittest.TestCase):
    """Source closure: the direct source-closure step is a mandatory, explicit gate."""

    def test_both_workflows_run_the_closure_check_directly(self):
        for text in (CI, RELEASE):
            self.assertIn("python tools/check_source_imports.py", text)

    def test_source_artifact_job_rechecks_the_archive(self):
        section = job_section(CI, "source-artifact")
        self.assertIn("python tools/check_source_imports.py", section)
        self.assertIn("verify_source_archive.ps1", section)

    def test_release_verifies_the_archive(self):
        self.assertIn("verify_source_archive.ps1", RELEASE)
        self.assertIn("LookUpWindows-src.zip", RELEASE)

    def test_archive_ships_the_recovery_module(self):
        self.assertIn("src\\recovery.py", PACKAGE_SOURCE)
        self.assertTrue((ROOT / "src" / "recovery.py").exists())

    def test_archive_ships_the_monitor_geometry_the_recovery_verdict_depends_on(self):
        # screen.py is what decides "is this window really visible", so an archive
        # without it would install a recovery that restores onto nothing.
        for relative in ("src\\screen.py", "src\\screen.py"):
            self.assertIn(relative, PACKAGE_SOURCE)
        self.assertTrue((ROOT / "src" / "screen.py").exists())
        verify = (ROOT / "tools" / "verify_source_archive.ps1").read_text(encoding="utf-8")
        self.assertIn("src\\screen.py", verify)

    def test_archive_ships_the_recovery_ownership_regressions(self):
        self.assertTrue((ROOT / "tests" / "test_recovery_ownership.py").exists())
        verify = (ROOT / "tools" / "verify_source_archive.ps1").read_text(encoding="utf-8")
        self.assertIn("tests\\test_recovery_ownership.py", verify)

    def test_archive_ships_the_restore_executor_and_its_gates(self):
        for relative in ("src\\restoreguard.py", "tools\\check_build_env.py"):
            self.assertIn(relative, PACKAGE_SOURCE)
        for parts in (("src", "restoreguard.py"), ("tools", "check_build_env.py")):
            self.assertTrue(
                (ROOT.joinpath(*parts)).exists(), f"{'/'.join(parts)} is missing"
            )
        verify = (ROOT / "tools" / "verify_source_archive.ps1").read_text(encoding="utf-8")
        for relative in ("src\\restoreguard.py", "tools\\check_build_env.py",
                         "tests\\test_restore_guard.py"):
            self.assertIn(relative, verify)

class FrozenArtifactGateTests(unittest.TestCase):
    """Frozen artifact: a successful PyInstaller exit is not readiness; the exe must be run."""

    def test_release_runs_the_runtime_smoke_on_the_built_exe(self):
        self.assertIn("tools/runtime_smoke.py", RELEASE)
        self.assertIn("--mode exe", RELEASE)
        self.assertIn(PRODUCTION_ARTIFACT, RELEASE)
        self.assertIn("--exe", RELEASE)

    def test_release_builds_the_exe_before_the_smoke(self):
        build = RELEASE.index("build-onefile.bat")
        smoke = RELEASE.index("tools/runtime_smoke.py")
        self.assertLess(build, smoke)

    def test_every_scenario_is_gated_on_the_frozen_artifact(self):
        # A scenario that exists but is not wired into the release proves
        # nothing.  The workflow therefore runs the tool's own registry instead of
        # repeating its names: a scenario added to SCENARIOS becomes a release gate
        # automatically, and the list cannot rot.
        self.assertIn("tools/runtime_smoke.py", RELEASE)
        self.assertIn("--all", RELEASE)
        self.assertIn("--mode exe", RELEASE)
        self.assertIn('"${{ github.ref_name }}"', RELEASE)
        # No second, hand-maintained copy of the registry in the workflow.
        self.assertNotIn("$scenarios", RELEASE)
        for scenario in REQUIRED_SCENARIOS:
            self.assertIn(f'"{scenario}": scenario_', SMOKE, f"{scenario} is not registered")

    def test_the_registry_is_reachable_as_an_interface_not_only_as_a_loop(self):
        # --all is what makes the workflow registry-driven; --list-scenarios makes
        # the same registry inspectable without running a single scenario.
        self.assertIn('"--all"', SMOKE)
        self.assertIn('"--list-scenarios"', SMOKE)
        self.assertIn("def run_all(", SMOKE)
        self.assertIn("for name in SCENARIOS:", SMOKE)

    def test_release_fails_when_a_scenario_fails(self):
        self.assertRegex(
            RELEASE,
            r"if \(\$LASTEXITCODE -ne 0\) \{ exit \$LASTEXITCODE \}",
        )

    def test_release_confirms_the_frozen_python_runtime(self):
        # The published EXE has to be the 3.14 artifact, not whatever interpreter
        # happened to be on the runner.
        steps = job_steps(RELEASE, "build-release")
        verify = step_running(steps, "verify_frozen_runtime.py")
        self.assertIsNotNone(verify, "release does not verify the frozen Python runtime")
        self.assertLess(step_running(steps, "build-onefile.bat"), verify)
        self.assertIn("python314.dll", FROZEN_RUNTIME_TOOL)

    def test_smoke_covers_readiness_and_clean_quit(self):
        self.assertIn('"WPCtrl"', SMOKE)
        self.assertIn('"WPCard"', SMOKE)
        self.assertIn("wait_helpers", SMOKE)
        self.assertIn("request_quit", SMOKE)
        self.assertIn("hardkill", SMOKE)

    def test_smoke_scenarios_are_all_reachable_from_the_command_line(self):
        parser_block = SMOKE.split("def main()", 1)[1]
        self.assertIn("--scenario", parser_block)
        for scenario in REQUIRED_SCENARIOS:
            self.assertIn(scenario, SMOKE)

    def test_runtime_smoke_is_published_with_the_source_archive(self):
        self.assertTrue((ROOT / "tools" / "runtime_smoke.py").exists())
        contract = (ROOT / "tools" / "source_contract.py").read_text(encoding="utf-8")
        self.assertIn('("tools", "*.py")', contract)
        self.assertIn('("tools", "*.ps1")', contract)
        self.assertIn("source_contract.py", PACKAGE_SOURCE)


class OnefileOnlyPolicyTests(unittest.TestCase):
    """Onefile is the only production build; an onedir artifact must not creep back in."""

    PUBLIC_FILES = (
        ("README.md", README),
        ("docs/build.md", BUILD_DOC),
        (".github/workflows/release.yml", RELEASE),
        (".github/workflows/windows-ci.yml", CI),
        ("build-onefile.bat", BUILD_ONEFILE_BAT),
        ("tools/package_source.ps1", PACKAGE_SOURCE),
    )

    def test_no_onedir_build_script_exists(self):
        self.assertFalse((ROOT / "build.bat").exists())
        self.assertIn("--onefile", BUILD_ONEFILE_BAT)
        self.assertNotIn('"build.bat"', PACKAGE_SOURCE, "removed build.bat is still packaged")

    def test_release_publishes_only_the_onefile_exe(self):
        create = RELEASE.index("gh release create")
        published = RELEASE[create:]
        self.assertIn(PRODUCTION_ARTIFACT, published)
        self.assertNotIn("onedir", published.lower())
        # The onedir directory must not even be zipped for publication.
        self.assertNotIn("Compress-Archive", RELEASE)

    def test_release_does_not_smoke_a_second_artifact(self):
        section = job_section(RELEASE, "build-release")
        self.assertNotIn('"dist/LookUpWindows/', section)
        self.assertNotIn("LookUpWindows-onedir", section)

    def test_the_public_tree_never_calls_onedir_the_recommended_production_build(self):
        # The project has no onedir build. The word may therefore only survive in
        # public files inside a sentence that denies it exists; any co-occurrence
        # with a production/recommendation claim is the regression we care about.
        denials = ("нет", "не ", "ни ", "no ", "not ", "never")
        for name, text in self.PUBLIC_FILES:
            for line in text.splitlines():
                lowered = line.lower()
                if "onedir" not in lowered:
                    continue
                self.assertTrue(
                    any(token in lowered for token in denials),
                    f"{name} mentions onedir without denying it: {line.strip()}",
                )

    def test_documentation_names_onefile_as_the_production_build(self):
        for name, text in (("README.md", README), ("docs/build.md", BUILD_DOC)):
            self.assertIn("build-onefile.bat", text)
            self.assertIn(PRODUCTION_ARTIFACT.replace("/", "\\"), text.replace("/", "\\"))


class BuildEnvironmentGateTests(unittest.TestCase):
    """Toolchain: the artifact has to be built with the verified toolchain."""

    def test_the_release_toolchain_is_declared_once(self):
        self.assertIn("RELEASE_PYTHON = (3, 14)", BUILD_ENV_TOOL)
        self.assertIn('RELEASE_ARCHITECTURE = "64bit"', BUILD_ENV_TOOL)
        self.assertIn('RELEASE_PYINSTALLER = "6.22.3"', BUILD_ENV_TOOL)

    def test_the_build_scripts_fail_closed_on_the_toolchain(self):
        # build-onefile.bat is the only build script, so it is the only gate.
        self.assertIn(
            "tools\\check_build_env.py --mode release",
            BUILD_ONEFILE_BAT,
            "build-onefile.bat would build with an unverified interpreter",
        )
        self.assertNotIn(
            "sys.version_info >= (3, 10)",
            BUILD_ONEFILE_BAT,
            "build-onefile.bat still accepts any Python version for a release build",
        )

    def test_the_check_really_verifies_interpreter_and_pins(self):
        self.assertIn("platform.python_version()", BUILD_ENV_TOOL)
        self.assertIn("pointer_bits()", BUILD_ENV_TOOL)
        self.assertIn("PyInstaller.__version__", BUILD_ENV_TOOL)

    def test_ci_runs_the_environment_check(self):
        self.assertIn("tools/check_build_env.py --mode release", CI)
        self.assertIn("tools/check_build_env.py --mode dev", CI)

    def test_the_release_job_runs_the_environment_check(self):
        self.assertIn("tools/check_build_env.py --mode release", RELEASE)

    def test_the_build_scripts_also_gate_the_source_closure(self):
        self.assertIn("tools\\check_source_imports.py", BUILD_ONEFILE_BAT)


class BuildDependencyInstallGateTests(unittest.TestCase):
    """The release toolchain check is useless before PyInstaller is installed.

    ``check_build_env.py --mode release`` verifies the pinned PyInstaller.  A job
    that runs it against a bare interpreter fails for the wrong reason, or -- worse
    -- is "fixed" by dropping the check.  Install ``requirements-build.txt`` first.
    """

    JOBS = (("windows-ci.yml", CI), ("release.yml", RELEASE))

    def assert_installs_before_release_check(self, text: str, job: str) -> None:
        steps = job_steps(text, job)
        self.assertTrue(steps, f"{job}: no steps found")
        install = step_running(steps, "-r requirements-build.txt")
        self.assertIsNotNone(
            install, f"{job} never installs requirements-build.txt (PyInstaller 6.22.3)"
        )
        release_check = step_running(steps, "check_build_env.py --mode release")
        self.assertIsNotNone(
            release_check, f"{job} does not run the release environment check"
        )
        self.assertLess(
            install,
            release_check,
            f"{job} runs the release environment check before installing "
            "requirements-build.txt",
        )

    def test_source_artifact_installs_build_requirements_before_the_release_check(self):
        # This is the job that regressed: it verified the release toolchain while
        # only pytest and ruff were installed.
        self.assert_installs_before_release_check(CI, "source-artifact")

    def test_release_job_installs_build_requirements_before_the_release_check(self):
        self.assert_installs_before_release_check(RELEASE, "build-release")

    def test_every_release_check_runs_after_the_build_dependencies_are_installed(self):
        # Applies to every job in both workflows, not just the two known ones, so
        # a future job cannot reintroduce the same defect.
        checked = 0
        for label, text in self.JOBS:
            for job in job_names(text):
                steps = job_steps(text, job)
                release_check = step_running(steps, "check_build_env.py --mode release")
                if release_check is None:
                    continue
                checked += 1
                install = step_running(steps, "-r requirements-build.txt")
                self.assertIsNotNone(
                    install,
                    f"{label}:{job} checks the release toolchain but never installs "
                    "requirements-build.txt",
                )
                self.assertLess(
                    install,
                    release_check,
                    f"{label}:{job} checks the release toolchain before installing "
                    "requirements-build.txt",
                )
        self.assertGreaterEqual(checked, 2, "no release environment checks found")


def job_names(text: str) -> list[str]:
    return re.findall(r"^  ([a-zA-Z_-]+):\n", text, re.MULTILINE)


class FrozenRuntimeVerifierTests(unittest.TestCase):
    """The release must refuse an EXE that does not carry the 3.14 runtime."""

    def setUp(self):
        root = str(ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        from tools import verify_frozen_runtime as module

        self.module = module
        self.good = {
            "base_library.zip",
            "_tkinter.pyd",
            "python314.dll",
            "tcl90.dll",
            "tcl9tk90.dll",
        }
        # The archive contents are stubbed, so the checker only has to see a file
        # that exists.  The real dist/ artifact must not be a test prerequisite:
        # these tests also run inside the published source archive, which has no
        # dist/ directory and no toolchain to build one.
        handle, name = tempfile.mkstemp(suffix=".exe")
        os.close(handle)
        self.artifact = Path(name)
        self.addCleanup(self.artifact.unlink, True)
        self._real = module.archive_entry_names
        self.addCleanup(setattr, module, "archive_entry_names", self._real)

    def stub(self, names: set[str]):
        self.module.archive_entry_names = lambda _exe: names

    def test_a_314_artifact_passes(self):
        self.stub(self.good)
        self.assertEqual(self.module.check(self.artifact), [])

    def test_a_missing_interpreter_is_rejected(self):
        self.stub(self.good - {"python314.dll"})
        problems = self.module.check(self.artifact)
        self.assertTrue(any("python314.dll" in problem for problem in problems))

    def test_a_foreign_python_dll_is_rejected(self):
        # An artifact built on 3.13 carries python313.dll; the pin exists because
        # its frozen Tcl/Tk has never been smoke-tested.
        self.stub((self.good - {"python314.dll"}) | {"python313.dll"})
        problems = self.module.check(self.artifact)
        self.assertTrue(any("python314.dll" in problem for problem in problems))

    def test_a_missing_tcl_runtime_is_rejected(self):
        self.stub(self.good - {"tcl90.dll"})
        problems = self.module.check(self.artifact)
        self.assertTrue(any("tcl90.dll" in problem for problem in problems))

    def test_a_missing_artifact_is_rejected(self):
        self.stub(self.good)
        problems = self.module.check(ROOT / "dist" / "does-not-exist.exe")
        self.assertTrue(problems)


class DocumentedToolchainTests(unittest.TestCase):
    """README and docs/build.md must state the same production contract."""

    DOCS = (("README.md", README), ("docs/build.md", BUILD_DOC))

    def test_both_documents_state_the_release_toolchain(self):
        for name, text in self.DOCS:
            self.assertIn("3.14", text, f"{name} does not name Python 3.14")
            self.assertIn("x64", text, f"{name} does not require a 64-bit interpreter")
            self.assertIn("6.22.3", text, f"{name} does not pin PyInstaller 6.22.3")

    def test_no_document_gives_release_guidance_for_another_python(self):
        # Older interpreters may appear only in a sentence that explicitly limits
        # them to running the sources, never in build/release guidance.
        stale = ("3.11", "3.12", "3.13", "3.15")
        build_context = ("сборк", "build", "release", "релиз", "треб")
        for name, text in self.DOCS:
            for number, line in enumerate(text.splitlines(), 1):
                if not any(candidate in line for candidate in stale):
                    continue
                self.assertFalse(
                    any(word in line.lower() for word in build_context),
                    f"{name}:{number} gives build/release guidance for Python {number}: "
                    f"{line.strip()}",
                )

    def test_the_documented_source_compatibility_is_labelled_as_such(self):
        self.assertIn("3.10+", README)
        self.assertIn("исходник", README.lower())


class SourceCompatibilityGateTests(unittest.TestCase):
    """Broader Python versions are source/development compatibility only.

    Production is CPython 3.14 x64.  Running the sources on 3.10 is a
    convenience for developers, never a licence to publish an artifact built by
    another interpreter -- so the compatibility job must not build one.
    """

    def test_the_minimum_supported_version_is_ten(self):
        self.assertIn("SOURCE_PYTHON_MINIMUM = (3, 10)", BUILD_ENV_TOOL)
        self.assertTrue((ROOT / "README.md").exists())

    def test_the_compatibility_matrix_includes_the_release_interpreter(self):
        versions = re.findall(r'"(\d+\.\d+)"', job_section(CI, "test"))
        self.assertIn("3.14", versions)
        self.assertIn("3.10", versions)

    def test_the_compatibility_matrix_never_builds_or_gates_a_release(self):
        section = job_section(CI, "test")
        self.assertNotIn("--mode release", section)
        for builder in ("build-onefile.bat", "build.bat", "PyInstaller", "arch.bat"):
            self.assertNotIn(builder, section, f"the compatibility job runs {builder}")

    def test_the_release_toolchain_is_314_x64_only(self):
        self.assertIn("RELEASE_PYTHON = (3, 14)", BUILD_ENV_TOOL)
        self.assertIn('RELEASE_ARCHITECTURE = "64bit"', BUILD_ENV_TOOL)
        # Meaningful equality, not a >= comparison that would accept 3.15 too.
        self.assertIn("sys.version_info[:2] != RELEASE_PYTHON", BUILD_ENV_TOOL)
        self.assertIn("pointer_bits() != 64", BUILD_ENV_TOOL)


class ReleaseManifestTests(unittest.TestCase):
    """The published pair must be attested by the bytes that are actually shipped.

    The defect this replaces: a local build left the *previous* release's checksum
    next to a new EXE, so a user could download a pair that fails verification while
    the repository still claimed those gates had passed for it.
    """

    def setUp(self):
        self.tool_path = ROOT / "tools" / "release_manifest.py"
        self.tool = self.tool_path.read_text(encoding="utf-8")
        sys.path.insert(0, str(ROOT / "tools"))

        self.module = importlib.import_module("release_manifest")
        self.addCleanup(sys.path.remove, str(ROOT / "tools"))

    def sandbox(self) -> Path:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        return Path(tmp)

    def test_the_release_job_writes_and_verifies_the_manifest(self):
        steps = job_steps(RELEASE, "build-release")
        write = step_running(steps, "release_manifest.py --write")
        verify = step_running(steps, "release_manifest.py --verify")
        self.assertIsNotNone(write, "the release does not write a release manifest")
        self.assertIsNotNone(verify, "the release does not verify the published pair")
        # The two are one step: the manifest is written and then verified, in that
        # order, so a mismatching pair cannot reach the publication step.
        self.assertEqual(write, verify)
        self.assertIn("release_manifest.py --write", steps[write][1])
        self.assertIn("release_manifest.py --verify", steps[verify][1])
        self.assertLess(
            steps[write][1].index("--write"), steps[verify][1].index("--verify")
        )
        # ... after the gates that attest the bytes.
        for gate in ("runtime_smoke.py", "arch.bat", "verify_source_archive.ps1"):
            self.assertLess(step_running(steps, gate), write)
        # The manifest itself is published, so the attestation travels with the files.
        self.assertIn('"release-manifest.json"', RELEASE)

    def test_a_build_invalidates_the_previous_attestation(self):
        # Otherwise a rebuild after a validated release keeps publishing an old
        # checksum as if it described the new bytes.
        self.assertIn("release_manifest.py --invalidate", BUILD_ONEFILE_BAT)
        invalidation = BUILD_ONEFILE_BAT.index("--invalidate")
        build = BUILD_ONEFILE_BAT.index("-m PyInstaller")
        self.assertLess(invalidation, build, "the attestation outlives the rebuild")
        self.assertIn("def invalidate(", self.tool)

    def test_the_manifest_refuses_to_attest_bytes_without_every_gate(self):
        for gate in self.module.REQUIRED_GATES:
            self.assertIn(gate, RELEASE, f"gate {gate} is never recorded for the manifest")
        problems = self.module.check_gates({name: True for name in self.module.REQUIRED_GATES})
        self.assertEqual(problems, [])
        missing = self.module.check_gates({})
        self.assertTrue(any("was not recorded" in problem for problem in missing))
        failed = self.module.check_gates(
            {name: (name != "runtime_smoke_all") for name in self.module.REQUIRED_GATES}
        )
        self.assertTrue(any("did not pass" in problem for problem in failed))

    def test_the_manifest_binds_both_published_artifacts_and_the_revision(self):
        self.assertIn(PRODUCTION_ARTIFACT, self.tool)
        self.assertIn("LookUpWindows-src.zip", self.tool)
        self.assertIn('"revision": current_revision', self.tool)
        self.assertIn('"sha256": sha256_of(path)', self.tool)
        self.assertIn('"bytes": path.stat().st_size', self.tool)
        # The source fingerprint moves with the tree, so the manifest also says
        # which sources the published bytes were produced from.
        self.assertIn("def source_fingerprint()", self.tool)
        self.assertIn('"digest": combined', self.tool)
        self.assertIn("source_relative_paths", self.tool)
        self.assertNotIn("SOURCE_GLOBS", self.tool)

    def test_verification_recomputes_every_hash(self):
        verify = self.tool.split("def verify(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("sha256_of(candidate)", verify)
        self.assertIn("does not describe the current", verify)
        self.assertIn("hashes to", verify)
        self.assertIn("is missing", verify)

    def test_writing_the_manifest_produces_the_sidecars_it_verifies(self):
        root = self.sandbox()
        exe = root / "dist" / "LookUpWindows.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"binary")
        (root / "LookUpWindows-src.zip").write_bytes(b"zip")
        gates = root / "gates.json"
        gates.write_text(
            json.dumps({name: True for name in self.module.REQUIRED_GATES}), encoding="utf-8"
        )
        original_root = self.module.ROOT
        original_tree_problem = self.module.git_tree_problem
        original_revision = self.module.git_revision
        original_fingerprint = self.module.source_fingerprint
        self.module.ROOT = root
        self.module.git_tree_problem = lambda: None
        self.module.git_revision = lambda: "deadbeef"
        self.module.source_fingerprint = lambda: {"digest": "source", "fileCount": 0}
        self.addCleanup(setattr, self.module, "ROOT", original_root)
        self.addCleanup(setattr, self.module, "git_tree_problem", original_tree_problem)
        self.addCleanup(setattr, self.module, "git_revision", original_revision)
        self.addCleanup(setattr, self.module, "source_fingerprint", original_fingerprint)
        try:
            self.assertEqual(self.module.record_gates(gates), 0)
            self.assertEqual(self.module.write(gates), 0)
            self.assertEqual(self.module.verify(), 0)
            # A rebuild changes the bytes: the old attestation must stop verifying.
            exe.write_bytes(b"different binary")
            self.assertEqual(self.module.verify(), 1)
            # ... and invalidating removes it, so nothing stale can be published.
            self.assertEqual(self.module.invalidate(), 0)
            self.assertFalse((root / "release-manifest.json").exists())
            self.assertEqual(self.module.verify(), 1)
        finally:
            self.module.ROOT = original_root

    def test_writing_is_refused_when_an_artifact_is_missing(self):
        root = self.sandbox()
        gates = root / "gates.json"
        gates.write_text(
            json.dumps({name: True for name in self.module.REQUIRED_GATES}), encoding="utf-8"
        )
        original_root = self.module.ROOT
        self.module.ROOT = root
        self.addCleanup(setattr, self.module, "ROOT", original_root)
        try:
            self.assertEqual(self.module.write(gates), 1)
            self.assertFalse((root / "release-manifest.json").exists())
        finally:
            self.module.ROOT = original_root

    def test_the_manifest_is_never_created_by_the_build_itself(self):
        # The build cannot know that the gates passed for the bytes it produced:
        # only the release job may write the attestation.
        for line in BUILD_ONEFILE_BAT.splitlines():
            if line.startswith("python") and "release_manifest" in line:
                self.assertIn("--invalidate", line)
        self.assertNotIn("sha256", BUILD_ONEFILE_BAT.lower())


class BuildScriptTests(unittest.TestCase):
    def test_arch_script_delegates_to_the_packaging_tool(self):
        self.assertIn("package_source.ps1", ARCH_BAT)

    def test_build_script_stops_the_instance_gracefully(self):
        script = BUILD_ONEFILE_BAT
        self.assertIn("tools\\stop_running_instance.py", script)
        self.assertNotIn("taskkill", script.lower())


if __name__ == "__main__":
    unittest.main()
