from __future__ import annotations

import json
import hashlib
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
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
            """CREATE TABLE IF NOT EXISTS operation_events (
            id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, state TEXT NOT NULL,
            detail TEXT NOT NULL, created_at INTEGER NOT NULL,
            FOREIGN KEY(operation_id) REFERENCES operations(id) ON DELETE CASCADE)"""
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS operation_events_operation_id "
            "ON operation_events(operation_id, id)"
        )
        self.db.commit()

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
            "SELECT state, detail FROM operation_events WHERE operation_id=? ORDER BY id DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        if previous and previous["state"] == state and previous["detail"] == encoded:
            return
        self.db.execute(
            "INSERT INTO operation_events(operation_id,state,detail,created_at) VALUES(?,?,?,?)",
            (operation_id, state, encoded, created_at or int(time.time())),
        )

    def setting(self, key: str) -> dict | None:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: dict) -> None:
        self.db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), int(time.time())),
        )
        self.db.commit()

    def security_event(self, event: str, username: str = "", client: str = "", detail: dict | None = None) -> None:
        self.db.execute(
            "INSERT INTO security_events(event,username,client,detail,created_at) VALUES(?,?,?,?,?)",
            (event, username, client, json.dumps(detail or {}), int(time.time())),
        )
        self.db.commit()

    def recent_security_events(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
        ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def create_confirmation(self, token: str, kind: str, torrent_hash: str, fingerprint: str, expires_at: int) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute(
            "INSERT INTO confirmations(token_hash,kind,torrent_hash,fingerprint,expires_at) VALUES(?,?,?,?,?)",
            (token_hash, kind, torrent_hash.casefold(), fingerprint, expires_at),
        )
        self.db.commit()

    def consume_confirmation(self, token: str, kind: str, torrent_hash: str, fingerprint: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        cursor = self.db.execute(
            """UPDATE confirmations SET used_at=? WHERE token_hash=? AND kind=? AND torrent_hash=?
            AND fingerprint=? AND used_at IS NULL AND expires_at>=?""",
            (now, token_hash, kind, torrent_hash.casefold(), fingerprint, now),
        )
        self.db.commit()
        if cursor.rowcount != 1:
            raise PermissionError("Confirmation is invalid, expired, already used, or belongs to a stale plan")

    def record(self, torrent_hash: str, app: str, state: str, detail: dict, kind: str = "reconcile") -> int:
        now = int(time.time())
        cursor = self.db.execute(
            "INSERT INTO operations(torrent_hash,app,kind,state,detail,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (torrent_hash, app, kind, state, json.dumps(detail), now, now),
        )
        operation_id = int(cursor.lastrowid)
        self._record_event(operation_id, state, detail, now)
        self.db.commit()
        print(f"stowarr operation id={operation_id} kind={kind} state={state}", flush=True)
        return operation_id

    def update(self, operation_id: int, state: str, detail: dict) -> None:
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
        rows = self.db.execute("SELECT * FROM operations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]

    def operation_events(self, operation_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, operation_id, state, detail, created_at FROM operation_events "
            "WHERE operation_id=? ORDER BY id",
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
        terminal = ("COMPLETE", "FAILED", "BLOCKED", "DRY_RUN")
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
            rows = self.db.execute(
                f"SELECT id, state FROM operations WHERE id IN ({placeholders})", selected
            ).fetchall()
            nonterminal = [row["id"] for row in rows if row["state"] not in terminal]
            if nonterminal:
                raise ValueError("Active operations cannot be removed from History")
            selected = [int(row["id"]) for row in rows]
        if not selected:
            return 0
        placeholders = ",".join("?" for _ in selected)
        self.db.execute(f"DELETE FROM operation_events WHERE operation_id IN ({placeholders})", selected)
        cursor = self.db.execute(f"DELETE FROM operations WHERE id IN ({placeholders})", selected)
        self.db.commit()
        return int(cursor.rowcount)

    def active(self, torrent_hash: str, kind: str | None = None) -> list[dict]:
        terminal = ("COMPLETE", "FAILED", "BLOCKED", "DRY_RUN")
        query = "SELECT * FROM operations WHERE torrent_hash=? AND state NOT IN (?,?,?,?)"
        values: list = [torrent_hash, *terminal]
        if kind:
            query += " AND kind=?"
            values.append(kind)
        rows = self.db.execute(query, values).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
