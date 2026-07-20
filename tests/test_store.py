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
