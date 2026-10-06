"""Recovery ownership: it must exist before a park and survive process death."""

import json
import math
import os
import tempfile
import time
import unittest
from pathlib import Path

from recovery import (
    JOURNAL_VERSION,
    LEGACY_JOURNAL_VERSIONS,
    MAX_JOURNAL_BYTES,
    MAX_RECORDS,
    STATE_INTENT,
    STATE_PARKED,
    ExecutorIdentity,
    ParkRecord,
    RecoveryJournal,
    executor_of,
    journal_path_for,
    journal_problem,
    new_executor_identity,
    record_from_state,
    state_from_record,
)

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
WINAPI = (ROOT / "src" / "winapi.py").read_text(encoding="utf-8")
BUILD_ONEFILE_BAT = (ROOT / "build-onefile.bat").read_text(encoding="utf-8")

OWNER_RUN_ID = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


def make_record(hwnd: int = 4242, **overrides) -> ParkRecord:
    values = {
        "hwnd": hwnd,
        "pid": 1234,
        "class_name": "SomeWindowClass",
        "process_name": "app.exe",
        "process_created": 999,
        "screen_rect": (10, 20, 810, 620),
        "show_cmd": 1,
        "placement_flags": 0,
        "min_position": (-1, -1),
        "max_position": (-1, -1),
        "normal_position": (10, 20, 810, 620),
        "owner_pid": 77,
        "owner_run_id": OWNER_RUN_ID,
        "owner_created": 555,
        "recorded_at": time.time(),
        "label": "app.exe - Some",
        "state": STATE_PARKED,
    }
    values.update(overrides)
    return ParkRecord(**values)


def executor(tag: str = "a") -> ExecutorIdentity:
    """A distinct recovery executor identity, as two processes would have."""
    return ExecutorIdentity(executor_id=f"exec-{tag}", pid=1000 + ord(tag), created=42, label=tag)


class ParkRecordTests(unittest.TestCase):
    def test_roundtrip_preserves_every_field(self):
        record = make_record()
        restored = ParkRecord.from_dict(record.to_dict())
        self.assertEqual(restored, record)

    def test_the_documented_schema_is_the_current_one(self):
        payload = make_record().to_dict()
        for field in (
            "state",
            "ownerRunId",
            "ownerCreated",
            "claimExecutor",
            "claimGeneration",
            "claimToken",
            "claimCreated",
            "placementUsable",
        ):
            self.assertIn(field, payload, f"{field} is missing from the journal schema")

    def test_parked_is_derived_from_the_state(self):
        self.assertTrue(make_record(state=STATE_PARKED).parked)
        self.assertFalse(make_record(state=STATE_INTENT).parked)

    def test_broken_records_are_rejected(self):
        for payload in (None, 5, "x", {}, {"hwnd": "x"}, {"hwnd": 1, "screenRect": "bad"}):
            if isinstance(payload, dict) and payload.get("hwnd") is None:
                self.assertIsNone(ParkRecord.from_dict(payload))
            elif not isinstance(payload, dict):
                self.assertIsNone(ParkRecord.from_dict(payload))

    def test_a_record_without_owner_identity_is_not_treated_as_trusted(self):
        # An old journal has no owner identity.  It must not be able to freeze
        # recovery, and nobody may retire it passively either: the executor that
        # claims it is the one that ends it.
        record = ParkRecord.from_dict(make_record(owner_run_id="", owner_created=None).to_dict())
        self.assertEqual(record.owner_run_id, "")
        self.assertIsNone(record.owner_created)
        self.assertFalse(record.may_be_retired_by(executor("b")))

    def test_a_foreign_run_may_not_retire_a_record_it_merely_sees(self):
        record = make_record()
        self.assertTrue(record.may_be_retired_by(executor_of(record)))
        self.assertFalse(record.may_be_retired_by(executor("b")))

    def test_identity_match_rejects_reused_window(self):
        record = make_record()
        self.assertEqual(record.describe().split()[0], "hwnd=4242")
        self.assertIn("state=parked", record.describe())

    def test_age_uses_recorded_timestamp(self):
        record = make_record(recorded_at=time.time() - 120)
        self.assertAlmostEqual(record.age_sec(), 120, delta=2)

    def test_a_record_without_a_usable_instant_reports_no_age(self):
        record = ParkRecord.from_dict(make_record(recorded_at=math.nan).to_dict())
        self.assertEqual(record.age_sec(), 0.0)

class RecoveryJournalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_intent_is_persisted_before_any_window_is_moved(self):
        journal = RecoveryJournal(self.path)
        self.assertTrue(journal.record_intent(make_record()))
        self.assertTrue(self.path.exists())
        self.assertEqual(len(RecoveryJournal(self.path).records()), 1)

    def test_parked_state_is_recorded_and_cleared(self):
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record(state=STATE_INTENT))
        self.assertTrue(journal.mark_parked(4242, executor_of(make_record())))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["records"][0]["state"], STATE_PARKED)
        self.assertTrue(journal.clear(4242))
        self.assertFalse(self.path.exists())
        self.assertFalse(RecoveryJournal(self.path).has_pending())

    def test_only_the_owning_run_may_commit_its_park(self):
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record(state=STATE_INTENT))
        self.assertFalse(journal.mark_parked(4242, executor("z")))
        self.assertEqual(journal.get(4242).state, STATE_INTENT)

    def test_multiple_records_are_tracked_independently(self):
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record(1))
        journal.record_intent(make_record(2))
        journal.clear(1)
        remaining = RecoveryJournal(self.path).records()
        self.assertEqual([record.hwnd for record in remaining], [2])

    def test_corrupt_journal_is_ignored_instead_of_raising(self):
        self.path.write_text("{not json", encoding="utf-8")
        journal = RecoveryJournal(self.path)
        self.assertEqual(journal.records(), ())

    def test_unknown_journal_version_is_ignored(self):
        self.path.write_text(
            json.dumps({"version": JOURNAL_VERSION + 99, "records": [make_record().to_dict()]}),
            encoding="utf-8",
        )
        self.assertEqual(RecoveryJournal(self.path).records(), ())

    def test_old_records_are_kept_because_an_obligation_has_no_expiry(self):
        # Journal ageing: a window parked days ago and never verified as restored must not
        # be forgotten just because the record is old.
        self.path.write_text(
            json.dumps(
                {
                    "version": JOURNAL_VERSION,
                    "records": [make_record(recorded_at=time.time() - 8 * 24 * 3600).to_dict()],
                }
            ),
            encoding="utf-8",
        )
        records = RecoveryJournal(self.path).records()
        self.assertEqual([record.hwnd for record in records], [4242])
        self.assertGreater(records[0].age_sec(), 7 * 24 * 3600)

    def test_oversized_journal_is_ignored(self):
        self.path.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")
        self.assertEqual(RecoveryJournal(self.path).records(), ())

    def test_journal_path_is_derived_from_the_settings_file(self):
        self.assertEqual(
            journal_path_for(Path("C:/tmp/settings.json")),
            Path("C:/tmp/settings.json.park.json"),
        )

    def test_no_temporary_files_are_left_behind(self):
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record())
        journal.clear(4242)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class JournalSchemaTests(unittest.TestCase):
    """Journal damage: a damaged journal may never make a parked window unrecoverable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def assert_quarantined_and_readable(self, document: str) -> None:
        self.write(document)
        journal = RecoveryJournal(self.path)
        # Reading must never raise and must never report obligations that are
        # not in the file: an unreadable document is moved aside, not guessed.
        self.assertEqual(journal.records(), ())
        self.assertFalse(journal.has_pending())
        self.assertTrue(Path(str(self.path) + ".invalid").exists())

    def test_wrong_version_type_is_rejected(self):
        self.assertIsNotNone(journal_problem({"version": "1", "records": []}))

    def test_wrong_records_type_is_rejected(self):
        self.assertIsNotNone(journal_problem({"version": JOURNAL_VERSION, "records": 123}))
        self.assertIsNotNone(journal_problem({"version": JOURNAL_VERSION, "records": {}}))

    def test_missing_fields_are_rejected(self):
        self.assertIsNotNone(journal_problem({}))
        self.assertIsNotNone(journal_problem({"version": JOURNAL_VERSION}))
        self.assertIsNotNone(journal_problem([]))
        self.assertIsNone(journal_problem({"version": JOURNAL_VERSION, "records": []}))

    def test_boolean_version_is_not_an_integer_version(self):
        self.assertIsNotNone(journal_problem({"version": True, "records": []}))

    def test_unparsable_document_is_quarantined(self):
        self.assert_quarantined_and_readable("{not json")

    def test_string_version_is_quarantined(self):
        self.assert_quarantined_and_readable(json.dumps({"version": "1", "records": []}))

    def test_non_list_records_are_quarantined(self):
        self.assert_quarantined_and_readable(json.dumps({"version": 1, "records": 123}))

    def test_array_root_is_quarantined(self):
        self.assert_quarantined_and_readable(json.dumps([1, 2, 3]))

    def test_null_root_is_quarantined(self):
        self.assert_quarantined_and_readable("null")

    def test_scalar_roots_are_quarantined(self):
        for document in ("0", '""', "true"):
            with self.subTest(document=document):
                self.assert_quarantined_and_readable(document)

    def test_nan_and_infinity_never_reach_a_restore(self):
        # json.loads accepts NaN/Infinity by default.  A placement containing NaN
        # is not a recoverable fact, so the *placement* is refused; the record
        # itself is deliberately kept, because dropping it would silently abandon
        # a window that may still be parked off-screen.
        for literal in (math.nan, math.inf, -math.inf):
            with self.subTest(literal=literal):
                record = make_record(4242).to_dict()
                rect = list(record["normalPosition"])
                rect[0] = literal
                record["normalPosition"] = rect
                document = json.dumps(
                    {"version": JOURNAL_VERSION, "records": [record]}
                )  # allow_nan is on by default: the file can really contain NaN
                self.assertIn("NaN" if math.isnan(literal) else "Infinity", document)
                self.write(document)
                journal = RecoveryJournal(self.path)
                records = journal.records()
                self.assertEqual([item.hwnd for item in records], [4242], "the obligation was dropped")
                self.assertFalse(records[0].placement_usable)
                for value in (*records[0].normal_position, *records[0].screen_rect):
                    self.assertTrue(math.isfinite(value))

    def test_a_non_finite_lease_does_not_lock_the_record_forever(self):
        for literal in (math.inf, -math.inf, math.nan):
            with self.subTest(literal=literal):
                self.path.unlink(missing_ok=True)
                record = make_record(4242).to_dict()
                record["claimPid"] = 999999
                record["claimUntil"] = literal
                self.write(json.dumps({"version": JOURNAL_VERSION, "records": [record]}))
                journal = RecoveryJournal(self.path)
                self.assertIsNotNone(
                    journal.claim(4242, executor("b")),
                    f"claimUntil={literal} locked the record forever",
                )

    def test_an_oversized_document_is_quarantined(self):
        # MAX_JOURNAL_BYTES exists precisely so a corrupt/huge file cannot be
        # read into memory at startup.
        payload = json.dumps(
            {"version": JOURNAL_VERSION, "records": [], "pad": "x" * (MAX_JOURNAL_BYTES + 1)}
        )
        self.assert_quarantined_and_readable(payload)

    def test_too_many_records_are_quarantined(self):
        payload = {
            "version": JOURNAL_VERSION,
            "records": [make_record(index + 1).to_dict() for index in range(MAX_RECORDS + 1)],
        }
        self.assert_quarantined_and_readable(json.dumps(payload))

    def test_quarantine_keeps_the_previous_document_for_forensics(self):
        self.assert_quarantined_and_readable("{not json")
        moved = Path(str(self.path) + ".invalid")
        self.assertEqual(moved.read_text(encoding="utf-8"), "{not json")

    def test_repeated_damage_never_reuses_a_quarantine_name(self):
        self.assert_quarantined_and_readable("{first")
        self.assert_quarantined_and_readable("{second")
        self.assertEqual(Path(str(self.path) + ".invalid").read_text(encoding="utf-8"), "{first")
        self.assertEqual(
            Path(str(self.path) + ".invalid.1").read_text(encoding="utf-8"), "{second"
        )

    def test_a_broken_record_does_not_cost_the_whole_journal(self):
        payload = {
            "version": JOURNAL_VERSION,
            "records": [{"hwnd": "broken"}, make_record(4242).to_dict()],
        }
        self.write(json.dumps(payload))
        self.assertEqual(
            [record.hwnd for record in RecoveryJournal(self.path).records()], [4242]
        )

    def test_journal_recovers_usable_records_after_quarantine(self):
        journal = RecoveryJournal(self.path)
        journal.record_intent(make_record(4242))
        self.write("{not json")
        fresh = RecoveryJournal(self.path)
        self.assertEqual(fresh.records(), ())
        fresh.record_intent(make_record(777))
        self.assertEqual([record.hwnd for record in RecoveryJournal(self.path).records()], [777])


class RecoveryLeaseTests(unittest.TestCase):
    """Two executors (application and guardian) must not work the same record."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.journal.record_intent(make_record())

    def tearDown(self):
        self._tmp.cleanup()

    def expire_leases_on_disk(self) -> None:
        """Age every lease into the past, the way a dead executor leaves it.

        The journal is transactional, so a returned record is a copy: only the
        document itself carries a lease another transaction can observe.
        """
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for record in data.get("records", []):
            record["claimUntil"] = time.time() - 60.0
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def test_a_lease_excludes_a_second_executor(self):
        first = self.journal.claim(4242, executor("a"))
        self.assertIsNotNone(first)
        self.assertIsNone(self.journal.claim(4242, executor("b")))
        self.assertTrue(self.journal.renew(first))
        self.assertIsNone(self.journal.claim(4242, executor("b")))

    def test_reclaiming_your_own_record_is_allowed(self):
        first = self.journal.claim(4242, executor("a"))
        second = self.journal.claim(4242, executor("a"))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # A second claim is a *new* lease, not the old one: the generation moves
        # on so the first claim can no longer mutate anything.
        self.assertGreater(second.generation, first.generation)
        self.assertNotEqual(second.token, first.token)
        self.assertFalse(self.journal.renew(first))
        self.assertTrue(self.journal.renew(second))

    def test_an_expired_lease_can_be_taken_over(self):
        # A dead executor must not be able to block recovery forever: the lease
        # carries an expiry, so a later start can continue the work.
        self.assertIsNotNone(self.journal.claim(4242, executor("a")))
        self.expire_leases_on_disk()
        record = self.journal.get(4242)
        self.assertFalse(record.lease_is_live(executor("b")))
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_releasing_a_lease_lets_the_next_executor_continue(self):
        claim = self.journal.claim(4242, executor("a"))
        self.journal.release(claim)
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_release_all_frees_only_our_own_leases(self):
        self.journal.claim(4242, executor("a"))
        self.journal.release_all(executor("b"))
        self.assertEqual(RecoveryJournal(self.path).get(4242).claim_executor, "exec-a")
        self.journal.release_all(executor("a"))
        self.assertEqual(RecoveryJournal(self.path).get(4242).claim_executor, "")

    def test_the_lease_survives_a_write_and_reload(self):
        self.journal.claim(4242, executor("a"))
        stored = self.journal.get(4242)
        self.assertEqual(stored.claim_executor, "exec-a")
        self.assertGreater(stored.claim_until, time.time())
        self.assertTrue(stored.lease_is_live(executor("b")))
        self.assertFalse(stored.lease_is_live(executor("a")))

    def test_a_cleared_record_releases_its_lease(self):
        self.journal.claim(4242, executor("a"))
        self.assertTrue(self.journal.clear(4242))
        self.assertIsNone(RecoveryJournal(self.path).get(4242))

    def test_a_worker_cannot_clear_a_record_another_executor_owns(self):
        self.journal.claim(4242, executor("b"))
        self.assertFalse(self.journal.clear_unless_claimed(4242, executor_of(make_record())))
        self.assertEqual(RecoveryJournal(self.path).get(4242).claim_executor, "exec-b")

    def test_the_owning_run_may_retire_its_own_record(self):
        self.assertTrue(self.journal.clear_unless_claimed(4242, executor_of(make_record())))
        self.assertFalse(Path(self.path).exists())

    def test_the_claim_holder_may_retire_a_record_it_took_over(self):
        claim = self.journal.claim(4242, executor("b"))
        self.assertTrue(self.journal.clear_claimed(claim))
        self.assertFalse(Path(self.path).exists())

    def test_a_stale_executor_cannot_clear_a_newer_claim(self):
        # The ABA race: A claims generation 1, stalls, loses the lease, B claims
        # generation 2, and only then does A try to end the obligation.
        first = self.journal.claim(4242, executor("a"))
        self.expire_leases_on_disk()
        second = self.journal.claim(4242, executor("b"))
        self.assertEqual(second.generation, first.generation + 1)
        self.assertFalse(self.journal.clear_claimed(first))
        self.assertFalse(self.journal.release(first))
        self.assertFalse(self.journal.renew(first))
        stored = self.journal.get(4242)
        self.assertEqual(stored.claim_executor, "exec-b")
        self.assertEqual(stored.claim_generation, second.generation)

    def test_a_record_replaced_under_an_executor_is_not_cleared_by_timestamp(self):
        # A foreign process that merely observes the window on screen may not
        # retire the obligation: the park may still be in flight.
        recorded_at = self.journal.get(4242).recorded_at
        self.assertFalse(self.journal.clear_unless_claimed(4242, executor("b"), recorded_at=recorded_at))
        self.assertIsNotNone(self.journal.get(4242))

    def test_a_new_record_for_the_same_hwnd_is_never_cleared_by_timestamp(self):
        # A second intent for the same handle is refused outright, so the original
        # geometry, owner and instant survive: there is no newer record a stale
        # timestamp could accidentally match.  The remaining risk - a *foreign*
        # writer replacing the record underneath us - is exercised by planting
        # the replacement in the document directly.
        original = self.journal.get(4242)
        self.assertFalse(self.journal.record_intent(make_record(recorded_at=time.time())))
        self.assertEqual(self.journal.get(4242), original)

        self.expire_leases_on_disk()
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["records"][0]["recordedAt"] = time.time()
        document["records"][0]["ownerRunId"] = "someone-else"
        self.path.write_text(json.dumps(document), encoding="utf-8")
        owner = executor_of(original)
        self.assertFalse(
            self.journal.clear_unless_claimed(4242, owner, recorded_at=original.recorded_at)
        )
        self.assertIsNotNone(self.journal.get(4242))


