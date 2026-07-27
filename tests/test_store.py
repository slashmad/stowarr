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
            self.assertRegex(record["public_id"], r"^(?=.*[A-Z])(?=.*\d)[A-Z2-9]{5}$")

    def test_public_job_ids_are_short_unique_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            first_id = store.record("first", "radarr", "COMPLETE", {})
            second_id = store.record("second", "sonarr", "COMPLETE", {})
            queued = store.enqueue_move("third", "p1", {}, "fingerprint", {})

            operations = {item["id"]: item for item in Store(path).recent()}
            public_ids = {
                operations[first_id]["public_id"],
                operations[second_id]["public_id"],
                queued["public_id"],
            }
            self.assertEqual(len(public_ids), 3)
            for public_id in public_ids:
                self.assertRegex(public_id, r"^(?=.*[A-Z])(?=.*\d)[A-Z2-9]{5}$")

    def test_existing_history_and_queue_receive_public_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            operation_id = store.record("hash", "radarr", "COMPLETE", {}, kind="move")
            queued = store.enqueue_move("queued", "p1", {}, "fingerprint", {})
            store.db.execute("DROP INDEX operations_public_id")
            store.db.execute("DROP INDEX move_queue_public_id")
            store.db.execute("UPDATE operations SET public_id=NULL")
            store.db.execute("UPDATE move_queue SET public_id=NULL")
            store.db.commit()

            reopened = Store(path)
            operation = next(item for item in reopened.recent() if item["id"] == operation_id)
            queue_item = next(item for item in reopened.move_queue() if item["id"] == queued["id"])
            self.assertTrue(operation["public_id"])
            self.assertTrue(queue_item["public_id"])

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
                operation_id, "MOVE_RELOCATING",
                {"progress": {"percent": 45, "message": "Relocating files"}},
            )
            store.update(
                operation_id, "FAILED",
                {"error": "Source disappeared", "recovery": "Rebuild the plan"},
            )

            events = Store(path).operation_events(operation_id)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["detail"]["percent"], 45)
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
            self.assertEqual(claimed["public_id"], first["public_id"])
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

    def test_reconcile_queue_is_separate_persistent_and_cancellable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            store = Store(path)
            move = store.enqueue_move("move", "p1", {}, "move-fingerprint", {})
            repair = store.enqueue_reconcile(
                "repair", {"auxiliaryFiles": ["/subtitle.srt"]},
                "repair-fingerprint", {"torrent_name": "Repair"},
            )
            operation_id = store.record(
                "repair", "radarr", "RECONCILE_VERIFYING", {},
                kind="reconcile", public_id=repair["public_id"],
            )

            reopened = Store(path)
            self.assertEqual(reopened.claim_next_move()["public_id"], move["public_id"])
            reconcile_entry = reopened.reconcile_queue()[0]
            self.assertEqual(reconcile_entry["public_id"], repair["public_id"])
            self.assertEqual(reconcile_entry["operation_id"], operation_id)
            self.assertTrue(
                reopened.cancel_queued_reconcile_by_public_id(repair["public_id"])
            )
            self.assertEqual(reopened.reconcile_queue()[0]["state"], "CANCELLED")

    def test_move_and_reconcile_share_one_global_fifo_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            first = store.enqueue_move("move-one", "p1", {}, "move-one", {})
            second = store.enqueue_reconcile(
                "repair-one", {"auxiliaryFiles": []}, "repair-one", {}
            )
            third = store.enqueue_move("move-two", "p3", {}, "move-two", {})

            move_entries = {
                item["public_id"]: item for item in store.move_queue()
            }
            reconcile_entries = {
                item["public_id"]: item for item in store.reconcile_queue()
            }
            self.assertEqual(move_entries[first["public_id"]]["position"], 1)
            self.assertEqual(reconcile_entries[second["public_id"]]["position"], 2)
            self.assertEqual(move_entries[third["public_id"]]["position"], 3)

            claimed_first = store.claim_next_operation()
            self.assertEqual(
                (claimed_first["kind"], claimed_first["public_id"]),
                ("move", first["public_id"]),
            )
            store.finish_move(claimed_first["id"], "COMPLETE")
            claimed_second = store.claim_next_operation()
            self.assertEqual(
                (claimed_second["kind"], claimed_second["public_id"]),
                ("reconcile", second["public_id"]),
            )
            store.finish_reconcile(claimed_second["id"], "COMPLETE")
            claimed_third = store.claim_next_operation()
            self.assertEqual(
                (claimed_third["kind"], claimed_third["public_id"]),
                ("move", third["public_id"]),
            )

    def test_queue_cleanup_keeps_running_work_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            finished = store.enqueue_move("finished", "p1", {}, "finished", {})
            store.claim_next_move()
            operation_id = store.record(
                "finished", "radarr", "COMPLETE", {}, kind="move",
                public_id=finished["public_id"],
            )
            store.finish_move(finished["id"], "COMPLETE", operation_id)
            store.enqueue_move("waiting", "p1", {}, "waiting", {})

            running = store.claim_next_move()
            queued = store.enqueue_move("queued", "p1", {}, "queued", {})
            self.assertEqual(store.clear_move_queue(), 2)
            remaining = store.move_queue()
            self.assertEqual([item["public_id"] for item in remaining], [running["public_id"]])
            self.assertNotEqual(remaining[0]["public_id"], queued["public_id"])
            self.assertEqual(store.recent()[0]["id"], operation_id)

    def test_restart_marks_queue_and_history_and_pauses_later_work(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            running = store.enqueue_move(
                "moving", "p3", {}, "moving-fingerprint",
                {"torrent_name": "Moving title", "app": "radarr"},
            )
            waiting = store.enqueue_reconcile(
                "waiting", {"auxiliaryFiles": []}, "waiting-fingerprint",
                {"torrent_name": "Waiting title", "app": "radarr"},
            )
            claimed = store.claim_next_operation()
            operation_id = store.record(
                "moving",
                "radarr",
                "MOVE_RELOCATING",
                {
                    "torrent_name": "Moving title",
                    "current_save_path": "/source",
                    "target_save_path": "/target",
                },
                kind="move",
                public_id=running["public_id"],
            )
            store.update(
                operation_id,
                "MOVE_RECHECKING",
                {
                    "torrent_name": "Moving title",
                    "current_save_path": "/source",
                    "target_save_path": "/target",
                },
            )

            recovered = store.recover_interrupted_operations()

            self.assertEqual(claimed["public_id"], running["public_id"])
            self.assertEqual(recovered["queue_count"], 1)
            self.assertEqual(recovered["operation_count"], 1)
            interrupted = next(
                item for item in store.move_queue()
                if item["public_id"] == running["public_id"]
            )
            self.assertEqual(interrupted["state"], "INTERRUPTED")
            operation = store.operation_by_public_id(running["public_id"])
            self.assertEqual(operation["state"], "RECOVERY_REQUIRED")
            self.assertEqual(
                operation["detail"]["recovery"]["previous_state"],
                "MOVE_RECHECKING",
            )
            self.assertTrue(store.has_recovery_required())
            self.assertIsNone(store.claim_next_operation())
            queued = next(
                item for item in store.reconcile_queue()
                if item["public_id"] == waiting["public_id"]
            )
            self.assertEqual(queued["state"], "QUEUED")

            resolved = store.resolve_recovery(
                running["public_id"],
                "qBittorrent recheck and Radarr target inspected",
            )
            self.assertEqual(resolved["state"], "FAILED")
            self.assertFalse(store.has_recovery_required())
            self.assertEqual(
                store.claim_next_operation()["public_id"],
                waiting["public_id"],
            )

    def test_restart_records_recovery_when_queue_job_has_no_history_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            queued = store.enqueue_reconcile(
                "repair", {"auxiliaryFiles": []}, "fingerprint",
                {"torrent_name": "Repair title", "app": "sonarr"},
            )
            store.claim_next_operation()

            recovered = store.recover_interrupted_operations()

            self.assertEqual(recovered["operation_count"], 1)
            operation = store.operation_by_public_id(queued["public_id"])
            self.assertEqual(operation["state"], "RECOVERY_REQUIRED")
            self.assertEqual(operation["kind"], "reconcile")
            self.assertEqual(
                operation["detail"]["failed_after"],
                "QUEUE_RUNNING_BEFORE_OPERATION_REGISTRATION",
            )

    def test_restart_repairs_queue_bookkeeping_for_terminal_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            queued = store.enqueue_move(
                "finished", "p3", {}, "fingerprint",
                {"torrent_name": "Finished title", "app": "radarr"},
            )
            store.claim_next_operation()
            store.record(
                "finished",
                "radarr",
                "COMPLETE",
                {"torrent_name": "Finished title"},
                kind="move",
                public_id=queued["public_id"],
            )

            recovered = store.recover_interrupted_operations()

            self.assertEqual(recovered["operation_count"], 0)
            self.assertFalse(store.has_recovery_required())
            queue_item = next(
                item for item in store.move_queue()
                if item["public_id"] == queued["public_id"]
            )
            self.assertEqual(queue_item["state"], "COMPLETE")

    def test_restart_marks_direct_nonterminal_operation_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            operation_id = store.record(
                "direct",
                "sonarr",
                "ARR_RESCANNING",
                {"torrent_name": "Direct title"},
                kind="reconcile",
            )

            recovered = store.recover_interrupted_operations()

            self.assertEqual(recovered["queue_count"], 0)
            self.assertEqual(recovered["operation_count"], 1)
            operation = next(
                item for item in store.recovery_required()
                if item["id"] == operation_id
            )
            self.assertEqual(
                operation["detail"]["recovery"]["previous_state"],
                "ARR_RESCANNING",
            )
