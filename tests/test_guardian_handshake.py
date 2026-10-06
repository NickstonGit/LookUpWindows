"""Real pipe/process tests for bounded guardian readiness and failed startup."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import restoreguard  # noqa: E402 (after source-path bootstrap)


class GuardianHandshakeTests(unittest.TestCase):
    def child(self, script):
        process = subprocess.Popen([sys.executable, "-u", "-c", script], stdout=subprocess.PIPE)
        self.addCleanup(self.cleanup, process)
        return process

    @staticmethod
    def cleanup(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def test_a_live_silent_child_does_not_defeat_the_timeout(self):
        child = self.child("import time; time.sleep(60)")
        started = time.monotonic()
        self.assertFalse(restoreguard._wait_for_token(child, .5))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(child.poll(), "the deadline must work even while the writer stays alive")

    def test_a_partial_token_also_times_out_without_blocking_close(self):
        child = self.child("import sys,time; sys.stdout.write('rea'); sys.stdout.flush(); time.sleep(60)")
        started = time.monotonic()
        self.assertFalse(restoreguard._wait_for_token(child, .5))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_token_split_across_writes_is_accepted(self):
        child = self.child("import sys,time; sys.stdout.write('rea'); sys.stdout.flush(); time.sleep(.1); print('dy'); time.sleep(60)")
        self.assertTrue(restoreguard._wait_for_token(child, 5))
        self.assertIsNone(child.poll())

    def test_eof_without_ready_is_rejected(self):
        self.assertFalse(restoreguard._wait_for_token(self.child("pass"), 5))

    def test_an_invalid_token_is_rejected(self):
        self.assertFalse(restoreguard._wait_for_token(self.child("print('wrong')"), 5))

    @unittest.skipUnless(os.name == "nt", "Windows onefile launcher tree cleanup")
    def test_failed_startup_cleans_the_launcher_and_its_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            go = Path(tmp) / "go"
            script = (
                "import subprocess,sys,time\n"
                "from pathlib import Path\n"
                f"go=Path({str(go)!r})\n"
                "while not go.exists(): time.sleep(.01)\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"Path({str(marker)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            real_wait_for_token = restoreguard._wait_for_token

            def wait_after_adoption(process, timeout, prefix=restoreguard.GUARDIAN_READY_TOKEN):
                # _spawn calls _wait_for_token only after the launcher has been
                # adopted into the temporary kill-on-close job.  Release the
                # fake launcher here so its child is deterministically born into
                # that job rather than racing the adoption call.
                go.touch()
                return real_wait_for_token(process, timeout, prefix)

            # This test is about *failed-startup tree cleanup*, not about whether
            # the machine hosting the test permits CREATE_BREAKAWAY_FROM_JOB.
            # GitHub-hosted Windows runners may themselves live in a Job Object
            # that denies breakaway, in which case production correctly fails
            # closed before a fake launcher can start.  Exercise the later cleanup
            # branch deterministically while leaving production policy unchanged.
            with (
                patch.object(
                    restoreguard,
                    "guardian_command",
                    return_value=[sys.executable, "-c", script],
                ),
                patch.object(
                    restoreguard,
                    "_guardian_creation_flags",
                    return_value=restoreguard._CREATE_NO_WINDOW,
                ),
                patch.object(restoreguard, "process_in_any_job", return_value=False),
                patch.object(restoreguard, "_wait_for_token", side_effect=wait_after_adoption),
            ):
                started = time.monotonic()
                self.assertIsNone(restoreguard.spawn_guardian(Path(tmp) / "journal", timeout=2))
                self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(marker.exists(), "the fake onefile launcher did not spawn its child")
            child_pid = int(marker.read_text())
            self.assertFalse(restoreguard.process_is_alive(child_pid), "unready onefile child survived cleanup")


if __name__ == "__main__":
    unittest.main()
