from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine

# Findings recorded during an out-of-control investigation stay attached to
# the measurement when it is corrected; correcting a mistyped value never
# erases the investigation trail.
INVESTIGATION_FIELDS = (
    "reject_reason",
    "rejection_rule",
    "flags",
    "z_score",
    "rule_snapshot",
    "investigation",
    "investigation_reason",
    "investigator_id",
    "findings",
    "resolution",
    "resolved_by",
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, tx):
        def lookup(kind, field, value):
            return tx.find_entities(self.rules.normalize_kind(kind), field, value)

        return lookup

    def _enrich(self, entity, tx):
        if entity and entity["kind"] == "instrument":
            entity = dict(entity)
            entity["revision"] = tx.get_revision(entity["id"])
        return entity

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self.repository.find_entities)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None,
                   expected_revision=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        payload = dict(data or {})
        if kind == "qc_run" and action == "correct":
            return self.correct_qc_run(
                actor, entity_id, payload, expected_revision, idempotency_key
            )
        if kind == "result_batch" and action == "release":
            return self.release_batch(actor, entity_id, payload, expected_version, expected_revision)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self.repository.find_entities
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # -- quality control correction with versioning ------------------------

    def correct_qc_run(self, actor, run_id, data, expected_revision=None, idempotency_key=None):
        data = dict(data or {})
        # A retry after a failed/partial request must not mint a second
        # corrected version; the idempotency key resolves to the one created
        # by the first accepted submission.
        if idempotency_key:
            existing_id = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing_id:
                existing = self.repository.get_entity(existing_id)
                if existing:
                    return existing

        with self.repository.transaction() as tx:
            lookup = self._lookup(tx)
            old_run = tx.get_entity(run_id)
            if not old_run:
                raise NotFoundError("entity not found: " + run_id)
            instrument_id = old_run["data"].get("instrument_id")

            # Same-instrument arbitration comes before rule validation:
            # the loser of a correction/release race must receive a clear
            # revision conflict rather than a downstream business error.
            new_revision = tx.advance_revision(instrument_id, expected_revision)

            # Role/status/input validation against the locked snapshot.
            self.rules.check_transition(actor, old_run, "correct", data, lookup)

            new_run_id = str(uuid4())
            new_data = {
                key: old_run["data"].get(key)
                for key in ("assay_id", "qc_lot_id", "instrument_id", "run_at", "run_at_actual")
                if key in old_run["data"]
            }
            # Carry investigation conclusions forward so a retry does not lose
            # what the lab already concluded about the out-of-control run.
            carried = {
                key: old_run["data"][key]
                for key in INVESTIGATION_FIELDS
                if key in old_run["data"]
            }
            new_data.update(carried)
            new_data["value"] = float(data["value"])
            new_data["correction_of"] = old_run["id"]
            new_data["correction_reason"] = data["reason"]
            new_data["correction_revision"] = new_revision
            new_run = tx.create_entity(
                new_run_id,
                "qc_run",
                self.rules.initial_status("qc_run"),
                new_data,
                actor.user_id,
            )

            old_data = dict(old_run["data"])
            old_data["superseded_by"] = new_run_id
            old_data["correction_reason"] = data["reason"]
            old_data["correction_revision"] = new_revision
            tx.update_entity(old_run["id"], old_run["version"], "superseded", old_data)

            recalled, repointed = self._repoint_batches(tx, old_run, new_run_id, actor, new_revision)

            tx.append_audit(
                old_run["id"],
                actor.user_id,
                actor.role,
                "correct",
                old_run["status"],
                "superseded",
                {
                    "reason": data["reason"],
                    "new_run_id": new_run_id,
                    "old_value": old_run["data"].get("value"),
                    "new_value": float(data["value"]),
                    "instrument_revision": new_revision,
                    "carried_investigation": sorted(carried),
                    "recalled_batches": recalled,
                    "repointed_batches": repointed,
                },
            )
            tx.append_audit(
                new_run_id,
                actor.user_id,
                actor.role,
                "create",
                None,
                new_run["status"],
                {
                    "kind": "qc_run",
                    "correction_of": old_run["id"],
                    "reason": data["reason"],
                    "instrument_revision": new_revision,
                    "carried_investigation": sorted(carried),
                },
            )
            if idempotency_key:
                tx.save_idempotency(actor.user_id, idempotency_key, new_run_id)
            return new_run

    def _repoint_batches(self, tx, old_run, new_run_id, actor, new_revision):
        """Already-released batches are recalled; the rest point at the new version."""
        lookup = self._lookup(tx)
        batches = lookup("result_batch", "qc_run_id", old_run["id"])
        recalled = []
        repointed = []
        for batch in batches:
            batch_data = dict(batch["data"])
            trail = list(batch_data.get("disposition_trail") or [])
            if batch["status"] == "released":
                trail.append(
                    {
                        "action": "auto_recall",
                        "reason": "qc run %s corrected (revision %s)"
                        % (old_run["id"], new_revision),
                        "from_status": batch["status"],
                        "to_status": "waiting",
                        "replacement_qc_run_id": new_run_id,
                        "released_by": batch_data.get("released_by"),
                        "released_at": batch_data.get("released_at", batch["updated_at"]),
                        "actor_id": actor.user_id,
                    }
                )
                batch_data["disposition_trail"] = trail
                batch_data["qc_run_id"] = new_run_id
                batch_data.pop("released_by", None)
                tx.update_entity(batch["id"], batch["version"], "waiting", batch_data)
                tx.append_audit(
                    batch["id"],
                    actor.user_id,
                    actor.role,
                    "auto_recall",
                    "released",
                    "waiting",
                    {
                        "superseded_qc_run_id": old_run["id"],
                        "replacement_qc_run_id": new_run_id,
                        "instrument_revision": new_revision,
                    },
                )
                recalled.append(batch["id"])
            else:
                trail.append(
                    {
                        "action": "repoint",
                        "reason": "qc run %s corrected before release (revision %s)"
                        % (old_run["id"], new_revision),
                        "replacement_qc_run_id": new_run_id,
                        "actor_id": actor.user_id,
                    }
                )
                batch_data["disposition_trail"] = trail
                batch_data["qc_run_id"] = new_run_id
                tx.update_entity(batch["id"], batch["version"], batch["status"], batch_data)
                tx.append_audit(
                    batch["id"],
                    actor.user_id,
                    actor.role,
                    "repoint",
                    batch["status"],
                    batch["status"],
                    {
                        "superseded_qc_run_id": old_run["id"],
                        "replacement_qc_run_id": new_run_id,
                        "instrument_revision": new_revision,
                    },
                )
                repointed.append(batch["id"])
        return recalled, repointed

    # -- result batch release with same-instrument arbitration -------------

    def release_batch(self, actor, batch_id, data, expected_version=None, expected_revision=None):
        batch = self.repository.get_entity(batch_id)
        if not batch:
            raise NotFoundError("entity not found: " + batch_id)
        expected = int(expected_version) if expected_version is not None else batch["version"]
        payload = dict(data or {})
        with self.repository.transaction() as tx:
            lookup = self._lookup(tx)
            locked_batch = tx.get_entity(batch_id)
            # Revision arbitration first: if a correction won the race, the
            # batch may already have been recalled/repointed, and the releasing
            # client must get a conflict instead of a stale rule error.
            instrument_id = locked_batch["data"].get("instrument_id")
            new_revision = tx.advance_revision(instrument_id, expected_revision)
            # Role/status/business validation against the locked snapshot.
            self.rules.check_transition(actor, locked_batch, "release", payload, lookup)

            patch = dict(payload)
            patch["released_by"] = actor.user_id
            patch["released_revision"] = new_revision
            merged = dict(locked_batch["data"])
            merged.update(patch)
            updated = tx.update_entity(batch_id, expected, "released", merged)
            tx.append_audit(
                batch_id,
                actor.user_id,
                actor.role,
                "release",
                locked_batch["status"],
                "released",
                {"patch": patch, "instrument_revision": new_revision},
            )
            return updated

    def get(self, entity_id):
        with self.repository.transaction() as tx:
            entity = tx.get_entity(entity_id)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            return self._enrich(entity, tx)

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        with self.repository.transaction() as tx:
            return [self._enrich(entity, tx) for entity in tx.list_entities(kind=kind, status=status)]

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