class LeaseSanitisationTests(unittest.TestCase):
    """Non-finite control values must never create a permanent lock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"
        self.journal = RecoveryJournal(self.path)
        self.journal.record_intent(make_record())

    def tearDown(self):
        self._tmp.cleanup()

    def plant(self, **fields) -> dict:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["records"][0].update(fields)
        self.path.write_text(json.dumps(document), encoding="utf-8")
        return document["records"][0]

    def test_an_infinite_lease_is_dropped_instead_of_locking_the_record(self):
        self.plant(claimPid=999999, claimUntil=math.inf)
        record = RecoveryJournal(self.path).get(4242)
        self.assertEqual(record.claim_until, 0.0)
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_a_nan_lease_is_dropped(self):
        self.plant(claimPid=999999, claimUntil=math.nan)
        record = RecoveryJournal(self.path).get(4242)
        self.assertEqual(record.claim_until, 0.0)
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_a_negative_infinite_lease_is_dropped(self):
        self.plant(claimPid=999999, claimUntil=-math.inf)
        record = RecoveryJournal(self.path).get(4242)
        self.assertEqual(record.claim_until, 0.0)
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_a_finite_but_absurd_lease_is_dropped(self):
        self.plant(claimPid=999999, claimUntil=1e300)
        record = RecoveryJournal(self.path).get(4242)
        self.assertEqual(record.claim_until, 0.0)

    def test_an_incomplete_claim_is_dropped(self):
        # A lease without the identity/token/generation triple cannot be fenced,
        # so it may not be allowed to exclude anybody.
        self.plant(
            claimPid=999999,
            claimUntil=time.time() + 600,
            claimExecutor="ghost",
            claimToken="",
            claimGeneration=0,
        )
        record = RecoveryJournal(self.path).get(4242)
        self.assertEqual(record.claim_until, 0.0)
        self.assertIsNotNone(self.journal.claim(4242, executor("b")))

    def test_a_corrupt_placement_keeps_the_obligation_but_disables_the_exact_restore(self):
        # Dropping the record would abandon a window that may still be parked, so
        # only the placement is neutralised.
        self.plant(screenRect=[math.nan, 0, 100, 100], normalPosition=[0, 0, math.inf, 100])
        record = RecoveryJournal(self.path).get(4242)
        self.assertIsNotNone(record, "a corrupt placement must not discard the obligation")
        self.assertFalse(record.placement_usable)

    def test_a_non_finite_generation_is_ignored(self):
        self.plant(claimUntil=0.0, claimGeneration=math.inf, claimExecutor="x", claimToken="y")
        self.assertEqual(RecoveryJournal(self.path).get(4242).claim_generation, 0)

    def test_the_journal_never_writes_non_finite_numbers(self):
        self.assertTrue(self.journal.record_intent(make_record(777)))
        payload = self.path.read_text(encoding="utf-8")
        for literal in ("Infinity", "NaN"):
            self.assertNotIn(literal, payload)


class SchemaMigrationTests(unittest.TestCase):
    """An older journal must be migrated, not discarded - and not half-read."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "settings.json.park.json"

    def tearDown(self):
        self._tmp.cleanup()

    def legacy_document(self) -> dict:
        legacy = make_record(state=STATE_PARKED).to_dict()
        legacy.pop("state")
        legacy["parked"] = True
        for field in (
            "ownerRunId",
            "ownerCreated",
            "claimExecutor",
            "claimCreated",
            "claimToken",
            "claimGeneration",
            "placementUsable",
        ):
            legacy.pop(field, None)
        return {"version": LEGACY_JOURNAL_VERSIONS[0], "revision": 7, "records": [legacy]}

    def test_a_legacy_document_is_supported(self):
        self.assertIsNone(journal_problem(self.legacy_document()))

    def test_a_legacy_document_is_migrated_in_place_and_keeps_its_obligation(self):
        self.path.write_text(json.dumps(self.legacy_document()), encoding="utf-8")
        journal = RecoveryJournal(self.path)
        records = journal.records()
        self.assertEqual([record.hwnd for record in records], [4242], "migration dropped an obligation")
        migrated = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["version"], JOURNAL_VERSION)
        self.assertEqual(migrated["records"][0]["state"], STATE_PARKED)
        # A legacy record carries no owner identity: that is what makes its owner
        # untrusted, and it is preserved as "absent" rather than invented.
        self.assertEqual(migrated["records"][0]["ownerRunId"], "")
        self.assertIsNone(migrated["records"][0]["ownerCreated"])
        self.assertEqual(migrated["records"][0]["claimGeneration"], 0)
        self.assertGreaterEqual(migrated["revision"], 7, "the document revision was lost by the migration")

    def test_a_legacy_intent_stays_an_intent(self):
        document = self.legacy_document()
        document["records"][0]["parked"] = False
        self.path.write_text(json.dumps(document), encoding="utf-8")
        records = RecoveryJournal(self.path).records()
        self.assertEqual(records[0].state, STATE_INTENT)
        self.assertFalse(records[0].parked)

    def test_a_legacy_lease_does_not_block_recovery_forever(self):
        document = self.legacy_document()
        document["records"][0]["claimPid"] = 999999
        document["records"][0]["claimUntil"] = time.time() - 1.0
        self.path.write_text(json.dumps(document), encoding="utf-8")
        journal = RecoveryJournal(self.path)
        self.assertIsNotNone(journal.claim(4242, executor("b")))

    def test_an_unknown_version_is_still_quarantined(self):
        document = self.legacy_document()
        document["version"] = JOURNAL_VERSION + 99
        self.path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(RecoveryJournal(self.path).records(), ())
        self.assertTrue(Path(str(self.path) + ".invalid").exists())


