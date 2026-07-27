from __future__ import annotations

import threading


class OperationQueueWorker:
    """Run Move and Reconcile jobs through one shared, globally ordered slot."""

    def __init__(self, manager):
        self.manager = manager
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="stowarr-operation-queue", daemon=True
        )
        self._move_processor = MoveQueueWorker(manager)
        self._reconcile_processor = ReconcileQueueWorker(manager)

    def start(self) -> None:
        interrupted_moves = self.manager.store.interrupt_running_moves()
        interrupted_reconciles = self.manager.store.interrupt_running_reconciles()
        interrupted = interrupted_moves + interrupted_reconciles
        if interrupted:
            print(
                f"stowarr queue interrupted={interrupted}; manual recovery required before retry",
                flush=True,
            )
        self.manager.queue_worker = self
        self.manager.reconcile_queue_worker = self
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        return not self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _wait(self, seconds: float = 2) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.manager.connections_ready or not self.manager.config.apply:
                self._wait()
                continue
            with self.manager._move_lock:
                job = self.manager.store.claim_next_operation()
                if job:
                    if job["kind"] == "move":
                        self._move_processor._process(job)
                    else:
                        self._reconcile_processor._process(job)
            if not job:
                self._wait()


class MoveQueueWorker:
    """Run confirmed Move transactions serially without replaying interrupted work."""

    def __init__(self, manager):
        self.manager = manager
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stowarr-move-queue", daemon=True)

    def start(self) -> None:
        interrupted = self.manager.store.interrupt_running_moves()
        if interrupted:
            print(
                f"stowarr queue interrupted={interrupted}; manual recovery required before retry",
                flush=True,
            )
        self.manager.queue_worker = self
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        return not self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _wait(self, seconds: float = 2) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.manager.connections_ready or not self.manager.config.apply:
                self._wait()
                continue
            job = self.manager.store.claim_next_move()
            if not job:
                self._wait()
                continue
            self._process(job)

    def _process(self, job: dict) -> None:
        """Revalidate and execute one claimed job, recording one terminal outcome."""
        operation_id = None
        previous = self.manager.store.latest_operation(job["torrent_hash"], "move")
        previous_operation_id = previous["id"] if previous else 0
        try:
            print(
                f"stowarr queue id={job['id']} state=RUNNING torrent={job['torrent_hash']}",
                flush=True,
            )
            payload = job["payload"]
            plan = self.manager.move_plan(job["torrent_hash"], job["target_pool"]).json()
            fingerprint = self.manager._operation_fingerprint("move", plan, payload)
            if fingerprint != job["fingerprint"]:
                raise RuntimeError(
                    "The Move plan changed after it was queued. Review the current plan and queue it again."
                )
            result = self.manager.move(
                job["torrent_hash"],
                job["target_pool"],
                payload["additionalFiles"],
                wait_for_slot=True,
                public_id=job["public_id"],
            )
            operation_id = result.get("operation_id")
            state = result.get("state")
            if state != "COMPLETE":
                raise RuntimeError(f"Queued Move ended in state {state}")
            self.manager.store.finish_move(job["id"], "COMPLETE", operation_id)
            print(
                f"stowarr queue id={job['id']} state=COMPLETE operation={operation_id}",
                flush=True,
            )
        except Exception as error:
            if operation_id is None:
                latest = self.manager.store.latest_operation(job["torrent_hash"], "move")
                if latest and latest["id"] > previous_operation_id:
                    operation_id = latest["id"]
            self.manager.store.finish_move(job["id"], "FAILED", operation_id, str(error))
            print(f"stowarr queue id={job['id']} state=FAILED error={error}", flush=True)


class ReconcileQueueWorker:
    """Run confirmed Reconcile jobs serially in their own persistent queue."""

    def __init__(self, manager):
        self.manager = manager
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="stowarr-reconcile-queue", daemon=True
        )

    def start(self) -> None:
        self.manager.store.interrupt_running_reconciles()
        self.manager.reconcile_queue_worker = self
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=5)
        return not self._thread.is_alive()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.manager.connections_ready or not self.manager.config.apply:
                self._wake.wait(2)
                self._wake.clear()
                continue
            job = self.manager.store.claim_next_reconcile()
            if not job:
                self._wake.wait(2)
                self._wake.clear()
                continue
            self._process(job)

    def _process(self, job: dict) -> None:
        operation_id = None
        previous = self.manager.store.latest_operation(job["torrent_hash"], "reconcile")
        previous_id = previous["id"] if previous else 0
        try:
            payload = job["payload"]
            plan = self.manager.plan(job["torrent_hash"]).json()
            fingerprint = self.manager._operation_fingerprint("reconcile", plan, payload)
            if fingerprint != job["fingerprint"]:
                raise RuntimeError(
                    "The Reconcile plan changed after it was queued. Review it and queue it again."
                )
            self.manager._move_lock.acquire()
            try:
                result = self.manager.reconcile(
                    job["torrent_hash"],
                    set(payload["auxiliaryFiles"]),
                    public_id=job["public_id"],
                )
            finally:
                self.manager._move_lock.release()
            operation_id = result.get("operation_id")
            if result.get("state") != "COMPLETE":
                raise RuntimeError(f"Queued Reconcile ended in state {result.get('state')}")
            self.manager.store.finish_reconcile(job["id"], "COMPLETE", operation_id)
        except Exception as error:
            if operation_id is None:
                latest = self.manager.store.latest_operation(job["torrent_hash"], "reconcile")
                if latest and latest["id"] > previous_id:
                    operation_id = latest["id"]
            self.manager.store.finish_reconcile(
                job["id"], "FAILED", operation_id, str(error)
            )
