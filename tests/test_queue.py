import unittest

from stowarr.queue import MoveQueueWorker


class FakePlan:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


class FakeStore:
    def __init__(self):
        self.finished = []
        self.latest = None

    def latest_operation(self, torrent_hash, kind):
        return self.latest

    def finish_move(self, queue_id, state, operation_id=None, error=""):
        self.finished.append((queue_id, state, operation_id, error))


class FakeManager:
    def __init__(self):
        self.store = FakeStore()
        self.move_calls = []
        self.fingerprint = "expected"
        self.move_result = {"state": "COMPLETE", "operation_id": 17}

    def move_plan(self, torrent_hash, target_pool):
        return FakePlan({"torrent_hash": torrent_hash, "target_pool": target_pool})

    def _operation_fingerprint(self, kind, plan, payload):
        return self.fingerprint

    def move(self, torrent_hash, target_pool, additional_files, wait_for_slot=False):
        self.move_calls.append((torrent_hash, target_pool, additional_files, wait_for_slot))
        return self.move_result


class MoveQueueWorkerTest(unittest.TestCase):
    def job(self, fingerprint="expected"):
        return {
            "id": 4,
            "torrent_hash": "abc",
            "target_pool": "p1",
            "payload": {"additionalFiles": [{"source": "/old", "action": "move"}]},
            "fingerprint": fingerprint,
        }

    def test_process_executes_revalidated_job_and_records_completion(self):
        manager = FakeManager()
        MoveQueueWorker(manager)._process(self.job())

        self.assertEqual(
            manager.move_calls,
            [("abc", "p1", [{"source": "/old", "action": "move"}], True)],
        )
        self.assertEqual(manager.store.finished, [(4, "COMPLETE", 17, "")])

    def test_process_rejects_changed_plan_without_mutating_services(self):
        manager = FakeManager()
        MoveQueueWorker(manager)._process(self.job(fingerprint="stale"))

        self.assertEqual(manager.move_calls, [])
        self.assertEqual(manager.store.finished[0][0:3], (4, "FAILED", None))
        self.assertIn("plan changed", manager.store.finished[0][3])

    def test_process_links_operation_created_before_move_failure(self):
        manager = FakeManager()

        def fail_move(*args, **kwargs):
            manager.store.latest = {"id": 21}
            raise RuntimeError("move failed")

        manager.move = fail_move
        MoveQueueWorker(manager)._process(self.job())

        self.assertEqual(manager.store.finished, [(4, "FAILED", 21, "move failed")])


if __name__ == "__main__":
    unittest.main()
