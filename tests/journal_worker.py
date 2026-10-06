"""Subprocess worker for the inter-process journal race tests.

``RecoveryJournal`` is shared between the application and a recovery guardian,
so the lost-update and double-claim regressions can only be reproduced with
real separate processes.  Each worker is driven by a compact operation spec so
the parent test can arrange exact interleavings:

    intent:4242;wait:C:\\tmp\\go;parked:4242;touch:C:\\tmp\\done

Supported tokens:

    intent:<hwnd>          register ownership before a park
    parked:<hwnd>          mark the park as committed
    claim:<hwnd>           take the fenced lease (fails when another executor owns it)
    renew:<hwnd>           extend the lease this worker holds
    release:<hwnd>         give the lease up early
    clear:<hwnd>           forget the record
    stale-clear:<hwnd>     end the record with a *previous* generation/token, i.e.
                           the fencing check a stalled executor faces (ABA)
    hold:<path>            take the inter-process journal lock, announce it by
                           creating <path>, then block forever holding it. Used to
                           kill a process while it owns the lock.
    records                report the current records
    revision               report the committed document revision
    wait:<path>            block until the file exists (synchronization point)
    touch:<path>           create the file (synchronization point)
    sleep:<seconds>        pause

Every operation is reported back as JSON so the parent can assert on outcomes
rather than on timing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recovery import (  # noqa: E402  (after sys.path bootstrap)
    STATE_INTENT,
    ExecutorIdentity,
    ParkRecord,
    RecoveryJournal,
)


def run_spec(journal_path: str, spec: str, identity: ExecutorIdentity) -> list:
    journal = RecoveryJournal(Path(journal_path))
    report = []
    claim = None
    for token in (spec or "").split(";"):
        token = token.strip()
        if not token:
            continue
        head, _, arg = token.partition(":")
        if head == "wait":
            deadline = time.time() + 60.0
            while not Path(arg).exists() and time.time() < deadline:
                time.sleep(0.02)
            report.append({"op": "wait", "present": Path(arg).exists()})
        elif head == "touch":
            Path(arg).parent.mkdir(parents=True, exist_ok=True)
            Path(arg).touch()
            report.append({"op": "touch"})
        elif head == "sleep":
            time.sleep(max(0.0, float(arg or 0)))
            report.append({"op": "sleep"})
        elif head == "hold":
            # Block *inside* a real journal transaction, so the parent can kill
            # this process at a moment when it demonstrably owns the inter-process
            # lock. Sleeping before a commit would only prove that an
            # uncommitted write may be lost, which the atomic replace guarantees
            # anyway.
            def _hold(_records):
                Path(arg).parent.mkdir(parents=True, exist_ok=True)
                Path(arg).touch()
                report.append({"op": "hold", "acquired": True})
                sys.stdout.write(json.dumps(report) + "\n")
                sys.stdout.flush()
                while True:
                    time.sleep(0.05)

            journal._transact(_hold)
            report.append({"op": "hold", "acquired": False})
        elif head == "records":
            report.append(
                {"op": "records", "hwnds": [int(record.hwnd) for record in journal.records()]}
            )
        elif head == "revision":
            report.append({"op": "revision", "value": int(journal.revision)})
        elif head == "intent":
            report.append(
                {"op": "intent", "hwnd": int(arg), "ok": bool(journal.record_intent(_make_record(int(arg), identity)))}
            )
        elif head == "parked":
            report.append({"op": "parked", "hwnd": int(arg), "ok": bool(journal.mark_parked(int(arg), identity))})
        elif head == "clear":
            report.append({"op": "clear", "hwnd": int(arg), "ok": bool(journal.clear(int(arg)))})
        elif head == "claim":
            claim = journal.claim(int(arg), identity)
            report.append(
                {
                    "op": "claim",
                    "hwnd": int(arg),
                    "ok": claim is not None,
                    "claimPid": identity.pid,
                    "generation": None if claim is None else claim.generation,
                    "token": None if claim is None else claim.token,
                }
            )
        elif head == "renew":
            report.append(
                {"op": "renew", "hwnd": int(arg), "ok": bool(claim is not None and journal.renew(claim))}
            )
        elif head == "release":
            if claim is not None:
                journal.release(claim)
            report.append({"op": "release", "hwnd": int(arg)})
        elif head == "stale-clear":
            # Exactly what a stalled executor tries: end the record with a claim
            # that a newer executor has already superseded.
            ok = claim is not None and journal.clear_claimed(claim)
            report.append(
                {
                    "op": "stale-clear",
                    "hwnd": int(arg),
                    "ok": bool(ok),
                    "generation": None if claim is None else claim.generation,
                }
            )
        else:
            report.append({"op": "unknown", "token": token})
    return report


def _make_record(hwnd: int, identity: ExecutorIdentity) -> ParkRecord:
    return ParkRecord(
        hwnd=int(hwnd),
        pid=1234,
        class_name="SomeWindowClass",
        process_name="app.exe",
        process_created=999,
        screen_rect=(10, 20, 810, 620),
        show_cmd=1,
        placement_flags=0,
        min_position=(-1, -1),
        max_position=(-1, -1),
        normal_position=(10, 20, 810, 620),
        owner_pid=identity.pid,
        owner_run_id=identity.executor_id,
        owner_created=identity.created,
        recorded_at=time.time(),
        label="app.exe - Some",
        state=STATE_INTENT,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return 2
    journal_path, spec = argv[1], argv[2]
    identity = ExecutorIdentity(
        executor_id=f"{os.getpid():x}{time.time_ns():x}"[-32:],
        pid=os.getpid(),
        created=None,
        label="journal-worker",
    )
    try:
        report = run_spec(journal_path, spec, identity)
    except Exception as exc:  # a worker crash must stay observable
        print(json.dumps({"error": f"{exc.__class__.__name__}: {exc}"}), flush=True)
        return 1
    print(json.dumps({"pid": os.getpid(), "executor": identity.executor_id, "report": report}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
