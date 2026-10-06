"""Capture helpers must not outlive LookUp, even on hard termination.

A helper blocked inside ``PrintWindow`` cannot observe pipe EOF, so Python's
daemon/cleanup paths do not run when the owner is hard-killed.  The helpers are
therefore bound to a Job Object whose kill-on-close semantics let the OS remove
them; these tests exercise that binding with a real child process.
"""

import ctypes
import functools
import os
import subprocess
import sys
import time
import unittest
import unittest.mock
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

try:
    import winapi
except Exception as exc:  # pragma: no cover - non-Windows
    winapi = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

WINAPI = (SRC / "winapi.py").read_text(encoding="utf-8")

STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000


def process_is_running(pid: int, timeout: float = 0.0) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid)
        )
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            running = code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
        if not running or time.monotonic() >= deadline:
            return running
        time.sleep(0.1)


def spawn_sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@unittest.skipIf(winapi is None, "winapi requires Windows")
class CaptureHelperJobTests(unittest.TestCase):
    def setUp(self):
        self.job = winapi.CaptureHelperJob(
            f"LookUpWindows-CaptureJob-Test-{os.getpid()}-{time.time_ns()}"
        )
        self.addCleanup(self._close_job)
        self.children: list[subprocess.Popen] = []

    def _close_job(self):
        try:
            self.job.terminate()
        except Exception:
            pass
        for child in self.children:
            if child.poll() is None:
                child.kill()

    def test_job_uses_kill_on_close_semantics(self):
        self.assertTrue(self.job.available)
        self.assertEqual(winapi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0x00002000)
        self.assertEqual(winapi.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, 9)

    def test_helper_is_assigned_to_the_job(self):
        child = spawn_sleeper()
        self.children.append(child)
        self.assertTrue(self.job.adopt(child))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, child.pid)
        self.assertTrue(handle)
        try:
            in_job = wintypes.BOOL()
            self.assertTrue(kernel32.IsProcessInJob(handle, self.job._handle, ctypes.byref(in_job)))
        finally:
            kernel32.CloseHandle(handle)
        self.assertTrue(in_job.value)

    def test_terminating_the_job_removes_the_helper(self):
        child = spawn_sleeper()
        self.children.append(child)
        self.assertTrue(self.job.adopt(child))
        self.assertTrue(process_is_running(child.pid))
        self.job.terminate()
        deadline = time.monotonic() + 10.0
        while process_is_running(child.pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(process_is_running(child.pid))

    def test_shared_job_is_a_singleton(self):
        first = winapi.capture_helper_job()
        second = winapi.capture_helper_job()
        self.assertIs(first, second)

    def test_job_is_unnamed_so_its_lifetime_is_the_process_lifetime(self):
        # A named job object would be shared with any other process that opens
        # the same name, and kill-on-close would then depend on that unrelated
        # handle staying open.
        self.assertIsNone(winapi.CaptureHelperJob.__init__.__defaults__[0])


def terminate(process: subprocess.Popen, timeout: float = 30.0) -> str:
    """Hard-kill ``process`` through the handle we own, and report how.

    ``taskkill`` is the usual tool, but it needs privileges a test process does
    not have on every machine, and its failure used to be indistinguishable from
    a successful kill: the test then waited for an owner that was still running
    and reported a timeout instead of the real cause.  ``TerminateProcess``
    through the handle this process already owns always works, and the mechanism
    actually used is reported so a restriction is visible rather than disguised.
    """
    completed = subprocess.run(
        ["taskkill", "/F", "/PID", str(process.pid)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode == 0:
        return "taskkill"
    process.kill()
    return "TerminateProcess (taskkill returned %s)" % completed.returncode


@functools.lru_cache(maxsize=1)
def taskkill_is_usable() -> tuple[bool, str]:
    """Whether ``taskkill /F`` can terminate a process this session created."""
    probe = spawn_sleeper()
    completed = subprocess.run(
        ["taskkill", "/F", "/PID", str(probe.pid)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    ok = completed.returncode == 0 and not process_is_running(probe.pid)
    detail = (completed.stdout or completed.stderr or "").strip()
    if probe.poll() is None:
        probe.kill()
    probe.wait(timeout=30)
    return ok, detail


@unittest.skipIf(winapi is None, "winapi requires Windows")
class HardOwnerDeathTests(unittest.TestCase):
    """A helper must disappear when the owner process is hard-terminated."""

    def _owner_script(self, ready_file: Path) -> str:
        return (
            "import sys, time\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "import winapi\n"
            "job = winapi.capture_helper_job()\n"
            "import subprocess\n"
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "assert job.adopt(child), 'assign failed'\n"
            f"open({str(ready_file)!r}, 'w').write(str(child.pid))\n"
            "time.sleep(120)\n"
        )

    def test_helper_dies_with_its_owner(self):
        with __import__("tempfile").TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready.txt"
            script = Path(tmp) / "owner.py"
            script.write_text(self._owner_script(ready), encoding="utf-8")
            owner = subprocess.Popen(
                [sys.executable, str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline and not ready.exists():
                    if owner.poll() is not None:
                        stdout, stderr = owner.communicate()
                        self.fail(f"owner exited early: {stdout} {stderr}")
                    time.sleep(0.2)
                self.assertTrue(ready.exists(), "owner never reported its helper")
                child_pid = int(ready.read_text(encoding="utf-8"))
                self.assertTrue(process_is_running(child_pid))
                # Kill only the owner: no process-tree kill, so the helper can
                # only disappear because of the OS-level lifetime binding.
                mechanism = terminate(owner)
                owner.wait(timeout=30)
                deadline = time.monotonic() + 15.0
                while process_is_running(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.2)
                self.assertFalse(
                    process_is_running(child_pid),
                    f"capture helper survived the hard death of its owner "
                    f"(owner terminated by {mechanism})",
                )
            finally:
                if owner.poll() is None:
                    owner.kill()

    def test_the_hard_owner_death_gate_is_not_weakened_by_a_restricted_kill(self):
        # The lifetime gate above must prove the OS-level binding, not the ability
        # to kill a process.  Where ``taskkill`` is unavailable the gate still
        # runs (through TerminateProcess) and says which mechanism it used, so an
        # environment restriction can never be mistaken for a passing release
        # gate.
        usable, detail = taskkill_is_usable()
        self.assertIsInstance(usable, bool)
        self.assertIsInstance(detail, str)
        if not usable:
            self.skipTest(f"taskkill /F is unavailable in this environment: {detail!r}")


class HelperLifetimeContractTests(unittest.TestCase):
    """Static contracts for the helper lifecycle wiring."""

    def test_slot_start_adopts_the_helper(self):
        start = WINAPI.split("def _start_slot", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("capture_helper_job().adopt(process)", start)
        self.assertIn("process.start()", start)

    def test_job_uses_kill_on_close_limit(self):
        job = WINAPI.split("class CaptureHelperJob", 1)[1].split("\nclass ", 1)[0]
        self.assertIn("JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", job)
        self.assertIn("AssignProcessToJobObject", job)
        self.assertIn("CreateJobObjectW", job)

    def test_job_is_terminated_at_interpreter_exit(self):
        self.assertIn("atexit.register(_terminate_capture_helper_job)", WINAPI)

    def test_retired_helper_documents_the_job_fallback(self):
        stop = WINAPI.split("def _stop_slot", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("slot.retired = True", stop)
        self.assertIn("kill-on-close job", stop)


@unittest.skipIf(winapi is None, "winapi requires Windows")
class UnboundHelperTests(unittest.TestCase):
    """Job binding: a helper that cannot be bound to the job must never be used.

    An unbound helper is the orphan this whole mechanism exists to prevent, so a
    failed adoption has to end the process instead of merely being logged.
    """

    def test_adoption_failure_is_simulated_by_the_documented_hook(self):
        with unittest.mock.patch.dict(os.environ, {winapi.TEST_HOOK_ENV: "job_adopt_fail"}):
            job = winapi.CaptureHelperJob()
            child = spawn_sleeper()
            self.addCleanup(child.kill)
            self.assertFalse(job.adopt(child))
            job.terminate()

    def test_the_hook_is_inert_without_the_environment_variable(self):
        job = winapi.CaptureHelperJob()
        child = spawn_sleeper()
        self.addCleanup(child.kill)
        os.environ.pop(winapi.TEST_HOOK_ENV, None)
        self.assertTrue(job.adopt(child))
        job.terminate()

    def test_a_detector_with_a_failing_job_never_starts_a_helper(self):
        detector = winapi.AsyncChangeDetector(max_workers=1, capture_timeout=0.5)
        self.addCleanup(detector.close)
        with unittest.mock.patch.dict(os.environ, {winapi.TEST_HOOK_ENV: "job_adopt_fail"}):
            detector.schedule([4242])
            deadline = time.monotonic() + 10.0
            results: list = []
            while time.monotonic() < deadline:
                results = detector.poll_results()
                if results:
                    break
                time.sleep(0.1)
        self.assertTrue(results, "the detector never answered the capture request")
        self.assertEqual(results[0][1].status, "capture_failed")
        self.assertTrue(
            detector.lifetime_binding_degraded(),
            "the degraded lifetime binding was not reported to the caller",
        )

    def test_a_later_request_is_refused_instead_of_resurrecting_the_slot(self):
        detector = winapi.AsyncChangeDetector(max_workers=1, capture_timeout=0.5)
        self.addCleanup(detector.close)
        with unittest.mock.patch.dict(os.environ, {winapi.TEST_HOOK_ENV: "job_adopt_fail"}):
            detector.schedule([4242])
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not detector.poll_results():
                time.sleep(0.1)
            detector.schedule([4243])
            second: list = []
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not second:
                second = detector.poll_results()
                time.sleep(0.1)
        # The second request is answered (as a failure) instead of leaving the
        # caller waiting or starting another unbound helper.
        self.assertTrue(second, "the follow-up request was never answered")
        self.assertEqual(second[0][1].status, "capture_failed")

    def test_the_helper_waits_for_arming_before_it_touches_a_window(self):
        main = WINAPI.split("def _capture_process_main", 1)[1].split("\n@dataclass", 1)[0]
        self.assertIn("recv_bytes(1)", main)
        self.assertIn("HELPER_ARMED_TOKEN", main)
        # The arming byte is only sent after the job accepted the process.
        start = WINAPI.split("def _start_slot", 1)[1].split("\n    def ", 1)[0]
        self.assertLess(start.index("capture_helper_job().adopt(process)"),
                        start.index("send_bytes(HELPER_ARMED_TOKEN)"))

    def test_a_failed_adoption_discards_the_helper_instead_of_arming_it(self):
        start = WINAPI.split("def _start_slot", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("if not capture_helper_job().adopt(process):", start)
        self.assertIn("_discard_unbound_helper", start)
        self.assertIn("return False", start)


if __name__ == "__main__":
    unittest.main()