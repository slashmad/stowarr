from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path


class Store:
    JOB_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    TERMINAL_OPERATION_STATES = ("COMPLETE", "FAILED", "BLOCKED", "DRY_RUN")

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY, torrent_hash TEXT NOT NULL, app TEXT,
            kind TEXT NOT NULL DEFAULT 'reconcile', state TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)"""
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(operations)")}
        if "kind" not in columns:
            self.db.execute("ALTER TABLE operations ADD COLUMN kind TEXT NOT NULL DEFAULT 'reconcile'")
        if "public_id" not in columns:
            self.db.execute("ALTER TABLE operations ADD COLUMN public_id TEXT")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS confirmations (
            token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, torrent_hash TEXT NOT NULL,
            fingerprint TEXT NOT NULL, expires_at INTEGER NOT NULL, used_at INTEGER)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS security_events (
            id INTEGER PRIMARY KEY, event TEXT NOT NULL, username TEXT, client TEXT,
            detail TEXT NOT NULL, created_at INTEGER NOT NULL)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS web_sessions (
            token_hash TEXT PRIMARY KEY, client TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS web_sessions_expires_at ON web_sessions(expires_at)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS operation_events (
            id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, state TEXT NOT NULL,
            detail TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE)"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS operation_events_operation_id "
            "ON operation_events(operation_id, id)"
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS move_queue (
            id INTEGER PRIMARY KEY, torrent_hash TEXT NOT NULL, target_pool TEXT NOT NULL,
            payload TEXT NOT NULL, fingerprint TEXT NOT NULL, detail TEXT NOT NULL,
            state TEXT NOT NULL, operation_id INTEGER, error TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            started_at INTEGER, finished_at INTEGER, queue_order INTEGER)"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS reconcile_queue (
            id INTEGER PRIMARY KEY, public_id TEXT NOT NULL UNIQUE,
            torrent_hash TEXT NOT NULL, payload TEXT NOT NULL,
            fingerprint TEXT NOT NULL, detail TEXT NOT NULL,
            state TEXT NOT NULL, operation_id INTEGER, error TEXT,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
            started_at INTEGER, finished_at INTEGER, queue_order INTEGER)"""
        )
        queue_columns = {row[1] for row in self.db.execute("PRAGMA table_info(move_queue)")}
        if "public_id" not in queue_columns:
            self.db.execute("ALTER TABLE move_queue ADD COLUMN public_id TEXT")
        if "queue_order" not in queue_columns:
            self.db.execute("ALTER TABLE move_queue ADD COLUMN queue_order INTEGER")
        reconcile_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(reconcile_queue)")
        }
        if "queue_order" not in reconcile_columns:
            self.db.execute("ALTER TABLE reconcile_queue ADD COLUMN queue_order INTEGER")
        self._backfill_public_ids()
        self._backfill_queue_order()
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS operations_public_id ON operations(public_id)"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS move_queue_public_id ON move_queue(public_id)"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS move_queue_active_torrent "
            "ON move_queue(torrent_hash) WHERE state IN ('QUEUED','RUNNING')"
        )
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS reconcile_queue_active_torrent "
            "ON reconcile_queue(torrent_hash) WHERE state IN ('QUEUED','RUNNING')"
        )
        self.db.commit()

    def _new_public_id(self) -> str:
        """Return a short id unique across operation history and queued work."""
        for _ in range(100):
            value = "".join(secrets.choice(self.JOB_ID_ALPHABET) for _ in range(5))
            if value.isalpha() or value.isdigit():
                continue
            exists = self.db.execute(
                """SELECT 1 FROM operations WHERE public_id=?
                UNION ALL SELECT 1 FROM move_queue WHERE public_id=?
                UNION ALL SELECT 1 FROM reconcile_queue WHERE public_id=? LIMIT 1""",
                (value, value, value),
            ).fetchone()
            if not exists:
                return value
        raise RuntimeError("Could not allocate a unique job ID")

    def _backfill_public_ids(self) -> None:
        for row in self.db.execute(
            "SELECT id FROM operations WHERE public_id IS NULL OR public_id=''"
        ).fetchall():
            self.db.execute(
                "UPDATE operations SET public_id=? WHERE id=?",
                (self._new_public_id(), row["id"]),
            )
        for row in self.db.execute(
            """SELECT q.id, o.public_id AS operation_public_id
            FROM move_queue q LEFT JOIN operations o ON o.id=q.operation_id
            WHERE q.public_id IS NULL OR q.public_id=''"""
        ).fetchall():
            self.db.execute(
                "UPDATE move_queue SET public_id=? WHERE id=?",
                (row["operation_public_id"] or self._new_public_id(), row["id"]),
            )

    def _backfill_queue_order(self) -> None:
        current = self.db.execute(
            """SELECT MAX(queue_order) AS value FROM (
            SELECT queue_order FROM move_queue
            UNION ALL SELECT queue_order FROM reconcile_queue
            )"""
        ).fetchone()["value"] or 0
        rows = self.db.execute(
            """SELECT 'move' AS kind,id,created_at FROM move_queue WHERE queue_order IS NULL
            UNION ALL
            SELECT 'reconcile' AS kind,id,created_at FROM reconcile_queue WHERE queue_order IS NULL
            ORDER BY created_at,kind,id"""
        ).fetchall()
        for row in rows:
            current += 1
            table = "move_queue" if row["kind"] == "move" else "reconcile_queue"
            # The table is selected from two constants above.
            self.db.execute(
                f"UPDATE {table} SET queue_order=? WHERE id=?",  # nosec
                (current, row["id"]),
            )

    def _next_queue_order(self) -> int:
        row = self.db.execute(
            """SELECT MAX(queue_order) AS value FROM (
            SELECT queue_order FROM move_queue
            UNION ALL SELECT queue_order FROM reconcile_queue
            )"""
        ).fetchone()
        return int(row["value"] or 0) + 1

    @staticmethod
    def _event_detail(detail: dict) -> dict:
        progress = detail.get("progress") or {}
        event = {
            key: progress[key]
            for key in ("percent", "message", "current", "completed_bytes", "total_bytes", "qbit_state")
            if progress.get(key) not in (None, "")
        }
        for key in ("error", "recovery", "failed_after"):
            if detail.get(key) not in (None, ""):
                event[key] = detail[key]
        return event

    def _record_event(self, operation_id: int, state: str, detail: dict, created_at: int | None = None) -> None:
        event = self._event_detail(detail)
        encoded = json.dumps(event, sort_keys=True)
        previous = self.db.execute(
            "SELECT id, state, detail FROM operation_events WHERE operation_id=? ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if previous and previous["state"] == state:
            if previous["detail"] == encoded:
                return
            self.db.execute(
                "UPDATE operation_events SET detail=?,created_at=? WHERE id=?",
                (encoded, created_at or int(time.time()), previous["id"]),
            )
            return
        self.db.execute(
            "INSERT INTO operation_events(operation_id,state,detail,created_at) VALUES(?,?,?,?)",
            (operation_id, state, encoded, created_at or int(time.time())),
        )

    def setting(self, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), int(time.time())),
            )
            self.db.commit()

    def security_event(self, event: str, username: str = "", client: str = "", detail: dict | None = None) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO security_events(event,username,client,detail,created_at) VALUES(?,?,?,?,?)",
                (event, username, client, json.dumps(detail or {}), int(time.time())),
            )
            self.db.execute(
                """DELETE FROM security_events WHERE id NOT IN (
                SELECT id FROM security_events ORDER BY id DESC LIMIT 500
                )"""
            )
            self.db.commit()

    def recent_security_events(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def create_web_session(self, token_hash: str, client: str, created_at: int, expires_at: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (created_at,))
            self.db.execute(
                """INSERT INTO web_sessions(token_hash,client,created_at,expires_at)
                VALUES(?,?,?,?)""",
                (token_hash, client, created_at, expires_at),
            )
            self.db.commit()

    def web_session(self, token_hash: str, now: int) -> dict | None:
        with self.lock:
            self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
            row = self.db.execute(
                "SELECT token_hash,client,created_at,expires_at FROM web_sessions WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            self.db.commit()
        return dict(row) if row else None

    def web_sessions(self, now: int) -> list[dict]:
        with self.lock:
            self.db.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
            rows = self.db.execute(
                """SELECT token_hash,client,created_at,expires_at FROM web_sessions
                ORDER BY created_at DESC"""
            ).fetchall()
            self.db.commit()
        return [dict(row) for row in rows]

    def delete_web_session(self, token_hash: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM web_sessions WHERE token_hash=?", (token_hash,))
            self.db.commit()

    def delete_web_sessions(self) -> int:
        with self.lock:
            cursor = self.db.execute("DELETE FROM web_sessions")
            self.db.commit()
        return int(cursor.rowcount)

    def create_confirmation(self, token: str, kind: str, torrent_hash: str, fingerprint: str, expires_at: int) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            self.db.execute(
                "INSERT INTO confirmations(token_hash,kind,torrent_hash,fingerprint,expires_at) VALUES(?,?,?,?,?)",
                (token_hash, kind, torrent_hash.casefold(), fingerprint, expires_at),
            )
            self.db.commit()

    def consume_confirmation(self, token: str, kind: str, torrent_hash: str, fingerprint: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        with self.lock:
            cursor = self.db.execute(
                """UPDATE confirmations SET used_at=? WHERE token_hash=? AND kind=? AND torrent_hash=?
                AND fingerprint=? AND used_at IS NULL AND expires_at>=?""",
                (now, token_hash, kind, torrent_hash.casefold(), fingerprint, now),
            )
            self.db.commit()
        if cursor.rowcount != 1:
            raise PermissionError("Confirmation is invalid, expired, already used, or belongs to a stale plan")

    def record(
        self, torrent_hash: str, app: str, state: str, detail: dict,
        kind: str = "reconcile", public_id: str | None = None,
    ) -> int:
        with self.lock:
            now = int(time.time())
            public_id = public_id or self._new_public_id()
            cursor = self.db.execute(
                """INSERT INTO operations(
                torrent_hash,app,kind,state,detail,created_at,updated_at,public_id
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (torrent_hash, app, kind, state, json.dumps(detail), now, now, public_id),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("Operation was not recorded")
            operation_id = int(cursor.lastrowid)
            self.db.execute(
                "UPDATE move_queue SET operation_id=? WHERE public_id=?",
                (operation_id, public_id),
            )
            self.db.execute(
                "UPDATE reconcile_queue SET operation_id=? WHERE public_id=?",
                (operation_id, public_id),
            )
            self._record_event(operation_id, state, detail, now)
            self.db.commit()
        print(
            f"stowarr job={public_id} operation_id={operation_id} kind={kind} state={state}",
            flush=True,
        )
        return operation_id

    def update(self, operation_id: int, state: str, detail: dict) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE operations SET state=?, detail=?, updated_at=? WHERE id=?",
                (state, json.dumps(detail), int(time.time()), operation_id),
            )
            self._record_event(operation_id, state, detail)
            self.db.commit()
        progress = detail.get("progress") or {}
        suffix = ""
        if progress:
            suffix = f' progress={progress.get("percent", 0)}%'
            if progress.get("current"):
                suffix += f' current={progress["current"]!r}'
            if progress.get("message"):
                suffix += f' message={progress["message"]!r}'
        print(f"stowarr operation id={operation_id} state={state}{suffix}", flush=True)

    def recent(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def operation_by_public_id(self, public_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM operations WHERE public_id=?",
                (public_id.upper(),),
            ).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None

    def operation_events(self, operation_id: int) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                """SELECT events.id, events.operation_id, events.state,
                events.detail, events.created_at
                FROM operation_events events
                JOIN (
                    SELECT state, MAX(id) AS id FROM operation_events
                    WHERE operation_id=? GROUP BY state
                ) latest ON latest.id=events.id
                ORDER BY events.id""",
                (operation_id,),
            ).fetchall()
            if rows:
                return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
            operation = self.db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if not operation:
            raise KeyError(f"Operation {operation_id} was not found")
        return [{
            "id": None,
            "operation_id": operation_id,
            "state": operation["state"],
            "detail": {
                "message": "Detailed event logging was not available when this operation ran",
                **self._event_detail(json.loads(operation["detail"])),
            },
            "created_at": operation["updated_at"],
        }]

    def delete_operations(self, operation_ids: list[int] | None = None) -> int:
        with self.lock:
            terminal = self.TERMINAL_OPERATION_STATES
            if operation_ids is None:
                rows = self.db.execute(
                    "SELECT id FROM operations WHERE state IN (?,?,?,?)", terminal
                ).fetchall()
                selected = [int(row["id"]) for row in rows]
            else:
                selected = sorted({int(value) for value in operation_ids if int(value) > 0})
                if not selected:
                    return 0
                placeholders = ",".join("?" for _ in selected)
                # Only the placeholder count is interpolated; values remain parameterized.
                rows = self.db.execute(
                    f"SELECT id, state FROM operations WHERE id IN ({placeholders})",  # nosec
                    selected,
                ).fetchall()
                nonterminal = [row["id"] for row in rows if row["state"] not in terminal]
                if nonterminal:
                    raise ValueError("Active operations cannot be removed from History")
                selected = [int(row["id"]) for row in rows]
            if not selected:
                return 0
            placeholders = ",".join("?" for _ in selected)
            self.db.execute(
                f"UPDATE move_queue SET operation_id=NULL WHERE operation_id IN ({placeholders})",  # nosec
                selected,
            )
            self.db.execute(
                f"UPDATE reconcile_queue SET operation_id=NULL WHERE operation_id IN ({placeholders})",  # nosec
                selected,
            )
            self.db.execute(
                f"DELETE FROM operation_events WHERE operation_id IN ({placeholders})",  # nosec
                selected,
            )
            cursor = self.db.execute(
                f"DELETE FROM operations WHERE id IN ({placeholders})",  # nosec
                selected,
            )
            self.db.commit()
            return int(cursor.rowcount)

    def active(self, torrent_hash: str, kind: str | None = None) -> list[dict]:
        terminal = self.TERMINAL_OPERATION_STATES
        query = "SELECT * FROM operations WHERE torrent_hash=? AND state NOT IN (?,?,?,?)"
        values: list = [torrent_hash, *terminal]
        if kind:
            query += " AND kind=?"
            values.append(kind)
        with self.lock:
            rows = self.db.execute(query, values).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    @staticmethod
    def _queue_row(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["detail"] = json.loads(result["detail"])
        return result

    def _queue_positions(self) -> dict[tuple[str, int], int]:
        rows = self.db.execute(
            """SELECT kind,id FROM (
            SELECT 'move' AS kind,id,queue_order FROM move_queue WHERE state='QUEUED'
            UNION ALL
            SELECT 'reconcile' AS kind,id,queue_order FROM reconcile_queue WHERE state='QUEUED'
            ) ORDER BY queue_order"""
        ).fetchall()
        return {
            (str(row["kind"]), int(row["id"])): index
            for index, row in enumerate(rows, start=1)
        }

    def enqueue_move(
        self, torrent_hash: str, target_pool: str, payload: dict, fingerprint: str, detail: dict
    ) -> dict:
        now = int(time.time())
        try:
            with self.lock:
                public_id = self._new_public_id()
                queue_order = self._next_queue_order()
                cursor = self.db.execute(
                    """INSERT INTO move_queue(
                    torrent_hash,target_pool,payload,fingerprint,detail,state,
                    created_at,updated_at,public_id,queue_order
                    ) VALUES(?,?,?,?,?,'QUEUED',?,?,?,?)""",
                    (
                        torrent_hash.casefold(), target_pool, json.dumps(payload, sort_keys=True),
                        fingerprint, json.dumps(detail), now, now, public_id, queue_order,
                    ),
                )
                self.db.commit()
                row = self.db.execute("SELECT * FROM move_queue WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as error:
            with self.lock:
                self.db.rollback()
            raise ValueError("This torrent already has an active queued Move") from error
        return self._queue_row(row)

    def move_queue(self, limit: int = 200) -> list[dict]:
        with self.lock:
            positions = self._queue_positions()
            rows = self.db.execute(
                """SELECT * FROM move_queue
                ORDER BY CASE state WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 ELSE 2 END,
                CASE WHEN state IN ('RUNNING','QUEUED') THEN id END ASC,
                CASE WHEN state NOT IN ('RUNNING','QUEUED') THEN id END DESC
                """
                "LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {**self._queue_row(row), "position": positions.get(("move", int(row["id"])))}
            for row in rows
        ]

    def _clear_queue(self, table: str) -> int:
        if table not in {"move_queue", "reconcile_queue"}:
            raise ValueError("Unknown queue")
        with self.lock:
            # The table is allowlisted above.
            cursor = self.db.execute(
                f"DELETE FROM {table} WHERE state!='RUNNING'"  # nosec
            )
            self.db.commit()
            return int(cursor.rowcount)

    def clear_move_queue(self) -> int:
        return self._clear_queue("move_queue")

    def clear_reconcile_queue(self) -> int:
        return self._clear_queue("reconcile_queue")

    def claim_next_move(self) -> dict | None:
        with self.lock:
            if self._has_recovery_required_unlocked():
                return None
            row = self.db.execute(
                "SELECT * FROM move_queue WHERE state='QUEUED' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = int(time.time())
            cursor = self.db.execute(
                "UPDATE move_queue SET state='RUNNING',started_at=?,updated_at=? WHERE id=? AND state='QUEUED'",
                (now, now, row["id"]),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                return None
            return self._queue_row(
                self.db.execute("SELECT * FROM move_queue WHERE id=?", (row["id"],)).fetchone()
            )

    def finish_move(
        self, queue_id: int, state: str, operation_id: int | None = None, error: str = ""
    ) -> None:
        if state not in {"COMPLETE", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError("Invalid terminal queue state")
        now = int(time.time())
        with self.lock:
            self.db.execute(
                "UPDATE move_queue SET state=?,operation_id=?,error=?,updated_at=?,finished_at=? WHERE id=?",
                (state, operation_id, error, now, now, queue_id),
            )
            self.db.commit()

    def cancel_queued_move(self, queue_id: int) -> bool:
        now = int(time.time())
        with self.lock:
            cursor = self.db.execute(
                """UPDATE move_queue SET state='CANCELLED',error='Cancelled before execution',
                updated_at=?,finished_at=? WHERE id=? AND state='QUEUED'""",
                (now, now, queue_id),
            )
            self.db.commit()
            return cursor.rowcount == 1

    def cancel_queued_move_by_public_id(self, public_id: str) -> bool:
        now = int(time.time())
        with self.lock:
            cursor = self.db.execute(
                """UPDATE move_queue SET state='CANCELLED',error='Cancelled before execution',
                updated_at=?,finished_at=? WHERE public_id=? AND state='QUEUED'""",
                (now, now, public_id.upper()),
            )
            self.db.commit()
            return cursor.rowcount == 1

    def enqueue_reconcile(
        self, torrent_hash: str, payload: dict, fingerprint: str, detail: dict
    ) -> dict:
        now = int(time.time())
        try:
            with self.lock:
                public_id = self._new_public_id()
                queue_order = self._next_queue_order()
                cursor = self.db.execute(
                    """INSERT INTO reconcile_queue(
                    public_id,torrent_hash,payload,fingerprint,detail,state,
                    created_at,updated_at,queue_order
                    ) VALUES(?,?,?,?,?,'QUEUED',?,?,?)""",
                    (
                        public_id, torrent_hash.casefold(), json.dumps(payload, sort_keys=True),
                        fingerprint, json.dumps(detail), now, now, queue_order,
                    ),
                )
                self.db.commit()
                row = self.db.execute(
                    "SELECT * FROM reconcile_queue WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
        except sqlite3.IntegrityError as error:
            with self.lock:
                self.db.rollback()
            raise ValueError("This torrent already has an active queued Reconcile") from error
        return self._queue_row(row)

    def reconcile_queue(self, limit: int = 200) -> list[dict]:
        with self.lock:
            positions = self._queue_positions()
            rows = self.db.execute(
                """SELECT * FROM reconcile_queue
                ORDER BY CASE state WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 ELSE 2 END,
                CASE WHEN state IN ('RUNNING','QUEUED') THEN id END ASC,
                CASE WHEN state NOT IN ('RUNNING','QUEUED') THEN id END DESC LIMIT ?""",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {
                **self._queue_row(row),
                "position": positions.get(("reconcile", int(row["id"]))),
            }
            for row in rows
        ]

    def claim_next_reconcile(self) -> dict | None:
        with self.lock:
            if self._has_recovery_required_unlocked():
                return None
            row = self.db.execute(
                "SELECT * FROM reconcile_queue WHERE state='QUEUED' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            now = int(time.time())
            cursor = self.db.execute(
                """UPDATE reconcile_queue SET state='RUNNING',started_at=?,updated_at=?
                WHERE id=? AND state='QUEUED'""",
                (now, now, row["id"]),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                return None
            return self._queue_row(
                self.db.execute("SELECT * FROM reconcile_queue WHERE id=?", (row["id"],)).fetchone()
            )

    def has_active_queue_work(self) -> bool:
        with self.lock:
            row = self.db.execute(
                """SELECT 1 FROM move_queue WHERE state IN ('QUEUED','RUNNING')
                UNION ALL
                SELECT 1 FROM reconcile_queue WHERE state IN ('QUEUED','RUNNING')
                LIMIT 1"""
            ).fetchone()
        return row is not None

    def claim_next_operation(self) -> dict | None:
        """Claim the oldest waiting Move or Reconcile from the shared execution queue."""
        with self.lock:
            if self._has_recovery_required_unlocked():
                return None
            row = self.db.execute(
                """SELECT kind,id,queue_order FROM (
                SELECT 'move' AS kind,id,queue_order FROM move_queue WHERE state='QUEUED'
                UNION ALL
                SELECT 'reconcile' AS kind,id,queue_order FROM reconcile_queue WHERE state='QUEUED'
                ) ORDER BY queue_order LIMIT 1"""
            ).fetchone()
            if not row:
                return None
            table = "move_queue" if row["kind"] == "move" else "reconcile_queue"
            now = int(time.time())
            # The table is selected from two constants above.
            update = f"""UPDATE {table} SET state='RUNNING',started_at=?,updated_at=?
                WHERE id=? AND state='QUEUED'"""  # nosec
            cursor = self.db.execute(
                update,
                (now, now, row["id"]),
            )
            self.db.commit()
            if cursor.rowcount != 1:
                return None
            claimed = self.db.execute(
                f"SELECT * FROM {table} WHERE id=?",  # nosec
                (row["id"],),
            ).fetchone()
            return {**self._queue_row(claimed), "kind": row["kind"]}

    def finish_reconcile(
        self, queue_id: int, state: str, operation_id: int | None = None, error: str = ""
    ) -> None:
        if state not in {"COMPLETE", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError("Invalid terminal queue state")
        now = int(time.time())
        with self.lock:
            self.db.execute(
                """UPDATE reconcile_queue SET state=?,operation_id=?,error=?,
                updated_at=?,finished_at=? WHERE id=?""",
                (state, operation_id, error, now, now, queue_id),
            )
            self.db.commit()

    def cancel_queued_reconcile_by_public_id(self, public_id: str) -> bool:
        now = int(time.time())
        with self.lock:
            cursor = self.db.execute(
                """UPDATE reconcile_queue SET state='CANCELLED',
                error='Cancelled before execution',updated_at=?,finished_at=?
                WHERE public_id=? AND state='QUEUED'""",
                (now, now, public_id.upper()),
            )
            self.db.commit()
            return cursor.rowcount == 1

    def interrupt_running_reconciles(self) -> int:
        return self.recover_interrupted_operations(kinds={"reconcile"})["queue_count"]

    def interrupt_running_moves(self) -> int:
        return self.recover_interrupted_operations(kinds={"move"})["queue_count"]

    def recover_interrupted_operations(
        self, kinds: set[str] | None = None
    ) -> dict:
        """Atomically stop interrupted work and make manual recovery explicit.

        A process loss can happen between any two external side effects. Queue
        rows and History operations therefore have to transition together, and
        no later work may run until every uncertain operation is acknowledged.
        """
        now = int(time.time())
        selected_kinds = kinds or {"move", "reconcile"}
        unknown = selected_kinds - {"move", "reconcile"}
        if unknown:
            raise ValueError(f"Unknown operation kinds: {', '.join(sorted(unknown))}")
        interrupted: list[dict] = []
        with self.lock:
            for kind in sorted(selected_kinds):
                table = "move_queue" if kind == "move" else "reconcile_queue"
                # The table is selected from the allowlist above.
                rows = self.db.execute(
                    f"SELECT * FROM {table} WHERE state='RUNNING' ORDER BY id"  # nosec
                ).fetchall()
                reason = (
                    f"Stowarr restarted while this {kind.capitalize()} was running. "
                    "The next queued operation is paused until recovery is reviewed."
                )
                for row in rows:
                    operation_id = row["operation_id"]
                    recovery = {
                        "required": True,
                        "reason": reason,
                        "detected_at": now,
                        "queue_kind": kind,
                        "queue_id": int(row["id"]),
                        "public_id": row["public_id"],
                    }
                    if operation_id:
                        operation = self.db.execute(
                            "SELECT * FROM operations WHERE id=?", (operation_id,)
                        ).fetchone()
                    else:
                        operation = None
                    if (
                        operation is not None
                        and operation["state"] in self.TERMINAL_OPERATION_STATES
                    ):
                        queue_state = (
                            "COMPLETE"
                            if operation["state"] == "COMPLETE"
                            else "FAILED"
                        )
                        terminal_message = (
                            "Recovered queue bookkeeping from the linked terminal "
                            f"History state {operation['state']}."
                        )
                        self.db.execute(
                            f"""UPDATE {table} SET state=?,operation_id=?,error=?,
                            updated_at=?,finished_at=? WHERE id=?"""  # nosec
                            ,
                            (
                                queue_state,
                                operation_id,
                                terminal_message,
                                now,
                                now,
                                row["id"],
                            ),
                        )
                        continue
                    if operation is None:
                        queue_detail = json.loads(row["detail"])
                        detail = {
                            **queue_detail,
                            "torrent_hash": row["torrent_hash"],
                            "recovery": recovery,
                            "failed_after": "QUEUE_RUNNING_BEFORE_OPERATION_REGISTRATION",
                        }
                        cursor = self.db.execute(
                            """INSERT INTO operations(
                            torrent_hash,app,kind,state,detail,created_at,updated_at,public_id
                            ) VALUES(?,?,?,'RECOVERY_REQUIRED',?,?,?,?)""",
                            (
                                row["torrent_hash"],
                                str(queue_detail.get("app") or ""),
                                kind,
                                json.dumps(detail),
                                row["started_at"] or row["created_at"],
                                now,
                                row["public_id"],
                            ),
                        )
                        if cursor.lastrowid is None:
                            raise RuntimeError("Interrupted operation was not recorded")
                        operation_id = int(cursor.lastrowid)
                        self._record_event(
                            operation_id, "RECOVERY_REQUIRED", detail, now
                        )
                    interrupted.append(
                        {
                            "kind": kind,
                            "queue_id": int(row["id"]),
                            "operation_id": int(operation_id),
                            "public_id": row["public_id"],
                        }
                    )
                    self.db.execute(
                        f"""UPDATE {table} SET state='INTERRUPTED',operation_id=?,
                        error=?,updated_at=?,finished_at=? WHERE id=?"""  # nosec
                        ,
                        (operation_id, reason, now, now, row["id"]),
                    )

            placeholders = ",".join("?" for _ in self.TERMINAL_OPERATION_STATES)
            active = self.db.execute(
                f"""SELECT * FROM operations
                WHERE state NOT IN ({placeholders},'RECOVERY_REQUIRED')"""  # nosec
                ,
                self.TERMINAL_OPERATION_STATES,
            ).fetchall()
            for row in active:
                if row["kind"] not in selected_kinds:
                    continue
                detail = json.loads(row["detail"])
                previous_state = row["state"]
                recovery = {
                    **(detail.get("recovery") or {}),
                    "required": True,
                    "reason": (
                        "Stowarr restarted before this operation reached a terminal state. "
                        "External state must be inspected before more writes are allowed."
                    ),
                    "detected_at": now,
                    "previous_state": previous_state,
                    "public_id": row["public_id"],
                }
                detail = {
                    **detail,
                    "recovery": recovery,
                    "failed_after": detail.get("failed_after") or previous_state,
                }
                self.db.execute(
                    """UPDATE operations SET state='RECOVERY_REQUIRED',detail=?,updated_at=?
                    WHERE id=?""",
                    (json.dumps(detail), now, row["id"]),
                )
                self._record_event(
                    int(row["id"]), "RECOVERY_REQUIRED", detail, now
                )
                if not any(
                    item["operation_id"] == int(row["id"]) for item in interrupted
                ):
                    interrupted.append(
                        {
                            "kind": row["kind"],
                            "queue_id": None,
                            "operation_id": int(row["id"]),
                            "public_id": row["public_id"],
                        }
                    )
            self.db.commit()
        return {
            "queue_count": sum(
                1 for item in interrupted if item["queue_id"] is not None
            ),
            "operation_count": len(interrupted),
            "operations": interrupted,
        }

    def recovery_required(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                """SELECT * FROM operations WHERE state='RECOVERY_REQUIRED'
                ORDER BY updated_at,id"""
            ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def has_recovery_required(self) -> bool:
        with self.lock:
            return self._has_recovery_required_unlocked()

    def _has_recovery_required_unlocked(self) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM operations WHERE state='RECOVERY_REQUIRED' LIMIT 1"
        ).fetchone()
        return row is not None

    def resolve_recovery(self, public_id: str, note: str) -> dict:
        """Acknowledge a manually inspected operation without replaying it."""
        note = note.strip()
        if len(note) < 3:
            raise ValueError("A recovery inspection note is required")
        now = int(time.time())
        with self.lock:
            row = self.db.execute(
                """SELECT * FROM operations
                WHERE public_id=? AND state='RECOVERY_REQUIRED'""",
                (public_id.upper(),),
            ).fetchone()
            if not row:
                raise KeyError(f"Recovery operation {public_id} was not found")
            detail = json.loads(row["detail"])
            detail["recovery"] = {
                **(detail.get("recovery") or {}),
                "required": False,
                "resolved_at": now,
                "resolution": "manual_acknowledgement",
                "note": note,
            }
            detail["error"] = (
                "Interrupted by a Stowarr restart; external state was manually reviewed."
            )
            self.db.execute(
                """UPDATE operations SET state='FAILED',detail=?,updated_at=?
                WHERE id=? AND state='RECOVERY_REQUIRED'""",
                (json.dumps(detail), now, row["id"]),
            )
            self._record_event(int(row["id"]), "FAILED", detail, now)
            self.db.commit()
            resolved = self.db.execute(
                "SELECT * FROM operations WHERE id=?", (row["id"],)
            ).fetchone()
        return {**dict(resolved), "detail": json.loads(resolved["detail"])}

    def latest_operation(self, torrent_hash: str, kind: str = "move") -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM operations WHERE torrent_hash=? AND kind=? ORDER BY id DESC LIMIT 1",
                (torrent_hash, kind),
            ).fetchone()
        return {**dict(row), "detail": json.loads(row["detail"])} if row else None
