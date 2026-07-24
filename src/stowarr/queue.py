from __future__ import annotations

import threading


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