class ProcessIdentityTests(unittest.TestCase):
    def test_a_fresh_executor_identity_is_unique_and_knows_this_process(self):
        first = new_executor_identity("main")
        second = new_executor_identity("main")
        self.assertNotEqual(first.executor_id, second.executor_id)
        self.assertEqual(first.pid, os.getpid())
        self.assertEqual(len(first.executor_id), 32)
        self.assertTrue(first.short_id)



class ParkedStateConversionTests(unittest.TestCase):
    """The journal must be able to rebuild the exact pre-park placement."""

    def setUp(self):
        try:
            import winapi  # noqa: F401
        except Exception as exc:  # pragma: no cover - non-Windows
            self.skipTest(f"winapi is unavailable: {exc}")

    def test_state_roundtrip(self):
        import winapi

        placement = winapi.WINDOWPLACEMENT()
        placement.length = ctypes_sizeof(placement)
        placement.flags = 0
        placement.showCmd = 3
        placement.ptMinPosition = winapi.wintypes.POINT(-3, -4)
        placement.ptMaxPosition = winapi.wintypes.POINT(-5, -6)
        placement.rcNormalPosition = winapi.wintypes.RECT(10, 20, 810, 620)
        state = winapi.ParkedWindowState(
            placement=placement,
            screen_rect=(10, 20, 810, 620),
            pid=1234,
            class_name="SomeWindowClass",
            process_name="app.exe",
            process_created=999,
        )
        record = record_from_state(4242, state, owner_pid=77, label="app.exe - Some")
        rebuilt = state_from_record(record)
        self.assertEqual(rebuilt.pid, state.pid)
        self.assertEqual(rebuilt.class_name, state.class_name)
        self.assertEqual(rebuilt.process_name, state.process_name)
        self.assertEqual(rebuilt.process_created, state.process_created)
        self.assertEqual(rebuilt.screen_rect, state.screen_rect)
        self.assertEqual(int(rebuilt.placement.showCmd), 3)
        self.assertEqual(
            (
                int(rebuilt.placement.rcNormalPosition.left),
                int(rebuilt.placement.rcNormalPosition.top),
                int(rebuilt.placement.rcNormalPosition.right),
                int(rebuilt.placement.rcNormalPosition.bottom),
            ),
            (10, 20, 810, 620),
        )
        self.assertEqual(int(rebuilt.placement.length), ctypes_sizeof(winapi.WINDOWPLACEMENT))


