import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "correction.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("qc-supervisor", "supervisor")

    def tearDown(self):
        self.tmp.cleanup()

    def _world(self, qc_value=5.02):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "a"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": qc_value,
                "run_at": "2026-09-27T08:00:00Z",
            },
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "a"})
        return assay, lot, instrument, run

    def _batch(self, assay, instrument, run, suffix="05", count=3):
        return self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:%s:00Z" % suffix,
                "patient_count": count,
            },
        )

    # -- version retention -------------------------------------------------

    def test_correction_creates_new_version_and_voids_old(self):
        _, _, instrument, run = self._world()
        new_run = self.service.transition(
            self.supervisor,
            run["id"],
            "correct",
            {"reason": "manual entry typo", "value": 4.99},
            expected_revision=0,
        )
        self.assertNotEqual(new_run["id"], run["id"])
        self.assertEqual(new_run["status"], "pending")
        self.assertEqual(new_run["data"]["value"], 4.99)
        self.assertEqual(new_run["data"]["correction_of"], run["id"])

        old_run = self.service.get(run["id"])
        self.assertEqual(old_run["status"], "superseded")
        self.assertEqual(old_run["data"]["superseded_by"], new_run["id"])
        self.assertEqual(old_run["data"]["correction_reason"], "manual entry typo")

        # Both versions remain queryable; the old one still carries its value.
        self.assertEqual(old_run["data"]["value"], 5.02)
        self.assertEqual(self.service.repository.get_revision(instrument["id"]), 1)

    def test_new_version_must_be_reevaluated_and_is_not_accepted_by_default(self):
        _, _, _, run = self._world()
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
            expected_revision=0,
        )
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": new_run["data"]["assay_id"],
                "instrument_id": new_run["data"]["instrument_id"],
                "qc_run_id": new_run["id"],
                "run_at": "2026-09-27T09:00:00Z",
                "patient_count": 1,
            },
        )
        # New version is pending until re-evaluated, so release is blocked.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "r"},
                expected_revision=1,
            )

    def test_batches_cannot_reference_a_superseded_version_directly(self):
        assay, _, instrument, run = self._world()
        self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
            expected_revision=0,
        )
        with self.assertRaises(ConflictError):
            self._batch(assay, instrument, run)

    # -- batch disposition -------------------------------------------------

    def test_released_batch_is_recalled_to_waiting_with_trail(self):
        assay, _, instrument, run = self._world()
        batch = self._batch(assay, instrument, run)
        batch = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "r"},
            expected_revision=0,
        )
        self.assertEqual(batch["status"], "released")

        new_run = self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
            expected_revision=1,
        )

        recalled = self.service.get(batch["id"])
        self.assertEqual(recalled["status"], "waiting")
        self.assertEqual(recalled["data"]["qc_run_id"], new_run["id"])
        trail = recalled["data"]["disposition_trail"]
        self.assertEqual(len(trail), 1)
        entry = trail[0]
        self.assertEqual(entry["action"], "auto_recall")
        self.assertEqual(entry["from_status"], "released")
        self.assertEqual(entry["to_status"], "waiting")
        self.assertEqual(entry["replacement_qc_run_id"], new_run["id"])
        self.assertEqual(entry["released_by"], "qc-supervisor")
        self.assertNotIn("released_by", recalled["data"])

        actions = [item["action"] for item in self.service.audit_log(batch["id"])]
        self.assertIn("auto_recall", actions)

        # After re-evaluation the recalled batch can be released again.
        new_run = self.service.transition(
            self.supervisor, new_run["id"], "evaluate", {"evaluated_by": "a"}
        )
        self.assertEqual(new_run["status"], "accepted")
        re_released = self.service.transition(
            self.supervisor, batch["id"], "release", {"reviewer_id": "r"},
            expected_revision=2,
        )
        self.assertEqual(re_released["status"], "released")

    def test_unreleased_batch_is_repointed_without_status_change(self):
        assay, _, instrument, run = self._world()
        waiting_batch = self._batch(assay, instrument, run, suffix="05")
        intercepted = self._batch(assay, instrument, run, suffix="06")
        intercepted = self.service.transition(
            self.supervisor, intercepted["id"], "intercept", {"reason": "hold"}
        )

        new_run = self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
            expected_revision=0,
        )

        waiting_batch = self.service.get(waiting_batch["id"])
        intercepted = self.service.get(intercepted["id"])
        self.assertEqual(waiting_batch["status"], "waiting")
        self.assertEqual(intercepted["status"], "intercepted")
        self.assertEqual(waiting_batch["data"]["qc_run_id"], new_run["id"])
        self.assertEqual(intercepted["data"]["qc_run_id"], new_run["id"])
        self.assertEqual(waiting_batch["data"]["disposition_trail"][0]["action"], "repoint")
        self.assertEqual(intercepted["data"]["disposition_trail"][0]["action"], "repoint")

    # -- concurrency arbitration -------------------------------------------

    def test_stale_revision_is_rejected_for_release_and_correction(self):
        assay, _, instrument, run = self._world()
        batch = self._batch(assay, instrument, run)
        # Another correction wins the instrument revision first.
        self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
            expected_revision=0,
        )
        with self.assertRaises(ConflictError) as context:
            self.service.transition(
                self.supervisor, batch["id"], "release", {"reviewer_id": "r"},
                expected_revision=0,
            )
        self.assertIn("revision conflict", str(context.exception))
        self.assertEqual(self.service.get(batch["id"])["status"], "waiting")

        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, run["id"], "correct", {"reason": "again", "value": 5.01},
                expected_revision=0,
            )

    def test_correction_and_release_race_has_single_winner(self):
        assay, _, instrument, run = self._world()
        batch = self._batch(assay, instrument, run)
        # Both submissions read revision 0 and race to commit on one instrument.
        barrier = threading.Barrier(2)
        outcomes = {}

        def release():
            barrier.wait()
            try:
                self.service.transition(
                    self.supervisor, batch["id"], "release", {"reviewer_id": "r"},
                    expected_revision=0,
                )
                outcomes["release"] = "ok"
            except ConflictError as exc:
                outcomes["release"] = str(exc)

        def correct():
            barrier.wait()
            try:
                self.service.transition(
                    self.supervisor, run["id"], "correct", {"reason": "typo", "value": 4.99},
                    expected_revision=0,
                )
                outcomes["correct"] = "ok"
            except ConflictError as exc:
                outcomes["correct"] = str(exc)

        threads = [threading.Thread(target=release), threading.Thread(target=correct)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(set(outcomes), {"release", "correct"})
        winners = [name for name, result in outcomes.items() if result == "ok"]
        self.assertEqual(len(winners), 1, outcomes)
        loser = next(name for name, result in outcomes.items() if result != "ok")
        self.assertIn("revision conflict", outcomes[loser])
        self.assertEqual(self.service.repository.get_revision(instrument["id"]), 1)

        final_batch = self.service.get(batch["id"])
        if winners == ["correct"]:
            self.assertEqual(final_batch["status"], "waiting")
            self.assertNotEqual(final_batch["data"]["qc_run_id"], run["id"])
        else:
            self.assertEqual(final_batch["status"], "released")

    # -- idempotency and investigation retention ---------------------------

    def test_correction_retry_is_idempotent(self):
        _, _, instrument, run = self._world()
        kwargs = {
            "actor": self.supervisor,
            "entity_id": run["id"],
            "action": "correct",
            "data": {"reason": "typo", "value": 4.99},
            "expected_revision": 0,
            "idempotency_key": "correct-retry-1",
        }
        first = self.service.transition(**kwargs)
        second = self.service.transition(**kwargs)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["data"]["value"], 4.99)

        runs = [item for item in self.service.list("qc_run")]
        self.assertEqual(len(runs), 2)  # original + one corrected version
        self.assertEqual(self.service.repository.get_revision(instrument["id"]), 1)
        old_audits = self.service.audit_log(run["id"])
        self.assertEqual([item["action"] for item in old_audits].count("correct"), 1)

    def test_retry_after_failed_correction_keeps_single_record(self):
        _, _, instrument, run = self._world()
        # A conflicting commit lands first, so this submission fails at
        # revision arbitration — before any record or idempotency key exists.
        self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "other fix", "value": 4.98},
            expected_revision=0,
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor,
                run["id"],
                "correct",
                {"reason": "typo", "value": 4.99},
                expected_revision=0,
                idempotency_key="retry-key",
            )
        self.assertIsNone(
            self.service.repository.get_idempotency("qc-supervisor", "retry-key")
        )

        # The lab evaluates the current version first (as required), then
        # retries the correction against it with the same key; a repeat
        # submission after success returns the same record.
        current = next(item for item in self.service.list("qc_run") if item["status"] == "pending")
        current = self.service.transition(
            self.supervisor, current["id"], "evaluate", {"evaluated_by": "a"}
        )
        self.assertEqual(current["status"], "accepted")
        retry = dict(
            actor=self.supervisor,
            entity_id=current["id"],
            action="correct",
            data={"reason": "typo", "value": 4.99},
            expected_revision=1,
            idempotency_key="retry-key",
        )
        committed = self.service.transition(**retry)
        again = self.service.transition(**retry)
        self.assertEqual(committed["id"], again["id"])
        self.assertEqual(len(self.service.list("qc_run")), 3)  # original, first fix, retry fix
        self.assertEqual(self.service.repository.get_revision(instrument["id"]), 2)

    def test_investigation_conclusions_carry_to_new_version(self):
        assay, lot, instrument, run = self._world(qc_value=5.5)
        self.assertEqual(run["status"], "rejected")
        investigated = self.service.transition(
            self.supervisor,
            run["id"],
            "investigate",
            {"reason": "possible reagent issue", "investigator_id": "inv-1",
             "findings": "reagent bottle swapped"},
        )
        resolved = self.service.transition(
            self.supervisor,
            investigated["id"],
            "resolve",
            {"resolution": "reagent replaced, value stands", "resolved_by": "inv-1"},
        )

        new_run = self.service.transition(
            self.supervisor,
            resolved["id"],
            "correct",
            {"reason": "value mistyped", "value": 5.05},
            expected_revision=0,
        )
        self.assertEqual(new_run["data"]["findings"], "reagent bottle swapped")
        self.assertEqual(new_run["data"]["resolution"], "reagent replaced, value stands")
        self.assertEqual(new_run["data"]["resolved_by"], "inv-1")
        self.assertEqual(new_run["data"]["reject_reason"], "quality control rule violation")
        self.assertIn("1_3s", new_run["data"]["flags"])

    def test_superseded_runs_are_excluded_from_evaluation_history(self):
        _, _, _, run = self._world(qc_value=5.5)
        self.service.transition(
            self.supervisor, run["id"], "correct", {"reason": "typo", "value": 5.5},
            expected_revision=0,
        )
        new_run_id = self.service.list("qc_run", status="pending")[0]["id"]
        # A history of bias flags from the voided value must not count here.
        evaluated = self.service.transition(
            self.supervisor, new_run_id, "evaluate", {"evaluated_by": "a"}
        )
        self.assertEqual(evaluated["status"], "rejected")  # 5.5 is still 1_3s
        self.assertEqual(evaluated["data"]["flags"], ["1_3s"])


if __name__ == "__main__":
    unittest.main()
