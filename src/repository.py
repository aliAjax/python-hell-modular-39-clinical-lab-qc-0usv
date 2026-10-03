import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def correct_qc_run(self, old_run_id, new_run_id, new_data, expected_version, actor, reason, idempotency_key):
        """Atomically supersede a QC run with a corrected version.

        The old run is voided and linked to the new version. Every result batch
        that referenced the old run is repointed to the new version; batches that
        had already been released under the old version are rolled back to
        ``waiting`` (with a ``release_rollbacks`` trail) so they are re-processed
        against the corrected QC result. All writes share one transaction, so a
        failed correction leaves no partial records to clean up on retry.
        """
        now = utcnow()
        connection = self._connect()
        affected = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (old_run_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + old_run_id)
            old = self._entity_from_row(row)
            if old["kind"] != "qc_run":
                raise ValidationError("correct is only supported for qc_run")
            current_version = int(old["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )

            # New version: a fresh qc_run at version 1, pending re-evaluation.
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'qc_run', 'pending', 1, ?, ?, ?, ?)",
                (new_run_id, json.dumps(new_data, ensure_ascii=False, sort_keys=True),
                 actor.user_id, now, now),
            )

            # Void the old version and link it to the new one.
            old_data = dict(old["data"])
            old_data["superseded_by_run_id"] = new_run_id
            old_history = list(old_data.get("correction_history") or [])
            old_history.append({
                "actor_id": actor.user_id,
                "reason": reason,
                "from_status": old["status"],
                "superseded_by_run_id": new_run_id,
                "at": now,
            })
            old_data["correction_history"] = old_history
            cursor = connection.execute(
                "UPDATE entities SET status = 'voided', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (json.dumps(old_data, ensure_ascii=False, sort_keys=True), now,
                 old_run_id, current_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError("version conflict while voiding qc run: " + old_run_id)

            # Repoint every batch that referenced the old run.
            batch_rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'result_batch'"
            ).fetchall()
            for brow in batch_rows:
                batch = self._entity_from_row(brow)
                if batch["data"].get("qc_run_id") != old_run_id:
                    continue
                was_released = batch["status"] == "released"
                bdata = dict(batch["data"])
                bdata["qc_run_id"] = new_run_id
                rollback = None
                if was_released:
                    rollback = {
                        "actor_id": actor.user_id,
                        "reason": reason,
                        "from_status": "released",
                        "to_status": "waiting",
                        "old_run_id": old_run_id,
                        "new_run_id": new_run_id,
                        "at": now,
                    }
                    bdata["release_rollbacks"] = list(bdata.get("release_rollbacks") or []) + [rollback]
                new_status = "waiting" if was_released else batch["status"]
                connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_status, json.dumps(bdata, ensure_ascii=False, sort_keys=True),
                     now, batch["id"]),
                )
                affected.append({
                    "batch_id": batch["id"],
                    "was_released": was_released,
                    "from_status": batch["status"],
                    "to_status": new_status,
                })
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch["id"],
                        actor.user_id,
                        actor.role,
                        "correct",
                        batch["status"],
                        new_status,
                        json.dumps(
                            {
                                "reason": reason,
                                "old_run_id": old_run_id,
                                "new_run_id": new_run_id,
                                "released_rolled_back": was_released,
                            },
                            ensure_ascii=False, sort_keys=True,
                        ),
                        now,
                    ),
                )

            # Audit trail for both versions.
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    old_run_id, actor.user_id, actor.role, "correct",
                    old["status"], "voided",
                    json.dumps({"reason": reason, "new_run_id": new_run_id,
                                "value": new_data.get("value")},
                               ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_run_id, actor.user_id, actor.role, "version_created",
                    None, "pending",
                    json.dumps({"reason": reason, "supersedes_run_id": old_run_id,
                                "value": new_data.get("value")},
                               ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )

            if idempotency_key:
                connection.execute(
                    "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (actor.user_id, idempotency_key, new_run_id, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {
            "old_run": self.get_entity(old_run_id),
            "new_run": self.get_entity(new_run_id),
            "affected": affected,
        }

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