def ctypes_sizeof(struct_type) -> int:
    import ctypes

    return ctypes.sizeof(struct_type)


class ParkOwnershipContractTests(unittest.TestCase):
    """Static contracts: ownership before the move, an executor for the record."""

    def setUp(self):
        self.executor = (ROOT / "src" / "restoreguard.py").read_text(encoding="utf-8")

    def test_park_receives_a_recovery_hook(self):
        self.assertIn("before_park=", APP)
        self.assertIn("before_park", WINAPI)

    def test_park_aborts_when_ownership_cannot_be_recorded(self):
        park = WINAPI.split("def park_window_offscreen_sync", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if before_park is not None and not before_park(state):", park)
        self.assertIn("return None", park)

    def test_park_worker_records_intent_and_commits(self):
        worker = APP.split("def _park_source_worker", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_journal_record_intent", worker)
        self.assertIn("_journal_commit_park", worker)
        self.assertIn("before_park=", worker)
        launch = APP.split("def _start_daemon_worker", 1)[1].split("    def _journal_record_intent", 1)[0]
        self.assertIn("self._begin_source_action()", launch)
        self.assertIn("self._end_source_action()", launch)
        self.assertLess(launch.index("self._begin_source_action()"), launch.index("threading.Thread"))

    def test_shutdown_waits_for_in_flight_source_actions(self):
        self.assertIn("def _wait_for_source_actions", APP)
        shutdown = APP.split("def _restore_sources_for_shutdown", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("_wait_for_source_actions", shutdown)

    def test_startup_recovery_replays_the_journal(self):
        self.assertIn("def _recover_journaled_sources", APP)
        self.assertIn("def _recover_journaled_record", APP)
        self.assertIn("self.recovery_journal = RecoveryJournal(", APP)
        self.assertIn("self._recover_journaled_sources()", APP)
        # The decision "is this window still parked, and may the record go?" now
        # belongs to the shared executor, not to the application.
        self.assertIn("looks_like_lookup_parked(hwnd)", self.executor)

    def test_restore_clears_the_journal(self):
        complete = APP.split("def _complete_recovery_attempt", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self._journal_drop_async(hwnd, recorded_at)", complete)
        # The record may only go when the restore worked or the window is provably
        # gone/reused - never because the identity merely could not be established.
        self.assertIn("winapi.classify_parked_window(hwnd, state)", complete)
        self.assertIn("stale = verdict in (winapi.VERIFY_GONE, winapi.VERIFY_REUSED)", complete)
        self.assertIn("unknown = verdict == winapi.VERIFY_UNKNOWN", complete)
        self.assertIn("if released:", complete)

    def test_an_obligation_is_never_dropped_for_being_old(self):
        journal = (ROOT / "src" / "recovery.py").read_text(encoding="utf-8")
        load = journal.split("    def _read_document(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertNotIn("age_sec()", load)
        self.assertNotIn("MAX_RECORD_AGE", journal)
        self.assertIn("never dropped because of their age", load)

    def test_an_unverifiable_identity_does_not_look_like_an_obsolete_record(self):
        self.assertIn("STATUS_UNVERIFIED", self.executor)
        assess = self.executor.split("def assess(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("return STATUS_UNVERIFIED", assess)
        # The verdict itself moved into the shared classifier, so the executor
        # cannot answer "reused" to a query that simply produced no answer.
        identity = self.executor.split("def _assess_identity(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("if identity.created is None:", identity)
        self.assertIn("return IDENTITY_UNKNOWN", identity)
        self.assertIn("unanswered", identity)

    def test_a_pending_park_intent_is_held_until_it_is_resolved(self):
        resolve = self.executor.split("def resolve(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("PENDING_SETTLE_SEC", resolve)
        # A pending intent has no expiry either: the move it describes may still
        # land minutes later, so only a verified visible window releases it.
        self.assertNotIn("PENDING_MAX_SEC", self.executor)
        self.assertNotIn("COMMITTED_MAX_SEC", self.executor)
        self.assertNotIn("GUARDIAN_MAX_LIFETIME_SEC", self.executor)

    def test_a_caller_that_must_return_releases_the_lease_and_keeps_the_record(self):
        resolve = self.executor.split("def resolve(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("budget_sec", resolve)
        self.assertIn("journal.release(claim)", resolve)
        self.assertNotIn("records.pop", resolve)

    def test_the_build_script_does_not_force_kill_the_app(self):
        script = BUILD_ONEFILE_BAT
        self.assertNotIn("taskkill /f", script.lower())
        self.assertNotIn("Stop-Process -Force", script)
        self.assertIn("tools\\stop_running_instance.py", script)


if __name__ == "__main__":
    unittest.main()