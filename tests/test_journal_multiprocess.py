"""The recovery journal must survive concurrent writers in real processes.

The journal is the durable ownership record for a foreign window that LookUp
parked off-screen, and it has two or more writers: the running application and
a recovery guardian that outlives it (plus, realistically, a second LookUp
start while the guardian is still executing).  These tests reproduce the
lost-update and double-claim races with separate processes, because an
in-process lock and an in-memory cache cannot demonstrate anything about them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from recovery import (  # noqa: E402  (after source-path bootstrap)
    JOURNAL_VERSION,
    STATE_INTENT,
    STATE_PARKED,
)

ROOT = Path(__file__).resolve().parent.parent
WORKER = ROOT / "tests" / "journal_worker.py"


def read_records(path: Path) -> list[dict]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    records = document.get("records")
    return records if isinstance(records, list) else []


def expire_lease(path: Path, hwnd: int) -> None:
    """Age one record's lease into the past, the way a stalled executor leaves it."""
    document = json.loads(path.read_text(encoding="utf-8"))
    for record in document.get("records", []):
        if int(record.get("hwnd") or 0) == int(hwnd):
            record["claimUntil"] = time.time() - 60.0
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


class MultiprocessJournalTests(unittest.TestCase):
    """Every test here arranges an interleaving that used to lose an obligation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.path = self.tmpdir / "settings.json.park.json"
        self._processes: list[subprocess.Popen] = []

    def tearDown(self):
        for process in self._processes:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
        for process in self._processes:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        self._tmp.cleanup()

    def marker(self, name: str) -> Path:
        return self.tmpdir / name

    def wait_for(self, path: Path, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(path.exists(), f"synchronization marker never appeared: {path.name}")

    def start(self, spec: str, **kwargs) -> subprocess.Popen:
        marker = kwargs.pop("marker", None)
        process = subprocess.Popen(
            [sys.executable, str(WORKER), str(self.path), spec],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self._processes.append(process)
        if marker is not None:
            self.wait_for(marker)
        return process

    def finish(self, process: subprocess.Popen, timeout: float = 120.0) -> dict:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.fail(f"journal worker timed out: {stderr}")
        self.assertEqual(process.returncode, 0, f"worker failed: {stderr}")
        return json.loads(stdout)

    def report_of(self, result: dict, op: str, hwnd: int | None = None) -> dict:
        for entry in result.get("report", []):
            if entry.get("op") == op and (hwnd is None or entry.get("hwnd") == hwnd):
                return entry
        self.fail(f"worker did not report {op} for hwnd={hwnd}: {result}")

    # ------------------------------------------------------------------ #
    def test_a_stale_writer_cannot_erase_a_newer_obligation(self):
        # A loads [A] -> B adds B -> A marks A parked from its older state.
        # The transactional journal must merge instead of clobbering.
        a = self.start(
            "intent:1001;"
            "touch:{A_LOADED};"
            "wait:{B_DONE};"
            "parked:1001;"
            "touch:{A_DONE}".format(
                A_LOADED=self.marker("a_loaded"),
                B_DONE=self.marker("b_done"),
                A_DONE=self.marker("a_done"),
            )
        )
        b = self.start(
            "wait:{B_GO};intent:2002;touch:{B_DONE}".format(
                B_GO=self.marker("b_go"), B_DONE=self.marker("b_done")
            )
        )
        # A's intent is durable before B is released, and B's intent is durable
        # before A is allowed to continue.
        self.wait_for(self.marker("a_loaded"))
        self.marker("b_go").touch()
        self.wait_for(self.marker("b_done"))
        self.finish(b)
        self.finish(a)

        records = {int(item["hwnd"]): item for item in read_records(self.path)}
        self.assertEqual(sorted(records), [1001, 2002])
        self.assertEqual(records[1001]["state"], STATE_PARKED, "A's own mutation was lost")
        self.assertEqual(records[2002]["state"], STATE_INTENT, "B was mutated by a writer that never saw it")

    def test_two_processes_cannot_both_claim_the_same_record(self):
        a = self.start(
            "intent:4242;"
            "touch:{A_LOADED};"
            "wait:{B_LOADED};"
            "claim:4242;"
            "touch:{A_CLAIMED}".format(
                A_LOADED=self.marker("a_loaded"),
                B_LOADED=self.marker("b_loaded"),
                A_CLAIMED=self.marker("a_claimed"),
            )
        )
        b = self.start(
            "wait:{B_GO};"
            "touch:{B_LOADED};"
            "wait:{A_CLAIMED};"
            "claim:4242".format(
                B_GO=self.marker("b_go"),
                B_LOADED=self.marker("b_loaded"),
                A_CLAIMED=self.marker("a_claimed"),
            )
        )
        self.wait_for(self.marker("a_loaded"))
        self.marker("b_go").touch()
        self.wait_for(self.marker("b_loaded"))
        result_a = self.finish(a)
        result_b = self.finish(b)

        outcomes = set()
        for result in (result_a, result_b):
            for entry in result.get("report", []):
                if entry.get("op") == "claim" and entry.get("hwnd") == 4242:
                    outcomes.add(bool(entry.get("ok")))
        # Exactly one process was granted the lease, and it is the durable one.
        self.assertEqual(outcomes, {True, False}, "both processes believe they claimed the record")
        record = read_records(self.path)[0]
        self.assertIn(int(record["claimPid"]), (result_a["pid"], result_b["pid"]))

    def test_a_stale_executor_cannot_end_a_newer_claim(self):
        # The ABA race in real processes: A claims, stalls, loses its lease, B
        # claims the same record with a higher generation, and only then does A
        # try to finish the job it started.  A must be rejected, or it would clear
        # an obligation that is now B's to discharge.
        stalled = self.start(
            "intent:9000;"
            "claim:9000;"
            "touch:{A_CLAIMED};"
            "wait:{B_CLAIMED};"
            "stale-clear:9000".format(
                A_CLAIMED=self.marker("a_claimed"),
                B_CLAIMED=self.marker("b_claimed"),
            )
        )
        self.wait_for(self.marker("a_claimed"))
        # A's claim is durable before anything else happens, at generation 1.
        self.assertEqual(read_records(self.path)[0]["claimGeneration"], 1)
        # A's lease expires the way a stalled executor's lease does.
        expire_lease(self.path, 9000)
        successor = self.start("claim:9000;touch:{B_CLAIMED}".format(B_CLAIMED=self.marker("b_claimed")))
        self.wait_for(self.marker("b_claimed"))
        taken = self.report_of(self.finish(successor), "claim", 9000)
        self.assertTrue(taken["ok"], "the expired lease could not be taken over")
        self.assertEqual(taken["generation"], 2)
        result = self.finish(stalled)
        rejected = self.report_of(result, "stale-clear", 9000)
        self.assertFalse(rejected["ok"], "a stale executor ended a newer claim")
        record = read_records(self.path)[0]
        self.assertEqual(int(record["hwnd"]), 9000, "the obligation was lost by the stale clear")
        self.assertEqual(record["claimGeneration"], 2)

    def test_a_guardian_clearing_one_record_keeps_another_process_record(self):
        # The guardian renews and clears A while a freshly started LookUp
        # registers B: B must survive the guardian's write.
        guardian = self.start(
            "intent:1001;"
            "claim:1001;"
            "renew:1001;"
            "touch:{G_READY};"
            "wait:{NEW_DONE};"
            "clear:1001".format(
                G_READY=self.marker("g_ready"), NEW_DONE=self.marker("new_done")
            )
        )
        new_app = self.start(
            "wait:{NEW_GO};intent:2002;touch:{NEW_DONE}".format(
                NEW_GO=self.marker("new_go"), NEW_DONE=self.marker("new_done")
            )
        )
        self.wait_for(self.marker("g_ready"))
        self.marker("new_go").touch()
        self.wait_for(self.marker("new_done"))
        self.finish(new_app)
        self.finish(guardian)

        self.assertEqual([int(item["hwnd"]) for item in read_records(self.path)], [2002])

    def test_a_new_application_park_survives_the_guardian_clearing_its_own(self):
        guardian = self.start(
            "intent:1001;"
            "claim:1001;"
            "touch:{G_READY};"
            "wait:{B_DONE};"
            "clear:1001".format(G_READY=self.marker("g_ready"), B_DONE=self.marker("b_done"))
        )
        new_app = self.start(
            "wait:{NEW_GO};intent:2002;parked:2002;touch:{B_DONE}".format(
                NEW_GO=self.marker("new_go"), B_DONE=self.marker("b_done")
            )
        )
        self.wait_for(self.marker("g_ready"))
        self.marker("new_go").touch()
        self.wait_for(self.marker("b_done"))
        self.finish(new_app)
        self.finish(guardian)

        records = {int(item["hwnd"]): item for item in read_records(self.path)}
        self.assertEqual(sorted(records), [2002])
        self.assertEqual(records[2002]["state"], STATE_PARKED)

    def test_many_concurrent_mutations_lose_nothing_and_double_claim_nothing(self):
        workers = 8
        shared = 9000

        # Phase 1: every worker registers and commits its own obligation while
        # all the others are writing; nothing may be lost.
        phase_one = []
        for index in range(workers):
            own = 1000 + index
            spec = f"sleep:{0.01 * (index % 4)};intent:{own};parked:{own}"
            if index == 0:
                spec += f";intent:{shared}"
            phase_one.append(self.start(spec))
        for process in phase_one:
            self.finish(process, timeout=180)

        records = {int(item["hwnd"]): item for item in read_records(self.path)}
        for index in range(workers):
            own = 1000 + index
            self.assertIn(own, records, f"worker {index} lost its own record")
            self.assertEqual(records[own]["state"], STATE_PARKED, f"worker {index} lost its parked state")

        # Phase 2: every worker races for the lease on one contested record.
        phase_two = []
        for _index in range(workers):
            phase_two.append(self.start(f"sleep:0.01;claim:{shared};records"))
        results = [self.finish(process, timeout=180) for process in phase_two]

        claims = [
            entry
            for result in results
            for entry in result.get("report", [])
            if entry.get("op") == "claim" and entry.get("hwnd") == shared and entry.get("ok")
        ]
        self.assertEqual(len(claims), 1, "the lease was granted to more than one process")
        records = {int(item["hwnd"]): item for item in read_records(self.path)}
        self.assertEqual(int(records[shared]["claimPid"]), int(claims[0]["claimPid"]))

        # The document stays a journal and no writer is left behind on disk.
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(document["version"], JOURNAL_VERSION)
        self.assertGreaterEqual(int(document.get("revision", 0)), workers * 2)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temporary files leaked: {leftovers}")

        # Draining the journal afterwards must leave a clean, empty file.
        drain = self.start(
            ";".join([f"clear:{1000 + index}" for index in range(workers)] + [f"clear:{shared}"])
        )
        self.finish(drain, timeout=120)
        self.assertFalse(self.path.exists(), "the journal survived a full drain")

    def test_the_journal_lock_is_released_when_a_process_is_killed(self):
        # A hard kill must not strand every other writer behind a stale lock, and
        # the record the dead process had already committed must survive the next
        # writer's transaction.
        #
        # The holder is killed while it demonstrably owns the inter-process lock
        # (``hold:`` announces that from inside the transaction), and the first
        # obligation is committed by a process that has already exited. Both
        # facts are established by markers and reports, never by a sleep: a
        # timed kill could land before the atomic replace and would then assert
        # something the transaction is not required to guarantee.
        owner = self.start("intent:1001")
        self.assertTrue(self.report_of(self.finish(owner, timeout=60), "intent", 1001)["ok"])

        held = self.marker("lock-held")
        holder = self.start(f"hold:{held}")
        self.wait_for(held)
        holder.kill()
        holder.wait(timeout=30)

        competitor = self.start("intent:2002;records")
        result = self.finish(competitor, timeout=120)
        self.assertTrue(self.report_of(result, "intent", 2002)["ok"])
        hwnds = [int(item["hwnd"]) for item in read_records(self.path)]
        self.assertEqual(sorted(hwnds), [1001, 2002])


if __name__ == "__main__":
    unittest.main()
