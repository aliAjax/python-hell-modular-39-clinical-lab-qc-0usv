import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
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

    def _base(self):
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
        lot = self.service.transition(self.supervisor, lot["id"], "activate", {"activated_by": "qc-1"})
        instrument = self.service.create(
            self.supervisor,
            "instrument",
            {"name": "Analyzer A", "serial": "A-100", "calibration_due": "2099-01-01"},
        )
        return assay, lot, instrument

    def _accepted_run(self, value=5.02):
        assay, lot, instrument = self._base()
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": "2026-09-27T08:00:00Z",
            },
        )
        run = self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})
        return assay, lot, instrument, run

    def _batch(self, assay, instrument, run, status=None):
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 12,
            },
        )
        if status == "released":
            batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        return batch

    def test_correction_versions_old_and_new(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        old_run = self.service.get(run["id"])
        self.assertEqual(old_run["status"], "voided")
        self.assertEqual(old_run["data"]["superseded_by_run_id"], new_run["id"])
        self.assertEqual(new_run["status"], "pending")
        self.assertEqual(new_run["data"]["value"], 5.03)
        self.assertEqual(new_run["data"]["supersedes_run_id"], run["id"])
        self.assertEqual(new_run["data"]["assay_id"], assay["id"])
        self.assertEqual(new_run["data"]["qc_lot_id"], lot["id"])
        self.assertEqual(new_run["data"]["instrument_id"], instrument["id"])
        self.assertTrue(new_run["data"]["correction_history"])
        # The new version must be re-evaluated before it can be used.
        re_evaluated = self.service.transition(
            self.supervisor, new_run["id"], "evaluate", {"evaluated_by": "qc-1"}
        )
        self.assertEqual(re_evaluated["status"], "accepted")

    def test_released_batch_rolled_back_and_repointed(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        batch = self._batch(assay, instrument, run, status="released")
        self.assertEqual(batch["status"], "released")
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        rolled = self.service.get(batch["id"])
        self.assertEqual(rolled["status"], "waiting")
        self.assertEqual(rolled["data"]["qc_run_id"], new_run["id"])
        rollbacks = rolled["data"]["release_rollbacks"]
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual(rollbacks[0]["from_status"], "released")
        self.assertEqual(rollbacks[0]["to_status"], "waiting")
        self.assertEqual(rollbacks[0]["new_run_id"], new_run["id"])
        # Audit trail records the whereabouts of the released batch.
        entries = self.service.audit_log(batch["id"])
        actions = [(entry["action"], entry["from_status"], entry["to_status"]) for entry in entries]
        self.assertIn(("correct", "released", "waiting"), actions)

    def test_unreleased_batch_repoints_to_new_version(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        batch = self._batch(assay, instrument, run, status="waiting")
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        repointed = self.service.get(batch["id"])
        self.assertEqual(repointed["status"], "waiting")
        self.assertEqual(repointed["data"]["qc_run_id"], new_run["id"])
        self.assertNotIn("release_rollbacks", repointed["data"])

    def test_investigation_conclusion_retained(self):
        assay, lot, instrument, run = self._accepted_run(9.9)
        run = self.service.transition(self.supervisor, run["id"], "investigate", {"reason": "out of range"})
        run = self.service.transition(
            self.supervisor, run["id"], "resolve", {"resolution": "qc material degraded, no patient impact"}
        )
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        self.assertEqual(new_run["data"]["resolution"], "qc material degraded, no patient impact")

    def test_idempotent_correction_retry_does_not_duplicate(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        first = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
            idempotency_key="correction-1",
        )
        second = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
            idempotency_key="correction-1",
        )
        self.assertEqual(first["id"], second["id"])
        runs = self.service.list("qc_run")
        self.assertEqual(len(runs), 2)  # old + new, no duplicates

    def test_stale_version_correction_conflicts(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, run["id"], "correct",
                {"reason": "transcription error", "value": 5.03},
                expected_version=999,
            )

    def test_release_after_correction_conflicts(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        batch = self._batch(assay, instrument, run, status="released")
        new_run = self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        # The rolled-back batch now points at the new (still pending) version,
        # so releasing it again must conflict until the new version is accepted.
        with self.assertRaises(ConflictError):
            self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        # Once the new version is accepted, release can proceed.
        self.service.transition(self.supervisor, new_run["id"], "evaluate", {"evaluated_by": "qc-1"})
        released = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(released["status"], "released")

    def test_correction_requires_value_and_reason(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        with self.assertRaises(ValidationError):
            self.service.transition(self.supervisor, run["id"], "correct", {"reason": "x"})
        with self.assertRaises(ValidationError):
            self.service.transition(self.supervisor, run["id"], "correct", {"value": 5.03})

    def test_cannot_correct_voided_run(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        self.service.transition(
            self.supervisor, run["id"], "correct",
            {"reason": "transcription error", "value": 5.03},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.supervisor, run["id"], "correct",
                {"reason": "again", "value": 5.04},
            )

    def test_correction_requires_supervisor_role(self):
        assay, lot, instrument, run = self._accepted_run(5.02)
        with self.assertRaises(Exception):
            self.service.transition(
                Actor("viewer", "viewer"), run["id"], "correct",
                {"reason": "transcription error", "value": 5.03},
            )


if __name__ == "__main__":
    unittest.main()
