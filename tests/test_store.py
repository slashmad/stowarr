import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from stowarr.store import Store


class StoreTest(unittest.TestCase):
    def test_confirmation_is_single_use_and_bound_to_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            store.create_confirmation("secret-token", "move", "ABC", "fingerprint", int(time.time()) + 60)
            with self.assertRaises(PermissionError):
                store.consume_confirmation("secret-token", "move", "ABC", "different")
            store.consume_confirmation("secret-token", "move", "abc", "fingerprint")
            with self.assertRaises(PermissionError):
                store.consume_confirmation("secret-token", "move", "ABC", "fingerprint")

    def test_settings_are_persisted_and_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            store.set_setting("connections", {"radarr": {"url": "http://one"}})
            store.set_setting("connections", {"radarr": {"url": "http://two"}})
            self.assertEqual(Store(path).setting("connections")["radarr"]["url"], "http://two")

    def test_existing_database_is_migrated_with_operation_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db = sqlite3.connect(path)
            db.execute(
                """CREATE TABLE operations (
                id INTEGER PRIMARY KEY, torrent_hash TEXT NOT NULL, app TEXT,
                state TEXT NOT NULL, detail TEXT NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL)"""
            )
            db.commit()
            db.close()

            store = Store(path)
            operation_id = store.record("hash", "radarr", "MOVE_PLANNED", {}, kind="move")
            record = next(item for item in store.recent() if item["id"] == operation_id)
            self.assertEqual(record["kind"], "move")

    def test_active_filters_terminal_operations_and_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            active_id = store.record("hash", "sonarr", "MOVE_RELOCATING", {}, kind="move")
            store.record("hash", "sonarr", "DRY_RUN", {}, kind="reconcile")
            self.assertEqual([item["id"] for item in store.active("hash", kind="move")], [active_id])

    def test_operation_events_are_persisted_and_duplicate_progress_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            operation_id = store.record(
                "hash", "radarr", "MOVE_RELOCATING",
                {"progress": {"percent": 10, "message": "Relocating files"}},
                kind="move",
            )
            store.update(
                operation_id, "MOVE_RELOCATING",
                {"progress": {"percent": 10, "message": "Relocating files"}},
            )
            store.update(
                operation_id, "FAILED",
                {"error": "Source disappeared", "recovery": "Rebuild the plan"},
            )

            events = Store(path).operation_events(operation_id)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["detail"]["percent"], 10)
            self.assertEqual(events[1]["state"], "FAILED")
            self.assertEqual(events[1]["detail"]["recovery"], "Rebuild the plan")

    def test_history_deletion_keeps_active_operations_and_removes_event_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            complete_id = store.record("complete", "radarr", "COMPLETE", {}, kind="move")
            failed_id = store.record("failed", "sonarr", "FAILED", {"error": "failed"}, kind="move")
            active_id = store.record("active", "radarr", "MOVE_RECHECKING", {}, kind="move")

            with self.assertRaisesRegex(ValueError, "Active operations"):
                store.delete_operations([active_id])
            self.assertEqual(store.delete_operations([complete_id]), 1)
            with self.assertRaises(KeyError):
                store.operation_events(complete_id)
            self.assertEqual(store.delete_operations(), 1)
            remaining = {item["id"] for item in store.recent()}
            self.assertEqual(remaining, {active_id})
            self.assertNotIn(failed_id, remaining)
