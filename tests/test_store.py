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

    def test_move_queue_is_fifo_persistent_and_rejects_duplicate_active_torrent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            first = store.enqueue_move(
                "FIRST", "p1", {"additionalFiles": []}, "first-fingerprint", {"torrent_name": "First"}
            )
            second = store.enqueue_move(
                "SECOND", "p3", {"additionalFiles": []}, "second-fingerprint", {"torrent_name": "Second"}
            )
            with self.assertRaisesRegex(ValueError, "already has an active"):
                store.enqueue_move(
                    "first", "p3", {"additionalFiles": []}, "duplicate", {"torrent_name": "Duplicate"}
                )

            reopened = Store(path)
            claimed = reopened.claim_next_move()
            self.assertEqual(claimed["id"], first["id"])
            self.assertEqual(claimed["state"], "RUNNING")
            reopened.finish_move(first["id"], "COMPLETE", operation_id=42)
            self.assertEqual(reopened.claim_next_move()["id"], second["id"])

    def test_queued_move_can_be_cancelled_but_running_move_cannot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            queued = store.enqueue_move(
                "queued", "p1", {"additionalFiles": []}, "queued-fingerprint", {}
            )
            self.assertTrue(store.cancel_queued_move(queued["id"]))
            self.assertFalse(store.cancel_queued_move(queued["id"]))

            running = store.enqueue_move(
                "running", "p1", {"additionalFiles": []}, "running-fingerprint", {}
            )
            store.claim_next_move()
            self.assertFalse(store.cancel_queued_move(running["id"]))

    def test_running_queue_entries_are_interrupted_instead_of_replayed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            queued = store.enqueue_move(
                "hash", "p1", {"additionalFiles": []}, "fingerprint", {"torrent_name": "Movie"}
            )
            store.claim_next_move()

            reopened = Store(path)
            self.assertEqual(reopened.interrupt_running_moves(), 1)
            entry = next(item for item in reopened.move_queue() if item["id"] == queued["id"])
            self.assertEqual(entry["state"], "INTERRUPTED")
            self.assertIsNone(reopened.claim_next_move())

    def test_deleting_history_keeps_completed_queue_record_without_dangling_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            queued = store.enqueue_move(
                "hash", "p1", {"additionalFiles": []}, "fingerprint", {}
            )
            store.claim_next_move()
            operation_id = store.record("hash", "radarr", "COMPLETE", {}, kind="move")
            store.finish_move(queued["id"], "COMPLETE", operation_id)

            self.assertEqual(store.delete_operations([operation_id]), 1)
            entry = next(item for item in store.move_queue() if item["id"] == queued["id"])
            self.assertIsNone(entry["operation_id"])

    def test_queue_lists_active_fifo_and_terminal_entries_newest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            old = store.enqueue_move("old", "p1", {"additionalFiles": []}, "old", {})
            store.claim_next_move()
            store.finish_move(old["id"], "COMPLETE")
            new = store.enqueue_move("new", "p1", {"additionalFiles": []}, "new", {})
            store.claim_next_move()
            store.finish_move(new["id"], "FAILED")
            queued = store.enqueue_move("queued", "p1", {"additionalFiles": []}, "queued", {})

            entries = store.move_queue()
            self.assertEqual(entries[0]["id"], queued["id"])
            self.assertEqual([item["id"] for item in entries[1:]], [new["id"], old["id"]])
