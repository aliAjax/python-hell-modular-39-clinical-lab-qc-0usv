from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import RuleEngine


# Investigation/retest conclusions that must survive a QC result correction so
# the laboratory does not re-investigate the same failure under the new version.
CARRY_OVER_FIELDS = (
    "reason",
    "resolution",
    "investigation_reason",
    "investigated_by",
    "resolved_by",
    "corrective_action",
    "retest_reason",
    "replacement_run_id",
    "conclusion",
    "note",
)


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
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

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if idempotency_key:
            cached = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if cached:
                cached_entity = self.repository.get_entity(cached)
                if cached_entity:
                    return cached_entity
        if entity["kind"] == "qc_run" and action == "correct":
            return self._correct_qc_run(actor, entity, data or {}, expected, idempotency_key)
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
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
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return updated

    def _correct_qc_run(self, actor, entity, data, expected_version, idempotency_key):
        """Correct a QC result: void the old version and create a new one.

        The new version keeps the same assay, lot, instrument and measurement
        time, carries over investigation conclusions, and starts pending so it
        is re-evaluated against the corrected value. Result batches referencing
        the old run are repointed to the new version; batches already released
        under the old version are rolled back to waiting with an audit trail.
        """
        next_status, patch = self.rules.validate_transition(
            actor, entity, "correct", dict(data), self._lookup
        )
        new_run_id = str(uuid4())
        carry = {
            field: entity["data"][field]
            for field in CARRY_OVER_FIELDS
            if field in entity["data"]
        }
        new_data = {
            "assay_id": entity["data"]["assay_id"],
            "qc_lot_id": entity["data"]["qc_lot_id"],
            "instrument_id": entity["data"]["instrument_id"],
            "run_at": entity["data"].get("run_at"),
            "value": patch.get("value", data.get("value")),
            "supersedes_run_id": entity["id"],
            "correction_reason": data.get("reason"),
            "corrected_by": actor.user_id,
            **carry,
            "correction_history": patch.get("correction_history") or [],
        }
        result = self.repository.correct_qc_run(
            entity["id"],
            new_run_id,
            new_data,
            expected_version,
            actor,
            data.get("reason"),
            idempotency_key,
        )
        return result["new_run"]

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
