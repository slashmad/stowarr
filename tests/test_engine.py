import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stowarr.archive import ArchiveMember, ExtractedFile
from stowarr.config import Config, Pool, Service
from stowarr.engine import (
    AuxiliaryFile,
    FilePair,
    MovePlan,
    Plan,
    Stowarr,
    is_archive,
    release_folder_warning,
    safe_restore_video_candidate,
    sha256,
    strong_release_matches_item,
    title_matches,
)


class EngineTest(unittest.TestCase):
    def test_safe_restore_video_requires_file_level_feature_evidence(self):
        self.assertTrue(
            safe_restore_video_candidate(
                "Moana 2", Path("/download/Moana.2.2024/Moana.2.2024.mkv")
            )
        )
        self.assertFalse(
            safe_restore_video_candidate(
                "Moana 2", Path("/download/Moana.2.2024/Sample.mkv")
            )
        )
        self.assertFalse(
            safe_restore_video_candidate(
                "Moana 2", Path("/download/Moana.2.2024/Extras/Moana.2.mkv")
            )
        )
        self.assertFalse(
            safe_restore_video_candidate(
                "Moana 2", Path("/download/Moana.2.2024/movie.mkv")
            )
        )

    def test_strong_release_match_requires_complete_title_and_compatible_year(self):
        item = {"title": "Crime 101", "year": 2026}

        self.assertTrue(
            strong_release_matches_item(item, "Crime.101.2026.2160p.REMUX")
        )
        self.assertFalse(
            strong_release_matches_item(item, "Crime.Scene.2026.2160p")
        )
        self.assertFalse(
            strong_release_matches_item(item, "Crime.101.2025.2160p")
        )

    def test_write_submission_is_rejected_while_recovery_is_required(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager.store = SimpleNamespace(has_recovery_required=lambda: True)
        manager.consume_confirmation = Mock()

        with self.assertRaisesRegex(RuntimeError, "Recovery"):
            manager.submit_move(
                "token",
                "hash",
                {"targetPool": "p3", "additionalFiles": {}},
            )
        manager.consume_confirmation.assert_not_called()

    def test_recovery_diagnosis_is_read_only_and_recommends_forward_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            media = target / "movie.mkv"
            media.write_bytes(b"verified-size")
            operation = {
                "id": 4,
                "public_id": "R4COV",
                "torrent_hash": "abc",
                "app": "radarr",
                "kind": "move",
                "state": "RECOVERY_REQUIRED",
                "detail": {
                    "torrent_name": "Example",
                    "current_save_path": str(Path(directory) / "source"),
                    "target_save_path": str(target),
                    "current_item_path": str(Path(directory) / "old-library"),
                    "target_item_path": str(Path(directory) / "new-library"),
                    "managed_files": [{"path": str(media), "size": media.stat().st_size}],
                    "recovery": {"previous_state": "MOVE_RECHECKING"},
                },
            }
            manager = Stowarr.__new__(Stowarr)
            manager.store = SimpleNamespace(
                operation_by_public_id=lambda public_id: operation
                if public_id == "R4COV"
                else None
            )
            manager.qbit = SimpleNamespace(
                torrent=lambda torrent_hash: {
                    "hash": torrent_hash,
                    "name": "Example",
                    "save_path": str(target),
                    "category": "radarr-pool3",
                    "state": "pausedUP",
                    "progress": 1,
                },
                files=lambda torrent_hash: [
                    {
                        "name": media.name,
                        "size": media.stat().st_size,
                        "progress": 1,
                    }
                ],
                pause=Mock(side_effect=AssertionError("diagnosis must not mutate qBit")),
            )
            manager.arr = {
                "radarr": SimpleNamespace(
                    download_mapping=lambda torrent_hash: {
                        "item": {"id": 7, "path": operation["detail"]["target_item_path"]},
                        "files": [{"path": str(media), "size": media.stat().st_size}],
                    },
                    library_mapping=Mock(
                        side_effect=AssertionError(
                            "exact download mapping should be sufficient"
                        )
                    ),
                )
            }

            result = manager.diagnose_recovery("R4COV")

            self.assertTrue(result["diagnosis"]["read_only"])
            self.assertTrue(
                result["diagnosis"]["qbittorrent"]["files"][
                    "all_visible_and_sized"
                ]
            )
            self.assertEqual(
                result["diagnosis"]["recommendation"]["code"],
                "CONTINUE_FORWARD_CANDIDATE",
            )
            manager.qbit.pause.assert_not_called()

    def test_manual_move_and_reconcile_queue_when_shared_queue_has_work(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(has_active_queue_work=lambda: True)
        manager.consume_confirmation = lambda token, kind, torrent_hash, payload, write_enabled: {
            "plan": {"torrent_name": "Example"},
            "payload": payload,
            "fingerprint": f"{kind}-fingerprint",
        }
        manager._enqueue_authorized_move = lambda torrent_hash, authorized: {
            "public_id": "M2JOB", "state": "QUEUED",
        }
        manager._enqueue_authorized_reconcile = lambda torrent_hash, authorized: {
            "public_id": "R2JOB", "state": "QUEUED",
        }
        manager._run_move = lambda *args, **kwargs: self.fail(
            "Move must not run while shared queue work exists"
        )
        manager.reconcile = lambda *args, **kwargs: self.fail(
            "Reconcile must not run while shared queue work exists"
        )

        move = manager.submit_move(
            "move-token", "move-hash",
            {"targetPool": "p1", "additionalFiles": {}},
        )
        reconcile = manager.submit_reconcile(
            "reconcile-token", "reconcile-hash", {"auxiliaryFiles": []},
        )

        self.assertEqual(
            (move["kind"], move["disposition"], move["public_id"]),
            ("move", "queued", "M2JOB"),
        )
        self.assertEqual(
            (reconcile["kind"], reconcile["disposition"], reconcile["public_id"]),
            ("reconcile", "queued", "R2JOB"),
        )

    def test_dry_run_submissions_record_directly_but_queues_remain_disabled(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=False)
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: self.fail(
                "Dry-run submissions must bypass persistent queue state"
            )
        )
        manager.consume_confirmation = lambda token, kind, torrent_hash, payload, write_enabled: {
            "plan": {"torrent_name": "Example"},
            "payload": payload,
            "fingerprint": f"{kind}-fingerprint",
        }
        manager._run_move = lambda *args, **kwargs: {
            "operation_id": 11, "state": "DRY_RUN",
        }
        manager.reconcile = lambda *args, **kwargs: {
            "operation_id": 12, "state": "DRY_RUN",
        }

        move = manager.submit_move(
            "move-token", "move-hash",
            {"targetPool": "p1", "additionalFiles": {}},
        )
        reconcile = manager.submit_reconcile(
            "reconcile-token", "reconcile-hash", {"auxiliaryFiles": []},
        )

        self.assertEqual(
            (move["state"], move["kind"], move["disposition"]),
            ("DRY_RUN", "move", "direct"),
        )
        self.assertEqual(
            (reconcile["state"], reconcile["kind"], reconcile["disposition"]),
            ("DRY_RUN", "reconcile", "direct"),
        )
        with self.assertRaisesRegex(RuntimeError, "dry-run"):
            manager.enqueue_move("token", "move-hash", {})
        with self.assertRaisesRegex(RuntimeError, "dry-run"):
            manager.enqueue_reconcile("token", "reconcile-hash", {})

    def test_dry_run_submissions_stay_dry_when_write_mode_changes_while_waiting(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=False)
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: self.fail(
                "Dry-run submissions must bypass persistent queue state"
            )
        )
        confirmed = threading.Event()
        manager.consume_confirmation = lambda token, kind, torrent_hash, payload, write_enabled: (
            confirmed.set() or {
                "plan": {"torrent_name": "Example"},
                "payload": payload,
                "fingerprint": f"{kind}-fingerprint",
            }
        )
        observed = []
        manager._run_move = lambda *args, **kwargs: (
            observed.append(("move", kwargs.get("write_enabled"))) or
            {"operation_id": 11, "state": "DRY_RUN"}
        )

        def reconcile(*args, **kwargs):
            with manager._move_lock:
                observed.append(("reconcile", kwargs.get("write_enabled")))
                return {"operation_id": 12, "state": "DRY_RUN"}

        manager.reconcile = reconcile

        def submit_move():
            manager.submit_move(
                "move-token", "move-hash",
                {"targetPool": "p1", "additionalFiles": {}},
            )

        manager._move_lock.acquire()
        move_thread = threading.Thread(target=submit_move)
        move_thread.start()
        self.assertTrue(confirmed.wait(timeout=1))
        manager.config = SimpleNamespace(apply=True)
        manager._move_lock.release()
        move_thread.join(timeout=1)
        self.assertFalse(move_thread.is_alive())

        manager.config = SimpleNamespace(apply=False)
        confirmed.clear()
        manager._move_lock.acquire()
        reconcile_thread = threading.Thread(
            target=lambda: manager.submit_reconcile(
                "reconcile-token", "reconcile-hash", {"auxiliaryFiles": []},
            )
        )
        reconcile_thread.start()
        self.assertTrue(confirmed.wait(timeout=1))
        manager.config = SimpleNamespace(apply=True)
        manager._move_lock.release()
        reconcile_thread.join(timeout=1)
        self.assertFalse(reconcile_thread.is_alive())

        self.assertEqual(observed, [("move", False), ("reconcile", False)])

    def test_dry_run_mode_is_captured_before_confirmation_is_consumed(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=False)
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: self.fail(
                "A submission that began in dry-run must never enter queue handling"
            )
        )

        def consume_confirmation(token, kind, torrent_hash, payload, write_enabled):
            manager.config = SimpleNamespace(apply=True)
            return {
                "plan": {"torrent_name": "Example"},
                "payload": payload,
                "fingerprint": f"{kind}-fingerprint",
            }

        manager.consume_confirmation = consume_confirmation
        observed = []
        manager._run_move = lambda *args, **kwargs: (
            observed.append(("move", kwargs.get("write_enabled"))) or
            {"operation_id": 11, "state": "DRY_RUN"}
        )
        manager.reconcile = lambda *args, **kwargs: (
            observed.append(("reconcile", kwargs.get("write_enabled"))) or
            {"operation_id": 12, "state": "DRY_RUN"}
        )

        move = manager.submit_move(
            "move-token", "move-hash",
            {"targetPool": "p1", "additionalFiles": {}},
        )
        manager.config = SimpleNamespace(apply=False)
        reconcile = manager.submit_reconcile(
            "reconcile-token", "reconcile-hash", {"auxiliaryFiles": []},
        )

        self.assertEqual(move["state"], "DRY_RUN")
        self.assertEqual(reconcile["state"], "DRY_RUN")
        self.assertEqual(observed, [("move", False), ("reconcile", False)])

    def test_explicit_dry_run_mode_controls_move_and_reconcile_write_gates(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        updates = []
        manager.store = SimpleNamespace(
            active=lambda *args, **kwargs: None,
            record=lambda *args, **kwargs: 17,
            update=lambda *args: updates.append(args),
        )
        manager.move_plan = lambda *args, **kwargs: MovePlan(
            "move-hash", "Example", "radarr", "p1", "p3",
            "/p1/download/Example", "/p3/download/Example", "radarr-pool3",
            42, "Example", [], 100, 1000, "ready",
        )
        reconcile_plan = Plan(
            "reconcile-hash", "Example", "radarr", "p3", 42, "Example",
            "/p1/movies/Example", "/p3/movies/Example", [], "ready",
        )

        move = manager._run_move(
            "move-hash", "p3", {}, write_enabled=False,
        )
        reconcile = manager._run_reconcile(
            "reconcile-hash", prepared_plan=reconcile_plan, write_enabled=False,
        )

        self.assertEqual(move["state"], "DRY_RUN")
        self.assertEqual(reconcile["state"], "DRY_RUN")
        self.assertEqual([update[1] for update in updates], ["DRY_RUN", "DRY_RUN"])

    def test_service_status_reports_live_versions_without_credentials(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager.qbit = SimpleNamespace(version=lambda: "5.2.1")
        manager.arr = {
            "radarr": SimpleNamespace(status=lambda: {"version": "6.0.0"}),
            "sonarr": SimpleNamespace(status=lambda: {"version": "5.0.0"}),
        }

        result = manager.service_status()

        self.assertEqual(result["version"], "1.0.0-beta.4")
        self.assertTrue(result["apply"])
        self.assertEqual(result["services"]["qbittorrent"]["version"], "5.2.1")
        self.assertEqual(result["services"]["radarr"]["status"], "connected")
        self.assertNotIn("credentials", result)

    def test_service_status_distinguishes_unavailable_and_unconfigured(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=False)
        manager.qbit = SimpleNamespace(version=lambda: (_ for _ in ()).throw(ConnectionError("offline")))
        manager.arr = {}

        result = manager.service_status()

        self.assertEqual(result["services"]["qbittorrent"]["status"], "unavailable")
        self.assertEqual(result["services"]["radarr"]["status"], "not_configured")
        self.assertEqual(result["services"]["sonarr"]["status"], "not_configured")

    def test_release_folder_warning_ignores_conventional_radarr_folder(self):
        warning = release_folder_warning(
            {
                "title": "Americana",
                "year": 2025,
                "path": "/media/movies/Americana (2025)",
            },
            "AMERICANA.2023.2160p.AMZN.WEB-DL.DDP5.1.HDR.H.265-TiTTNEYSWOONEY",
            "radarr",
        )

        self.assertIsNone(warning)

    def test_release_folder_warning_flags_different_release_name(self):
        warning = release_folder_warning(
            {
                "title": "Americana",
                "year": 2025,
                "path": "/media/movies/Americana.2023.NORDiC.1080p.BluRay.REMUX.AVC.TrueHD.7.1-EGEN",
            },
            "AMERICANA.2023.2160p.AMZN.WEB-DL.DDP5.1.HDR.H.265-TiTTNEYSWOONEY",
            "radarr",
        )

        self.assertEqual(warning["code"], "RADARR_RELEASE_FOLDER_MISMATCH")
        self.assertEqual(warning["suggestedPath"], "/media/movies/Americana (2025)")

    def test_release_folder_warning_ignores_matching_release_folder(self):
        release = "AMERICANA.2023.2160p.AMZN.WEB-DL.DDP5.1.HDR.H.265-TiTTNEYSWOONEY"
        warning = release_folder_warning(
            {"title": "Americana", "year": 2025, "path": f"/media/movies/{release}"},
            release,
            "radarr",
        )

        self.assertIsNone(warning)

    def test_move_plan_stops_before_file_discovery_when_torrent_is_already_on_target(self):
        pool = Pool(
            name="p1",
            prefix=Path("/mnt/p1/media"),
            download_roots=(Path("/mnt/p1/media/download"),),
            radarr_root=Path("/mnt/p1/media/movies"),
            sonarr_root=Path("/mnt/p1/media/series"),
            radarr_category="radarr-pool1",
            sonarr_category="sonarr-pool1",
            radarr_tag="radarr-pool1",
            sonarr_tag="sonarr-pool1",
        )
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: None,
        )

        def unexpected_file_discovery(_torrent_hash):
            raise AssertionError("same-pool recovery must not inspect or hash torrent files")

        manager.qbit = SimpleNamespace(
            torrent=lambda torrent_hash: {
                "hash": torrent_hash,
                "name": "Large.Release",
                "category": "",
                "save_path": "/mnt/p1/media/download",
                "total_size": 64 * 1024**3,
            },
            files=unexpected_file_discovery,
        )
        manager.arr = {}

        plan = manager.move_plan("hash", "p1")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "QBITTORRENT_ALREADY_ON_TARGET")
        self.assertIn("Reconcile", plan.error_details["action"])

    def test_recheck_must_be_observed_before_completion(self):
        states = iter([
            {"state": "pausedUP", "progress": 1},
            {"state": "checkingUP", "progress": .35},
            {"state": "checkingUP", "progress": .82},
            {"state": "pausedUP", "progress": 1},
        ])
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(torrent=lambda torrent_hash: next(states))
        progress = []

        with patch("stowarr.engine.time.sleep"):
            result = manager._wait_for_recheck("hash", lambda torrent, started: progress.append((torrent["state"], started)))

        self.assertEqual(result["state"], "pausedUP")
        self.assertEqual(progress[0], ("pausedUP", False))
        self.assertIn(("checkingUP", True), progress)

    def test_verified_unpackerr_derivative_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            torrent_name = "Release"
            derived = root / f"{torrent_name}_unpackerrred"
            library = root / "library" / "Movie.mkv"
            derived.mkdir()
            library.parent.mkdir()
            media = derived / "release.mkv"
            media.write_bytes(b"verified-media")
            library.write_bytes(b"verified-media")
            (derived / f"_unpackerrred.{torrent_name}.txt").write_text("complete")
            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(torrent=lambda torrent_hash: {"save_path": str(root), "name": torrent_name})

            removed = manager._cleanup_verified_unpackerr_derivatives(
                "hash", [{"target": str(library), "sha256": sha256(library)}]
            )

            self.assertEqual(removed, [str(derived)])
            self.assertFalse(derived.exists())

    def test_move_requires_a_completed_upload_state_before_success(self):
        self.assertTrue(Stowarr._is_seeding_state({"state": "stalledUP", "progress": 1}))
        self.assertTrue(Stowarr._is_seeding_state({"state": "uploading", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "pausedUP", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "stoppedUP", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "stalledUP", "progress": 0.9}))

    def test_confirmation_fingerprint_ignores_volatile_free_space(self):
        payload = {"targetPool": "p1", "additionalFiles": {}}
        first = {"status": "ready", "target_save_path": "/media/p1/download", "free_space": 100}
        second = {"status": "ready", "target_save_path": "/media/p1/download", "free_space": 99}

        self.assertEqual(
            Stowarr._operation_fingerprint("move", first, payload),
            Stowarr._operation_fingerprint("move", second, payload),
        )
        second["target_save_path"] = "/media/p3/download"
        self.assertNotEqual(
            Stowarr._operation_fingerprint("move", first, payload),
            Stowarr._operation_fingerprint("move", second, payload),
        )

    def test_confirmation_fingerprint_is_bound_to_execution_mode(self):
        operation = Stowarr._operation_fingerprint(
            "reconcile",
            {"status": "ready", "torrent_hash": "abc"},
            {"auxiliaryFiles": []},
        )

        self.assertNotEqual(
            Stowarr._confirmation_fingerprint(operation, False),
            Stowarr._confirmation_fingerprint(operation, True),
        )

    def test_title_match_rejects_unrelated_release(self):
        self.assertTrue(title_matches("The Shawshank Redemption", "The.Shawshank.Redemption.1994.1080p"))
        self.assertFalse(title_matches("The Final Cut", "The.Shawshank.Redemption.1994.1080p"))

    def test_hardlink_identity_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source = tmp_path / "download.mkv"
            library = tmp_path / "movie.mkv"
            source.write_bytes(b"media-content")
            os.link(source, library)
            self.assertEqual(source.stat().st_ino, library.stat().st_ino)
            self.assertEqual(source.stat().st_nlink, 2)
            self.assertEqual(sha256(source), sha256(library))

    def test_sha256_never_trusts_metadata_identity_as_content_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            source.write_bytes(b"release-a")
            unchanged_identity = (
                source.stat().st_dev,
                source.stat().st_ino,
                source.stat().st_size,
                1,
                1,
            )

            with (
                patch("stowarr.engine._file_identity", return_value=unchanged_identity),
                patch("stowarr.engine.hashlib.sha256", wraps=hashlib.sha256) as factory,
            ):
                first = sha256(source)
                source.write_bytes(b"release-b")
                second = sha256(source)

            self.assertNotEqual(first, second)
            self.assertEqual(factory.call_count, 2)

    def test_sha256_rejects_file_changed_during_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "movie.mkv"
            source.write_bytes(b"abcdefgh")
            changed = False
            original = (
                source.stat().st_dev,
                source.stat().st_ino,
                source.stat().st_size,
                1,
                1,
            )
            changed_identity = (*original[:3], 2, 2)

            def mutate_after_first_chunk(completed, total):
                nonlocal changed
                if not changed:
                    changed = True
                    source.write_bytes(b"ijklmnop")

            with (
                patch("stowarr.engine._file_identity", side_effect=[original, changed_identity]),
                self.assertRaisesRegex(RuntimeError, "changed while"),
            ):
                sha256(source, chunk_size=4, progress=mutate_after_first_chunk)

    def test_release_identity_accepts_exact_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "download"
            library = root / "movies" / "Movie"
            (download / "Release").mkdir(parents=True)
            library.mkdir(parents=True)
            torrent_file = download / "Release" / "Movie.mkv"
            arr_file = library / "Movie.mkv"
            torrent_file.write_bytes(b"same release")
            os.link(torrent_file, arr_file)

            result = Stowarr._release_identity(
                {"save_path": str(download)},
                [{"name": "Release/Movie.mkv", "size": torrent_file.stat().st_size, "priority": 1}],
                {"files": [{"id": 7, "path": str(arr_file), "size": arr_file.stat().st_size}]},
            )

            self.assertTrue(result["verified"])
            self.assertEqual(result["files"][0]["method"], "hardlink")

    def test_release_identity_blocks_replaced_arr_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "download"
            library = root / "movies" / "Movie"
            (download / "Release-A").mkdir(parents=True)
            library.mkdir(parents=True)
            torrent_file = download / "Release-A" / "Movie.mkv"
            arr_file = library / "Release-B.mkv"
            torrent_file.write_bytes(b"release-a")
            arr_file.write_bytes(b"release-b")

            result = Stowarr._release_identity(
                {"save_path": str(download)},
                [{"name": "Release-A/Movie.mkv", "size": torrent_file.stat().st_size, "priority": 1}],
                {"files": [{"id": 8, "path": str(arr_file), "size": arr_file.stat().st_size}]},
            )

            self.assertFalse(result["verified"])
            self.assertEqual(result["status"], "release-mismatch")
            self.assertEqual(result["files"][0]["matching_count"], 0)

    def test_plan_exposes_structured_error_details(self):
        plan = Plan(
            "hash", "torrent", "radarr", "p1", 118, "The Shawshank Redemption",
            "/media/movies/The.Final.Cut.2004", None, [], "blocked",
            "Radarr item title does not match its folder",
            "ARR_LIBRARY_FOLDER_TITLE_MISMATCH",
            {"current_folder_name": "The.Final.Cut.2004"},
        )

        payload = plan.json()
        self.assertEqual(payload["error_code"], "ARR_LIBRARY_FOLDER_TITLE_MISMATCH")
        self.assertEqual(payload["error_details"]["current_folder_name"], "The.Final.Cut.2004")

    def test_torrent_sidecars_are_hardlink_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "download"
            target = root / "library" / "Movie (2020)"
            download.mkdir()
            files = [
                {"name": "Release/Movie.mkv", "size": 100},
                {"name": "Release/Movie.sv.srt", "size": 20},
            ]

            sidecars = Stowarr._torrent_sidecars({"save_path": str(download)}, files, target)

            self.assertEqual(len(sidecars), 1)
            self.assertEqual(sidecars[0].origin, "qbittorrent")
            self.assertEqual(sidecars[0].operation, "hardlink")
            self.assertEqual(sidecars[0].target, str(target / "Movie.sv.srt"))

    def test_torrent_sidecars_block_flattened_name_conflicts(self):
        files = [
            {"name": "Release/Movie.mkv", "size": 100, "priority": 1},
            {
                "name": "Release/Subs-A/Movie.en.srt",
                "size": 20,
                "priority": 1,
            },
            {
                "name": "Release/Subs-B/Movie.en.srt",
                "size": 21,
                "priority": 1,
            },
        ]

        sidecars = Stowarr._torrent_sidecars(
            {"save_path": "/downloads"}, files, Path("/library/Movie (2020)")
        )

        self.assertEqual(len(sidecars), 2)
        self.assertEqual(
            {item.status for item in sidecars}, {"torrent-name-conflict"}
        )

    def test_archive_detection_covers_multi_part_releases(self):
        self.assertTrue(is_archive(Path("movie.rar")))
        self.assertTrue(is_archive(Path("movie.r00")))
        self.assertTrue(is_archive(Path("movie.001")))
        self.assertTrue(is_archive(Path("movie.7z")))
        self.assertFalse(is_archive(Path("movie.mkv")))

    def test_archive_integrity_is_never_skipped_from_metadata_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.rar"
            archive.write_bytes(b"archive-a")
            extractor = Mock()
            extractor.members.return_value = [ArchiveMember("movie.mkv", 100)]
            manager = Stowarr.__new__(Stowarr)
            manager.archive_extractor = extractor
            manager._archive_paths = lambda torrent_hash: [archive]

            first = manager._verify_archive_sets("hash")
            archive.write_bytes(b"archive-b")
            second = manager._verify_archive_sets("hash")

            self.assertEqual(first, second)
            self.assertEqual(extractor.test.call_count, 2)
            self.assertEqual(extractor.members.call_count, 2)

    def test_subtitle_inventory_distinguishes_subfolders_and_archives(self):
        torrent = {"save_path": "/downloads", "content_path": "/downloads/Release"}
        files = [
            {"name": "Release/Movie.en.srt", "priority": 1},
            {"name": "Release/Subs/Movie.sv.srt", "priority": 1},
            {"name": "Release/Subs/skipped.srt", "priority": 0},
        ]
        archive_members = [(Path("/downloads/Release/release.rar"), ArchiveMember("Subs/Movie.fi.srt", 42))]

        subtitles = Stowarr._subtitle_inventory(torrent, files, archive_members)

        self.assertEqual([item["location"] for item in subtitles], ["torrent", "subfolder", "archive"])
        self.assertEqual(subtitles[-1]["archive"], "/downloads/Release/release.rar")

    def test_archive_extraction_publishes_only_exact_managed_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download = root / "download"
            old_item = root / "old" / "Movie"
            new_item = root / "new" / "Movie"
            download.mkdir()
            old_item.mkdir(parents=True)
            archive = download / "release.rar"
            archive.write_bytes(b"archive")
            old_media = old_item / "Movie.mkv"
            old_media.write_bytes(b"verified-media")

            class Extractor:
                def members(self, entry):
                    return [ArchiveMember("release.mkv", len(b"verified-media"))]

                def extract(self, entry, destination):
                    destination.mkdir(parents=True)
                    output = destination / "release.mkv"
                    output.write_bytes(b"verified-media")
                    return [ExtractedFile("release.mkv", output, output.stat().st_size)]

            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(
                torrent=lambda torrent_hash: {"save_path": str(download)},
                files=lambda torrent_hash: [{"name": "release.rar", "priority": 1, "size": 7}],
            )
            manager.archive_extractor = Extractor()
            managed = {
                "id": 10, "path": str(old_media), "targetPath": str(new_item / "Movie.mkv"),
                "size": old_media.stat().st_size,
            }
            plan = MovePlan(
                "a" * 40, "release", "radarr", "p3", "p1", str(download), str(download),
                "radarr-p1", 1, "Movie", [managed], 7, 1000, "ready",
                target_item_path=str(new_item), extraction_required=True,
                extraction_space=old_media.stat().st_size, extraction_files=[managed],
            )

            published = manager._extract_managed_media("a" * 40, plan)

            self.assertEqual((new_item / "Movie.mkv").read_bytes(), b"verified-media")
            self.assertTrue(published[0]["created"])

    def test_move_preserves_relative_save_path_between_pool_download_roots(self):
        current = Pool(
            "p3", Path("/media/p3"), (Path("/media/p3/download"),),
            Path("/media/p3/movies"), Path("/media/p3/series"),
            "radarr-p3", "sonarr-p3", "radarr-p3", "sonarr-p3",
        )
        target = Pool(
            "p1", Path("/media/p1"), (Path("/media/p1/download"),),
            Path("/media/p1/movies"), Path("/media/p1/series"),
            "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
        )
        result = Stowarr._target_download_path(current, target, Path("/media/p3/download/manual"))
        self.assertEqual(result, Path("/media/p1/download/manual"))

    def test_destination_library_folder_is_not_treated_as_stale_source(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "Movie"
            destination.mkdir()
            (destination / "Movie.mkv").write_bytes(b"media")

            self.assertFalse(Stowarr._old_library_folder_remaining(destination, destination))
            self.assertTrue(Stowarr._old_library_folder_remaining(destination, destination.parent / "Other"))

    def test_move_inventory_separates_tracked_and_additional_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pool = root / "p3"
            target_pool_path = root / "p1"
            download = source_pool / "download"
            release = download / "Release"
            library = source_pool / "movies" / "Movie (2020)"
            release.mkdir(parents=True)
            library.mkdir(parents=True)
            (release / "Movie.mkv").write_bytes(b"video")
            (release / "plugin.txt").write_bytes(b"plugin")
            managed = library / "Movie.mkv"
            managed.write_bytes(b"video")
            (library / "poster.jpg").write_bytes(b"poster")
            target_pool = Pool(
                "p1", target_pool_path, (target_pool_path / "download",),
                target_pool_path / "movies", target_pool_path / "series",
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            torrent = {"save_path": str(download), "content_path": str(release)}
            torrent_files = [{"name": "Release/Movie.mkv", "size": 5, "priority": 1}]
            mapping = {"item": {"path": str(library)}, "files": [{"path": str(managed)}]}
            manager = Stowarr.__new__(Stowarr)

            tracked, additional = manager._move_inventory(
                torrent, torrent_files, mapping, target_pool, target_pool.download_roots[0], "radarr"
            )

            self.assertEqual([item["relative_path"] for item in tracked], ["Release/Movie.mkv"])
            self.assertEqual({item["scope"] for item in additional}, {"download", "library"})
            self.assertTrue(all(item["sha256"] for item in additional))

    def test_library_seeded_inventory_does_not_duplicate_download_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pool = root / "p3"
            target_pool_path = root / "p1"
            movie = source_pool / "movies" / "Movie (2020)"
            movie.mkdir(parents=True)
            managed = movie / "Movie.mkv"
            subtitle = movie / "Movie.sv.srt"
            managed.write_bytes(b"video")
            subtitle.write_bytes(b"subtitle")
            target_pool = Pool(
                "p1", target_pool_path, (target_pool_path / "download",),
                target_pool_path / "movies", target_pool_path / "series",
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            torrent = {"save_path": str(source_pool / "movies"), "content_path": str(movie)}
            torrent_files = [{"name": "Movie (2020)/Movie.mkv", "size": 5, "priority": 1}]
            mapping = {"item": {"path": str(movie)}, "files": [{"path": str(managed)}]}
            manager = Stowarr.__new__(Stowarr)

            tracked, additional = manager._move_inventory(
                torrent, torrent_files, mapping, target_pool, target_pool.download_roots[0], "radarr"
            )

            self.assertEqual([item["path"] for item in tracked], [str(managed)])
            self.assertEqual(len(additional), 1)
            self.assertEqual(additional[0]["source"], str(subtitle))
            self.assertEqual(additional[0]["scope"], "library")
            self.assertEqual(additional[0]["target"], str(target_pool.radarr_root / movie.name / subtitle.name))

    def test_reconciliation_plan_keeps_verified_library_mapping_after_qbit_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            p1 = Pool(
                "p1", root / "p1", (root / "p1" / "download",),
                root / "p1" / "movies", root / "p1" / "series",
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            p3 = Pool(
                "p3", root / "p3", (root / "p3" / "download",),
                root / "p3" / "movies", root / "p3" / "series",
                "radarr-p3", "sonarr-p3", "radarr-p3", "sonarr-p3",
            )
            release = p1.download_roots[0] / "Release"
            old_library = p3.radarr_root / "Movie (2020)"
            release.mkdir(parents=True)
            old_library.mkdir(parents=True)
            torrent_media = release / "Movie.mkv"
            library_media = old_library / "Movie.mkv"
            torrent_media.write_bytes(b"same media")
            library_media.write_bytes(b"same media")
            torrent = {
                "hash": "abc", "name": "Movie.2020", "category": "radarr-stowarr-moving-abc",
                "save_path": str(p1.download_roots[0]), "progress": 1, "total_size": torrent_media.stat().st_size,
            }
            mapping = {
                "item": {"id": 42, "title": "Movie", "path": str(old_library), "tags": []},
                "files": [{
                    "id": 7, "path": str(library_media), "relativePath": "Movie.mkv",
                    "size": library_media.stat().st_size, "episodeIds": [],
                }],
            }
            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(
                torrents=lambda: [torrent],
                files=lambda torrent_hash: [{
                    "name": "Release/Movie.mkv", "size": torrent_media.stat().st_size, "priority": 1,
                }],
            )
            manager.arr = {"radarr": SimpleNamespace(download_mapping=lambda torrent_hash: None)}
            manager.config = SimpleNamespace(
                pools=(p1, p3), apply=True,
                pool_for_path=lambda path: p1 if str(path).startswith(str(p1.prefix)) else p3,
                pool_for_category=lambda category: None,
            )

            plan = manager.plan("abc", mapping_hint=mapping, app_hint="radarr")

            self.assertEqual(plan.status, "ready")
            self.assertEqual(plan.item_id, 42)
            self.assertEqual(plan.pairs[0].torrent_file, str(torrent_media))
            self.assertEqual(plan.pairs[0].target_library, str(p1.radarr_root / "Movie (2020)" / "Movie.mkv"))

    def test_nested_move_links_rechecked_library_seeded_media_after_source_relocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_item = root / "p3" / "movies" / "Movie (2020)"
            target_item = root / "p1" / "movies" / "Movie (2020)"
            relocated = root / "p1" / "download" / "Movie (2020)" / "Movie.mkv"
            old_item.mkdir(parents=True)
            relocated.parent.mkdir(parents=True)
            relocated.write_bytes(b"qBittorrent rechecked media")
            old_source = old_item / "Movie.mkv"
            target = target_item / "Movie.mkv"
            old_sidecar = old_item / "Movie.sv.srt"
            target_sidecar = target_item / "Movie.sv.srt"
            old_sidecar.write_bytes(b"verified subtitle")
            record = {
                "id": 7, "path": str(old_source), "relativePath": "Movie.mkv",
                "size": relocated.stat().st_size, "episodeIds": [],
            }
            item = {"id": 42, "title": "Movie", "path": str(old_item), "tags": []}
            mapping = {"item": item, "files": [record]}
            refreshed = {
                "item": {**item, "path": str(target_item)},
                "files": [{**record, "path": str(target)}],
            }
            plan = Plan(
                "abc", "Movie.2020", "radarr", "p1", 42, "Movie",
                str(old_item), str(target_item),
                [FilePair(str(old_source), str(target), str(relocated), relocated.stat().st_size, "repairable")],
                "ready",
                auxiliary_files=[AuxiliaryFile(
                    str(old_sidecar), str(target_sidecar), old_sidecar.stat().st_size,
                    "missing-target", "library", "copy", "subtitle",
                )],
                managed_files=[record],
            )
            pool = Pool(
                "p1", root / "p1", (root / "p1" / "download",),
                root / "p1" / "movies", root / "p1" / "series",
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            client = SimpleNamespace(
                download_mapping=lambda torrent_hash: None,
                library_mapping=lambda paths: refreshed,
                sync_pool=lambda current, destination, tag, pool_tags: None,
                rescan=lambda item_id: None,
            )
            manager = Stowarr.__new__(Stowarr)
            manager.plan = lambda *args, **kwargs: self.fail("The prepared Move plan must not be rebuilt")
            manager.arr = {"radarr": client}
            manager.config = SimpleNamespace(apply=True, pools=(pool,))
            manager.store = SimpleNamespace(update=lambda *args, **kwargs: None)

            result = manager.reconcile(
                "abc", {str(old_sidecar)}, operation_id=9, mapping_hint=mapping, app_hint="radarr",
                relocated_library_sources={str(old_source)}, prepared_plan=plan,
            )

            self.assertEqual(result["state"], "COMPLETE")
            self.assertTrue(target.exists())
            self.assertEqual((target.stat().st_dev, target.stat().st_ino),
                             (relocated.stat().st_dev, relocated.stat().st_ino))
            self.assertEqual(target_sidecar.read_bytes(), b"verified subtitle")
            self.assertFalse(old_sidecar.exists())

    def test_verified_additional_copy_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            target = root / "target.srt"
            source.write_bytes(b"original")
            expected = sha256(source)
            source.write_bytes(b"changed")
            manager = Stowarr.__new__(Stowarr)

            with self.assertRaises(RuntimeError):
                manager._copy_verified(source, target, expected)

            self.assertFalse(target.exists())

    def test_reconcile_plan_blocks_missing_history_and_reports_packed_media_and_category(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3", "radarr-pool3", "sonarr-pool3",
        )
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: [{
                "hash": "click", "name": "Click.2006.2160p.UHD.BluRay",
                "category": "radarr", "save_path": "/p3/download/Click",
            }],
            files=lambda torrent_hash: [{
                "name": "Click.2006.2160p.UHD.BluRay/click.rar",
                "size": 100, "priority": 1,
            }],
        )
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: None,
        )
        manager.arr = {"radarr": SimpleNamespace(
            download_mapping=lambda torrent_hash: None,
            all_items=lambda: [{"id": 7, "title": "Click", "path": "/p1/movies/Click (2006)"}],
        )}

        plan = manager.plan("click")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "ARR_DOWNLOAD_HISTORY_MISSING")
        self.assertEqual(
            [issue["code"] for issue in plan.error_details["issues"]],
            [
                "QBITTORRENT_CATEGORY_UNROUTED",
                "ARR_DOWNLOAD_HISTORY_MISSING",
                "PACKED_MEDIA_REQUIRES_IMPORT",
            ],
        )
        self.assertTrue(plan.error_details["contains_archives"])
        self.assertEqual(plan.error_details["candidates"][0]["title"], "Click")

    def test_missing_radarr_title_requires_add_new_before_manual_import(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3",
            "radarr-pool3", "sonarr-pool3",
        )
        torrent = {
            "hash": "MISSING", "name": "Missing.Movie.2024.REMUX",
            "category": "radarr-pool3",
            "save_path": "/p3/download/Missing.Movie.2024.REMUX",
        }
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: [torrent],
            files=lambda torrent_hash: [{
                "name": "Missing.Movie.2024.REMUX.mkv",
                "size": 100,
                "priority": 1,
            }],
            categories=lambda: {
                "radarr-pool3": {"savePath": "/p3/download"},
            },
        )
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: (pool, "radarr"),
        )
        manager.arr = {"radarr": SimpleNamespace(
            download_mapping=lambda torrent_hash: None,
            history_for_downloads=lambda hashes: {"missing": 42},
            all_items=lambda: [],
        )}

        plan = manager.plan("MISSING")
        audit = manager.sync_audit("radarr")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "ARR_HISTORY_ITEM_MISSING")
        self.assertEqual(plan.item_id, 42)
        self.assertIn(
            "Radarr Add New",
            plan.error_details["issues"][0]["action"],
        )
        self.assertEqual(audit["rows"][0]["status"], "missing-item")
        self.assertIn(
            "Radarr Add New",
            audit["rows"][0]["action"],
        )

    def test_reconcile_plan_blocks_multiple_torrents_for_one_arr_item(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3", "radarr-pool3", "sonarr-pool3",
        )
        torrents = [
            {
                "hash": "beast-a", "name": "Beast.2026.2160p.GUACAMOLE",
                "category": "radarr-pool3", "save_path": "/p3/download/Beast-A",
            },
            {
                "hash": "beast-b", "name": "Beast.2026.2160p.FraMeSToR",
                "category": "radarr-pool3", "save_path": "/p3/download/Beast-B",
            },
        ]
        item = {"id": 42, "title": "Beast", "path": "/p1/movies/Beast (2026)"}
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(torrents=lambda: torrents, files=lambda torrent_hash: [])
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: (pool, "radarr"),
        )
        manager.arr = {"radarr": SimpleNamespace(
            download_mapping=lambda torrent_hash: {"item": item, "files": []},
            history_for_downloads=lambda hashes: {"beast-a": 42},
        )}

        plan = manager.plan("beast-a")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "ARR_ITEM_HAS_MULTIPLE_TORRENTS")
        self.assertEqual(len(plan.error_details["related_torrents"]), 2)
        self.assertEqual(
            {item["evidence"] for item in plan.error_details["related_torrents"]},
            {"exact-history", "strong-title"},
        )
        self.assertIn("seeding", plan.error_details["action"])

    def test_radarr_plan_restores_one_missing_video_and_selected_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download_root = root / "download"
            release = download_root / "Moana.2.2024.REMUX"
            movie = release / "Moana.2.2024.REMUX.mkv"
            english = release / "Moana.2.2024.REMUX.en.srt"
            swedish = release / "Moana.2.2024.REMUX.sv.srt"
            ignored = release / "Moana.2.2024.REMUX.no.srt"
            release.mkdir(parents=True)
            movie.write_bytes(b"verified movie")
            english.write_bytes(b"english")
            swedish.write_bytes(b"swedish")
            ignored.write_bytes(b"not selected")
            movie_root = root / "movies"
            item = {
                "id": 66,
                "title": "Moana 2",
                "path": str(movie_root / "Moana 2 (2024)"),
            }
            torrent = {
                "hash": "MOANA",
                "name": "Moana.2.2024.REMUX",
                "category": "radarr-pool3",
                "save_path": str(download_root),
            }
            records = [
                {
                    "name": str(path.relative_to(download_root)),
                    "size": path.stat().st_size,
                    "priority": priority,
                }
                for path, priority in (
                    (movie, 1), (english, 1), (swedish, 1), (ignored, 0)
                )
            ]
            pool = Pool(
                "p3", root, (download_root,), movie_root, root / "series",
                "radarr-pool3", "sonarr-pool3",
                "radarr-pool3", "sonarr-pool3",
            )
            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(
                torrents=lambda: [torrent],
                files=lambda torrent_hash: records,
            )
            manager.config = SimpleNamespace(
                pools=(pool,),
                pool_for_path=lambda path: pool,
                pool_for_category=lambda category: (pool, "radarr"),
            )
            manager.arr = {"radarr": SimpleNamespace(
                download_mapping=lambda torrent_hash: {
                    "app": "radarr", "item": item, "files": [],
                },
                history_for_downloads=lambda hashes: {"moana": 66},
            )}

            plan = manager.plan("MOANA")

            self.assertEqual(plan.status, "ready")
            self.assertEqual(len(plan.pairs), 1)
            self.assertEqual(plan.pairs[0].status, "missing-library")
            self.assertEqual(plan.pairs[0].torrent_file, str(movie))
            self.assertEqual(
                plan.pairs[0].target_library,
                str(movie_root / "Moana 2 (2024)" / movie.name),
            )
            self.assertEqual(
                {Path(item.source).name for item in plan.auxiliary_files},
                {english.name, swedish.name},
            )
            self.assertEqual(plan.managed_files[0]["id"], None)
            self.assertTrue(plan.managed_files[0]["plannedRestore"])

            verification = manager.verify("MOANA")
            self.assertEqual(verification["status"], "ready-to-restore")
            self.assertIsNone(
                verification["video_files"][0]["old_matches_torrent"]
            )

    def test_radarr_missing_media_restore_blocks_multiple_selected_videos(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3",
            "radarr-pool3", "sonarr-pool3",
        )
        item = {
            "id": 66, "title": "Moana 2",
            "path": "/p3/movies/Moana 2 (2024)",
        }
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: [{
                "hash": "MOANA", "name": "Moana.2.2024",
                "category": "radarr-pool3",
                "save_path": "/p3/download/Moana.2.2024",
            }],
            files=lambda torrent_hash: [
                {"name": "disc1.mkv", "size": 10, "priority": 1},
                {"name": "disc2.mkv", "size": 11, "priority": 1},
            ],
        )
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: (pool, "radarr"),
        )
        manager.arr = {"radarr": SimpleNamespace(
            download_mapping=lambda torrent_hash: {
                "app": "radarr", "item": item, "files": [],
            },
            history_for_downloads=lambda hashes: {"moana": 66},
        )}

        plan = manager.plan("MOANA")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "ARR_MANAGED_MEDIA_MISSING")
        self.assertEqual(plan.error_details["torrent_video_count"], 2)

    def test_radarr_missing_media_restore_rejects_sample_as_feature(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3",
            "radarr-pool3", "sonarr-pool3",
        )
        item = {
            "id": 66, "title": "Moana 2",
            "path": "/p3/movies/Moana 2 (2024)",
        }
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: [{
                "hash": "MOANA", "name": "Moana.2.2024",
                "category": "radarr-pool3",
                "save_path": "/p3/download/Moana.2.2024",
            }],
            files=lambda torrent_hash: [
                {"name": "Sample.mkv", "size": 10, "priority": 1},
            ],
        )
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: (pool, "radarr"),
        )
        manager.arr = {"radarr": SimpleNamespace(
            download_mapping=lambda torrent_hash: {
                "app": "radarr", "item": item, "files": [],
            },
            history_for_downloads=lambda hashes: {"moana": 66},
        )}

        plan = manager.plan("MOANA")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.error_code, "RADARR_FEATURE_VIDEO_UNPROVEN")
        self.assertEqual(plan.error_details["selected_video"], "/p3/download/Moana.2.2024/Sample.mkv")

    def test_radarr_missing_media_restore_force_rechecks_and_hardlinks_subtitles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download_root = root / "download"
            release = download_root / "Moana.2.2024.REMUX"
            movie = release / "Moana.2.2024.REMUX.mkv"
            subtitle = release / "Moana.2.2024.REMUX.sv.srt"
            release.mkdir(parents=True)
            movie.write_bytes(b"verified movie")
            subtitle.write_bytes(b"verified subtitle")
            movie_root = root / "movies"
            target_root = movie_root / "Moana 2 (2024)"
            target_movie = target_root / movie.name
            target_subtitle = target_root / subtitle.name
            item = {
                "id": 66,
                "title": "Moana 2",
                "path": str(target_root),
            }
            initial_mapping = {
                "app": "radarr", "item": item, "files": [],
            }
            refreshed_mapping = {
                "app": "radarr",
                "item": item,
                "files": [{
                    "id": 700,
                    "path": str(target_movie),
                    "relativePath": target_movie.name,
                    "size": movie.stat().st_size,
                    "episodeIds": [],
                }],
            }
            plan = Plan(
                "MOANA", "Moana.2.2024.REMUX", "radarr", "p3", 66,
                "Moana 2", str(target_root), str(target_root),
                [FilePair(
                    str(target_movie), str(target_movie), str(movie),
                    movie.stat().st_size, "missing-library", "hardlink",
                )],
                "ready",
                auxiliary_files=[AuxiliaryFile(
                    str(subtitle), str(target_subtitle),
                    subtitle.stat().st_size, "torrent-sidecar",
                    "qbittorrent", "hardlink", "subtitle",
                )],
                managed_files=[{
                    "id": None,
                    "path": str(target_movie),
                    "relativePath": target_movie.name,
                    "size": movie.stat().st_size,
                    "episodeIds": [],
                    "plannedRestore": True,
                }],
            )
            rescanned = False

            def rescan(item_id):
                nonlocal rescanned
                rescanned = True

            client = SimpleNamespace(
                download_mapping=lambda torrent_hash: (
                    refreshed_mapping if rescanned else initial_mapping
                ),
                sync_pool=Mock(),
                rescan=Mock(side_effect=rescan),
            )
            pool = Pool(
                "p3", root, (download_root,), movie_root, root / "series",
                "radarr-pool3", "sonarr-pool3",
                "radarr-pool3", "sonarr-pool3",
            )
            manager = Stowarr.__new__(Stowarr)
            manager.arr = {"radarr": client}
            manager.config = SimpleNamespace(
                apply=True,
                pools=(pool,),
                pool_for_path=lambda path: pool,
                pool_for_category=lambda category: (pool, "radarr"),
            )
            manager.store = SimpleNamespace(update=Mock())
            manager.qbit = SimpleNamespace(
                recheck=Mock(),
                torrent=lambda torrent_hash: {
                    "hash": torrent_hash,
                    "save_path": str(download_root),
                    "category": "radarr-pool3",
                },
                files=lambda torrent_hash: [
                    {
                        "name": str(movie.relative_to(download_root)),
                        "size": movie.stat().st_size,
                        "priority": 1,
                    },
                    {
                        "name": str(subtitle.relative_to(download_root)),
                        "size": subtitle.stat().st_size,
                        "priority": 1,
                    },
                ],
            )
            manager._wait_for_recheck = Mock(return_value={})
            manager._wait_for_visible_torrent_files = Mock(return_value=[])

            result = manager.reconcile(
                "MOANA", {str(subtitle)}, operation_id=9,
                mapping_hint=initial_mapping, prepared_plan=plan,
            )

            self.assertEqual(result["state"], "COMPLETE")
            manager.qbit.recheck.assert_called_once_with("MOANA")
            manager._wait_for_recheck.assert_called_once()
            client.rescan.assert_called_once_with(66)
            self.assertEqual(
                (target_movie.stat().st_dev, target_movie.stat().st_ino),
                (movie.stat().st_dev, movie.stat().st_ino),
            )
            self.assertEqual(
                (target_subtitle.stat().st_dev, target_subtitle.stat().st_ino),
                (subtitle.stat().st_dev, subtitle.stat().st_ino),
            )
            self.assertTrue(movie.exists())
            self.assertTrue(subtitle.exists())

    def test_sync_audit_reports_duplicate_and_unrouted_history_torrents(self):
        p1 = Pool(
            "p1", Path("/p1"), (Path("/p1/download"),),
            Path("/p1/movies"), Path("/p1/series"),
            "radarr-pool1", "sonarr-pool1", "radarr-pool1", "sonarr-pool1",
        )
        p3 = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3", "radarr-pool3", "sonarr-pool3",
        )
        torrents = [
            {"hash": "A", "name": "Beast.A", "category": "radarr-pool3", "save_path": "/p3/download/A"},
            {"hash": "B", "name": "Beast.B", "category": "radarr-pool3", "save_path": "/p3/download/B"},
            {"hash": "C", "name": "How.To.Make.A.Killing", "category": "", "save_path": "/p3/download/C"},
        ]
        items = [
            {"id": 42, "title": "Beast", "path": "/p1/movies/Beast (2026)"},
            {"id": 43, "title": "How to Make a Killing", "path": "/p1/movies/How to Make a Killing (2026)"},
        ]
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: torrents,
            categories=lambda: {
                "radarr-pool3": {"savePath": "/p3/download"},
            },
        )
        manager.config = SimpleNamespace(
            pools=(p1, p3),
            pool_for_path=lambda path: p3 if str(path).startswith("/p3") else p1,
            pool_for_category=lambda category: (
                (p3, "radarr") if category == "radarr-pool3" else None
            ),
        )
        manager.arr = {"radarr": SimpleNamespace(
            history_for_downloads=lambda hashes: {"a": 42, "b": 42, "c": 43},
            all_items=lambda: items,
        )}

        audit = manager.sync_audit("radarr")
        by_hash = {row["hash"]: row for row in audit["rows"]}

        self.assertEqual(audit["scanned"], 3)
        self.assertEqual(by_hash["A"]["status"], "multiple-torrents")
        self.assertEqual(by_hash["B"]["status"], "multiple-torrents")
        self.assertFalse(by_hash["A"]["safe_plan_candidate"])
        self.assertFalse(by_hash["B"]["safe_plan_candidate"])
        self.assertEqual(len(by_hash["A"]["related_torrents"]), 2)
        self.assertEqual(by_hash["C"]["status"], "category-unconfigured")
        self.assertEqual(by_hash["C"]["expected_category"], "radarr-pool3")
        self.assertTrue(by_hash["C"]["category_repairable"])
        self.assertTrue(by_hash["C"]["safe_plan_candidate"])

    def test_radarr_sync_audit_finds_missing_media_and_missing_hardlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download_root = root / "download"
            movie_root = root / "movies"
            download_root.mkdir()
            movie_root.mkdir()
            missing_download = download_root / "Missing.2024.mkv"
            copied_download = download_root / "Copied.2024.mkv"
            linked_download = download_root / "Linked.2024.mkv"
            for path in (missing_download, copied_download, linked_download):
                path.write_bytes(path.stem.encode())
            copied_root = movie_root / "Copied (2024)"
            linked_root = movie_root / "Linked (2024)"
            copied_root.mkdir()
            linked_root.mkdir()
            copied_library = copied_root / copied_download.name
            copied_library.write_bytes(copied_download.read_bytes())
            linked_library = linked_root / linked_download.name
            os.link(linked_download, linked_library)
            pool = Pool(
                "p3", root, (download_root,), movie_root, root / "series",
                "radarr-pool3", "sonarr-pool3",
                "radarr-pool3", "sonarr-pool3",
            )
            torrents = [
                {
                    "hash": name.upper(), "name": f"{name}.2024",
                    "category": "radarr-pool3",
                    "save_path": str(download_root),
                }
                for name in ("missing", "copied", "linked")
            ]
            torrent_paths = {
                "MISSING": missing_download,
                "COPIED": copied_download,
                "LINKED": linked_download,
            }
            items = [
                {
                    "id": 1, "title": "Missing",
                    "path": str(movie_root / "Missing (2024)"),
                },
                {
                    "id": 2, "title": "Copied", "path": str(copied_root),
                    "movieFile": {
                        "id": 20, "path": str(copied_library),
                        "relativePath": copied_library.name,
                        "size": copied_library.stat().st_size,
                    },
                },
                {
                    "id": 3, "title": "Linked", "path": str(linked_root),
                    "movieFile": {
                        "id": 30, "path": str(linked_library),
                        "relativePath": linked_library.name,
                        "size": linked_library.stat().st_size,
                    },
                },
            ]
            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(
                torrents=lambda: torrents,
                categories=lambda: {
                    "radarr-pool3": {"savePath": str(download_root)},
                },
                files=lambda torrent_hash: [{
                    "name": torrent_paths[torrent_hash.upper()].name,
                    "size": torrent_paths[torrent_hash.upper()].stat().st_size,
                    "priority": 1,
                }],
            )
            manager.config = SimpleNamespace(
                pools=(pool,),
                pool_for_path=lambda path: pool,
                pool_for_category=lambda category: (pool, "radarr"),
            )
            manager.arr = {"radarr": SimpleNamespace(
                history_for_downloads=lambda hashes: {
                    "missing": 1, "copied": 2, "linked": 3,
                },
                all_items=lambda: items,
            )}

            audit = manager.sync_audit("radarr")
            by_hash = {row["hash"]: row for row in audit["rows"]}

            self.assertEqual(
                by_hash["MISSING"]["status"], "missing-library-file"
            )
            self.assertTrue(by_hash["MISSING"]["safe_plan_candidate"])
            self.assertEqual(
                by_hash["COPIED"]["status"], "hardlink-missing"
            )
            self.assertTrue(by_hash["COPIED"]["safe_plan_candidate"])
            self.assertEqual(by_hash["LINKED"]["status"], "in-sync")
            self.assertFalse(by_hash["LINKED"]["safe_plan_candidate"])

    def test_radarr_sync_audit_does_not_offer_hardlink_repair_for_packed_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            download_root = root / "download"
            release = download_root / "Click.2006.2160p"
            archive = release / "click.rar"
            archive.parent.mkdir(parents=True)
            archive.write_bytes(b"packed movie")
            movie_root = root / "movies"
            library = movie_root / "Click (2006)" / "Click.2006.mkv"
            library.parent.mkdir(parents=True)
            library.write_bytes(b"derived movie")
            pool = Pool(
                "p3", root, (download_root,), movie_root, root / "series",
                "radarr-pool3", "sonarr-pool3",
                "radarr-pool3", "sonarr-pool3",
            )
            torrent = {
                "hash": "CLICK", "name": "Click.2006.2160p",
                "category": "radarr-pool3", "save_path": str(download_root),
            }
            item = {
                "id": 6, "title": "Click", "year": 2006,
                "path": str(library.parent),
                "movieFile": {
                    "id": 60, "path": str(library),
                    "relativePath": library.name,
                    "size": library.stat().st_size,
                },
            }
            manager = Stowarr.__new__(Stowarr)
            manager.qbit = SimpleNamespace(
                torrents=lambda: [torrent],
                categories=lambda: {
                    "radarr-pool3": {"savePath": str(download_root)},
                },
                files=lambda torrent_hash: [{
                    "name": str(archive.relative_to(download_root)),
                    "size": archive.stat().st_size,
                    "priority": 1,
                }],
            )
            manager.config = SimpleNamespace(
                pools=(pool,),
                pool_for_path=lambda path: pool,
                pool_for_category=lambda category: (pool, "radarr"),
            )
            manager.arr = {"radarr": SimpleNamespace(
                history_for_downloads=lambda hashes: {"click": 6},
                all_items=lambda: [item],
            )}

            row = manager.sync_audit("radarr")["rows"][0]

            self.assertEqual(row["status"], "packed-media")
            self.assertFalse(row["safe_plan_candidate"])
            self.assertEqual(
                row["issues"][0]["code"],
                "PACKED_MEDIA_HARDLINK_NOT_APPLICABLE",
            )

    def test_radarr_sync_audit_blocks_a_different_release_folder_from_safe_repair(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3",
            "radarr-pool3", "sonarr-pool3",
        )
        torrent = {
            "hash": "KILLING",
            "name": "How.To.Make.A.Killing.2026.2160p.REMUX-CiNEPHiLES",
            "category": "radarr-pool3",
            "save_path": "/p3/download/How.To.Make.A.Killing.2026",
        }
        item = {
            "id": 42, "title": "How to Make a Killing", "year": 2026,
            "path": (
                "/p3/movies/How.to.Make.a.Killing.2026.NORDiC.2160p."
                "WEB-DL.DDP5.1-CiUHD"
            ),
            "movieFile": {
                "id": 420,
                "relativePath": "How.to.Make.a.Killing.2026.mkv",
                "size": 100,
            },
        }
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(
            torrents=lambda: [torrent],
            categories=lambda: {
                "radarr-pool3": {"savePath": "/p3/download"},
            },
        )
        manager.config = SimpleNamespace(
            pools=(pool,),
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: (pool, "radarr"),
        )
        manager.arr = {"radarr": SimpleNamespace(
            history_for_downloads=lambda hashes: {"killing": 42},
            all_items=lambda: [item],
        )}

        row = manager.sync_audit("radarr")["rows"][0]

        self.assertEqual(row["status"], "library-folder-mismatch")
        self.assertFalse(row["safe_plan_candidate"])
        self.assertEqual(
            row["issues"][0]["code"], "RADARR_RELEASE_FOLDER_MISMATCH"
        )

    def test_sonarr_roots_preserve_anime_family_between_pools(self):
        p1 = Pool(
            "p1", Path("/p1"), (Path("/p1/download"),),
            Path("/p1/movies"), Path("/p1/series"),
            "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
        )
        p3 = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-p3", "sonarr-p3", "radarr-p3", "sonarr-p3",
        )
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(
            pools=(p1, p3),
            pool_for_path=lambda path: p1 if str(path).startswith("/p1/") else p3,
        )
        manager.arr = {"sonarr": SimpleNamespace(root_folders=lambda: [
            {"path": "/p1/anime"},
            {"path": "/p1/series"},
            {"path": "/p3/anime"},
            {"path": "/p3/series"},
        ])}

        target = manager._target_item_path(
            {"path": "/p3/anime/Dr. STONE"}, p1, "sonarr",
        )

        self.assertEqual(target, Path("/p1/anime/Dr. STONE"))
        self.assertEqual(
            manager._library_root_for_path("sonarr", p3, "/p3/anime/Dr. STONE"),
            Path("/p3/anime"),
        )

    def test_write_mode_validates_and_reports_discovered_sonarr_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "p1"
            download = prefix / "download"
            movies = prefix / "movies"
            series = prefix / "series"
            anime = prefix / "anime"
            for path in (download, movies, series, anime):
                path.mkdir(parents=True, exist_ok=True)
            pool = Pool(
                "p1", prefix, (download,), movies, series,
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            manager = Stowarr.__new__(Stowarr)
            manager.config = Config(
                pools=(pool,),
                qbittorrent=Service(""),
                radarr=Service(""),
                sonarr=Service(""),
                database=Path(directory) / "state.db",
                apply=False,
                listen="127.0.0.1",
                port=8787,
                api_token="",
                api_only=False,
                auth_method="forms",
                external_user_header="X-Forwarded-User",
            )
            manager.arr = {"sonarr": SimpleNamespace(root_folders=lambda: [
                {"path": str(anime)},
                {"path": str(series)},
            ])}
            manager.store = SimpleNamespace(set_setting=lambda *args: None)

            report = manager.runtime_settings()
            reported_paths = {
                item["path"]
                for item in report["deployment"]["pool_mounts"][0]["paths"]
            }

            self.assertIn(str(anime), reported_paths)
            original_write_bytes = Path.write_bytes

            def reject_anime(path, data):
                if path.parent == anime:
                    raise PermissionError("read-only anime root")
                return original_write_bytes(path, data)

            with patch.object(Path, "write_bytes", reject_anime):
                with self.assertRaisesRegex(
                    PermissionError, "Required media path is not writable"
                ):
                    manager.update_runtime_settings({"apply": True})
            self.assertFalse(manager.config.apply)

    def test_write_mode_rejects_missing_sonarr_discovery(self):
        pool = Pool(
            "p1", Path("/p1"), (Path("/p1/download"),),
            Path("/p1/movies"), Path("/p1/series"),
            "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
        )
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(pools=(pool,), apply=False)
        manager.arr = {}
        manager.store = SimpleNamespace(set_setting=lambda *args: None)

        with self.assertRaisesRegex(
            RuntimeError, "Sonarr must be configured"
        ):
            manager.update_runtime_settings({"apply": True})

        self.assertFalse(manager.config.apply)

    def test_connection_update_rolls_back_when_new_sonarr_roots_are_not_writable(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "p1"
            download = prefix / "download"
            movies = prefix / "movies"
            series = prefix / "series"
            anime = prefix / "anime"
            for path in (download, movies, series, anime):
                path.mkdir(parents=True, exist_ok=True)
            pool = Pool(
                "p1", prefix, (download,), movies, series,
                "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
            )
            service = Service("http://old", api_key="key")
            manager = Stowarr.__new__(Stowarr)
            manager.config = Config(
                pools=(pool,),
                qbittorrent=service,
                radarr=service,
                sonarr=service,
                database=Path(directory) / "state.db",
                apply=True,
                listen="127.0.0.1",
                port=8787,
                api_token="",
                api_only=False,
                auth_method="forms",
                external_user_header="X-Forwarded-User",
            )
            previous_qbit = object()
            previous_arr = {"radarr": object(), "sonarr": object()}
            manager.qbit = previous_qbit
            manager.arr = previous_arr
            manager.connection_error = None
            manager.store = Mock()

            def activate(*args, **kwargs):
                manager.qbit = object()
                manager.arr = {
                    "radarr": object(),
                    "sonarr": SimpleNamespace(root_folders=lambda: [
                        {"path": str(series)},
                        {"path": str(anime)},
                    ]),
                }
                return {"qbittorrent": "connected", "sonarr": "connected"}

            original_write_bytes = Path.write_bytes

            def reject_anime(path, data):
                if path.parent == anime:
                    raise PermissionError("read-only anime root")
                return original_write_bytes(path, data)

            payload = {"services": {
                "qbittorrent": {"url": "http://new-qbit", "api_key": "key"},
                "radarr": {"url": "http://new-radarr", "api_key": "key"},
                "sonarr": {"url": "http://new-sonarr", "api_key": "key"},
            }}
            with (
                patch.object(manager, "_activate_connections", side_effect=activate),
                patch.object(Path, "write_bytes", reject_anime),
                self.assertRaisesRegex(
                    PermissionError, "Required media path is not writable"
                ),
            ):
                manager.update_connections(payload)

            self.assertIs(manager.qbit, previous_qbit)
            self.assertIs(manager.arr, previous_arr)
            manager.store.set_setting.assert_not_called()

    def test_startup_disables_persisted_write_mode_when_roots_cannot_be_validated(self):
        pool = Pool(
            "p1", Path("/p1"), (Path("/p1/download"),),
            Path("/p1/movies"), Path("/p1/series"),
            "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
        )
        config = Config(
            pools=(pool,),
            qbittorrent=Service(""),
            radarr=Service(""),
            sonarr=Service(""),
            database=Path("/state/test.db"),
            apply=True,
            listen="127.0.0.1",
            port=8787,
            api_token="existing-token",
            api_only=False,
            auth_method="forms",
            external_user_header="X-Forwarded-User",
        )
        store = Mock()
        store.setting.return_value = None

        with (
            patch("stowarr.engine.Store", return_value=store),
            patch("stowarr.engine.AuthManager"),
            patch.object(Stowarr, "_activate_connections", return_value={}),
        ):
            manager = Stowarr(config)

        self.assertFalse(manager.config.apply)
        self.assertIn("Write mode was disabled", manager.connection_error)
        store.set_setting.assert_called_once_with("runtime", {"apply": False})

    def test_startup_preserves_write_mode_when_recovery_defers_validation(self):
        pool = Pool(
            "p1", Path("/p1"), (Path("/p1/download"),),
            Path("/p1/movies"), Path("/p1/series"),
            "radarr-p1", "sonarr-p1", "radarr-p1", "sonarr-p1",
        )
        config = Config(
            pools=(pool,),
            qbittorrent=Service(""),
            radarr=Service(""),
            sonarr=Service(""),
            database=Path("/state/test.db"),
            apply=True,
            listen="127.0.0.1",
            port=8787,
            api_token="existing-token",
            api_only=False,
            auth_method="forms",
            external_user_header="X-Forwarded-User",
        )
        store = Mock()
        store.setting.return_value = None
        store.has_recovery_required.return_value = True

        with (
            patch("stowarr.engine.Store", return_value=store),
            patch("stowarr.engine.AuthManager"),
            patch.object(Stowarr, "_activate_connections", return_value={}),
            patch.object(Stowarr, "_validate_write_paths") as validate,
        ):
            manager = Stowarr(config)

        self.assertTrue(manager.config.apply)
        self.assertFalse(manager._write_paths_validated)
        self.assertIn("deferred until Recovery", manager.connection_error)
        validate.assert_not_called()
        store.set_setting.assert_not_called()

    def test_deferred_write_path_validation_resumes_after_recovery(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager.store = Mock()
        manager.store.has_recovery_required.return_value = False
        manager._write_paths_validated = False
        manager.connection_error = (
            "Write-path validation is deferred until Recovery is resolved"
        )

        with patch.object(manager, "_validate_write_paths") as validate:
            self.assertTrue(manager._ensure_write_paths_validated())

        validate.assert_called_once_with()
        self.assertTrue(manager._write_paths_validated)
        self.assertIsNone(manager.connection_error)

    def test_sonarr_audit_accepts_anime_root_and_multiple_episode_torrents(self):
        p3 = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-p3", "sonarr-p3", "radarr-p3", "sonarr-p3",
        )
        torrents = [
            {
                "hash": "EP1", "name": "Dr.Stone.S04E01",
                "category": "sonarr-p3", "save_path": "/p3/download/Dr.Stone.S04E01",
            },
            {
                "hash": "EP2", "name": "Dr.Stone.S04E02",
                "category": "sonarr-p3", "save_path": "/p3/download/Dr.Stone.S04E02",
            },
        ]
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(
            pools=(p3,),
            pool_for_path=lambda path: p3,
            pool_for_category=lambda category: (
                (p3, "sonarr") if category == "sonarr-p3" else None
            ),
        )
        manager.qbit = SimpleNamespace(
            torrents=lambda: torrents,
            categories=lambda: {"sonarr-p3": {"savePath": "/p3/download"}},
        )
        manager.arr = {"sonarr": SimpleNamespace(
            root_folders=lambda: [
                {"path": "/p3/anime"},
                {"path": "/p3/series"},
            ],
            history_for_downloads=lambda hashes: {"ep1": 42, "ep2": 42},
            all_items=lambda: [{
                "id": 42, "title": "Dr. STONE",
                "path": "/p3/anime/Dr. STONE", "seriesType": "anime",
            }],
        )}

        audit = manager.sync_audit("sonarr")

        self.assertEqual(audit["in_sync"], 2)
        self.assertEqual(audit["issues"], 0)
        self.assertEqual({row["status"] for row in audit["rows"]}, {"in-sync"})
        self.assertEqual(
            {row["expected_root"] for row in audit["rows"]},
            {"/p3/anime"},
        )

    def test_sync_category_repair_revalidates_route_and_history(self):
        pool = Pool(
            "p3", Path("/p3"), (Path("/p3/download"),),
            Path("/p3/movies"), Path("/p3/series"),
            "radarr-pool3", "sonarr-pool3", "radarr-pool3", "sonarr-pool3",
        )
        changed = []
        security_events = []
        torrent = {
            "hash": "ABC123",
            "name": "Example",
            "category": "radarr",
            "save_path": "/p3/download/Example",
        }
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(
            apply=True,
            pool_for_path=lambda path: pool,
            pool_for_category=lambda category: None,
        )
        manager.qbit = SimpleNamespace(
            torrent=lambda torrent_hash: torrent,
            categories=lambda: {
                "radarr-pool3": {"savePath": "/p3/download"},
            },
            set_category=lambda torrent_hash, category: changed.append(
                (torrent_hash, category)
            ),
        )
        manager.arr = {"radarr": SimpleNamespace(
            history_for_downloads=lambda hashes: {"abc123": 42},
        )}
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: False,
            security_event=lambda *args: security_events.append(args),
            record=Mock(return_value=17),
            update=Mock(),
        )
        manager._move_lock = threading.RLock()

        result = manager.repair_sync_category("radarr", "ABC123")

        self.assertTrue(result["changed"])
        self.assertEqual(result["category"], "radarr-pool3")
        self.assertEqual(result["operation_id"], 17)
        self.assertEqual(changed, [("ABC123", "radarr-pool3")])
        self.assertEqual(security_events[0][0], "sync-category-repaired")
        self.assertEqual(manager.store.record.call_args.kwargs["kind"], "category")
        self.assertEqual(manager.store.update.call_args.args[1], "COMPLETE")

    def test_sync_category_repair_is_rejected_while_recovery_is_required(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager.arr = {"radarr": SimpleNamespace()}
        manager.qbit = Mock()
        manager.store = SimpleNamespace(
            has_recovery_required=lambda: True,
            has_active_queue_work=lambda: False,
        )
        manager._move_lock = threading.RLock()

        with self.assertRaisesRegex(RuntimeError, "Recovery"):
            manager.repair_sync_category("radarr", "ABC123")

        manager.qbit.torrent.assert_not_called()
        manager.qbit.set_category.assert_not_called()

    def test_safe_sync_plan_only_promotes_fresh_ready_reconciles(self):
        rows = [
            {
                "hash": "CATEGORY", "torrent_name": "Category Example",
                "item_title": "Category Example", "status": "category-unconfigured",
                "category_repairable": True, "qbit_pool": "p3", "category": "",
                "expected_category": "radarr-pool3", "reason": "unconfigured",
                "issues": [],
            },
            {
                "hash": "READY", "torrent_name": "Ready Example",
                "item_title": "Ready Example", "status": "root-mismatch",
                "reason": "wrong root", "issues": [],
            },
            {
                "hash": "BLOCKED", "torrent_name": "Blocked Example",
                "item_title": "Blocked Example", "status": "root-mismatch",
                "reason": "wrong root", "issues": [],
            },
            {
                "hash": "DUPLICATE", "torrent_name": "Duplicate Example",
                "item_title": "Duplicate Example", "status": "multiple-torrents",
                "reason": "ambiguous", "issues": [{"code": "DUPLICATE"}],
            },
        ]
        ready = Plan(
            "READY", "Ready Example", "radarr", "p3", 1, "Ready Example",
            "/p1/movies/Ready", "/p3/movies/Ready", [], "ready",
            auxiliary_files=[
                AuxiliaryFile(
                    "/p1/movies/Ready/subtitle.srt",
                    "/p3/movies/Ready/subtitle.srt",
                    10, "missing-target", "library", "copy", "subtitle",
                ),
                AuxiliaryFile(
                    "/p1/movies/Ready/conflict.jpg",
                    "/p3/movies/Ready/conflict.jpg",
                    10, "target-conflict", "library", "copy", "artwork",
                ),
            ],
        )
        blocked = Plan(
            "BLOCKED", "Blocked Example", "radarr", "p3", 2,
            "Blocked Example", "/p1/movies/Blocked", "/p3/movies/Blocked",
            [], "blocked", "Ambiguous mapping", "AMBIGUOUS",
        )
        manager = Stowarr.__new__(Stowarr)
        manager.sync_audit = Mock(return_value={
            "app": "radarr", "scanned": 4, "issues": 4, "rows": rows,
        })
        manager.plan = Mock(side_effect=lambda value: ready if value == "READY" else blocked)
        manager.store = SimpleNamespace(reconcile_queue=lambda: [])

        progress = []
        result = manager.safe_sync_plan("radarr", progress.append)

        self.assertEqual(result["safe_count"], 2)
        self.assertEqual(
            result["category_repairs"][0]["hash"], "CATEGORY"
        )
        self.assertEqual(
            result["reconcile_candidates"][0]["auxiliary_files"],
            ["/p1/movies/Ready/subtitle.srt"],
        )
        self.assertEqual(
            {item["hash"] for item in result["manual"]},
            {"BLOCKED", "DUPLICATE"},
        )
        self.assertEqual(
            {event["stage"] for event in progress},
            {"audit", "categories", "reconciles", "manual"},
        )
        self.assertEqual(
            progress[-2]["current"], progress[-2]["total"]
        )
        self.assertEqual(progress[-1]["stage"], "manual")

    def test_safe_category_batch_validates_every_item_before_mutating(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager._require_write_ready = Mock()
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: False,
            consume_confirmation=Mock(),
            record=Mock(return_value=23),
            update=Mock(),
        )
        manager._safe_category_selection = Mock(return_value={
            "app": "radarr",
            "category_repairs": [
                {"hash": "FIRST"},
                {"hash": "SECOND"},
            ],
        })
        manager._sync_category_repair_context = Mock(side_effect=[
            {
                "app": "radarr", "hash": "FIRST", "pool": "p3",
                "previous_category": "", "category": "radarr-pool3",
                "changed": True,
            },
            RuntimeError("Second item is no longer safe"),
        ])
        manager._apply_sync_category_context = Mock()

        with self.assertRaisesRegex(RuntimeError, "no longer safe"):
            manager.apply_safe_category_repairs(
                "token", "radarr", ["FIRST", "SECOND"]
            )

        manager._apply_sync_category_context.assert_not_called()

    def test_safe_category_batch_reports_real_validation_and_apply_progress(self):
        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(apply=True)
        manager._require_write_ready = Mock()
        manager._move_lock = threading.RLock()
        manager.store = SimpleNamespace(
            has_active_queue_work=lambda: False,
            consume_confirmation=Mock(),
            record=Mock(return_value=23),
            update=Mock(),
        )
        repairs = [
            {"hash": "FIRST", "torrent_name": "First"},
            {"hash": "SECOND", "torrent_name": "Second"},
        ]
        manager._safe_category_selection = Mock(return_value={
            "app": "radarr", "category_repairs": repairs,
        })
        contexts = [
            {
                "app": "radarr", "hash": item["hash"], "pool": "p3",
                "torrent_name": item["torrent_name"],
                "previous_category": "", "category": "radarr-pool3",
                "changed": True,
            }
            for item in repairs
        ]
        manager._sync_category_repair_context = Mock(side_effect=contexts)
        manager._apply_sync_category_context = Mock(
            side_effect=lambda context: context
        )
        progress = []

        result = manager.apply_safe_category_repairs(
            "token", "radarr", ["FIRST", "SECOND"], progress.append
        )

        self.assertEqual(result["changed"], 2)
        self.assertEqual(result["operation_id"], 23)
        manager.store.record.assert_called_once()
        self.assertEqual(manager.store.record.call_args.kwargs["kind"], "category")
        self.assertEqual(manager.store.update.call_args.args[1], "COMPLETE")
        self.assertEqual(
            [(event["stage"], event["current"], event["total"]) for event in progress],
            [
                ("validation", 0, 2),
                ("validation", 1, 2),
                ("validation", 2, 2),
                ("apply", 0, 2),
                ("apply", 1, 2),
                ("apply", 2, 2),
            ],
        )

    def test_category_batch_failure_after_write_requires_recovery(self):
        manager = Stowarr.__new__(Stowarr)
        manager.store = SimpleNamespace(
            record=Mock(return_value=31),
            update=Mock(),
        )
        contexts = [
            {
                "app": "radarr", "hash": "FIRST", "torrent_name": "First",
                "pool": "p3", "previous_category": "",
                "category": "radarr-pool3", "changed": True,
            },
            {
                "app": "radarr", "hash": "SECOND", "torrent_name": "Second",
                "pool": "p3", "previous_category": "",
                "category": "radarr-pool3", "changed": True,
            },
        ]
        manager._apply_sync_category_context = Mock(side_effect=[
            contexts[0],
            RuntimeError("qBittorrent connection was lost"),
        ])

        with self.assertRaisesRegex(RuntimeError, "connection was lost"):
            manager._apply_recorded_category_contexts("radarr", contexts)

        self.assertEqual(manager.store.update.call_args.args[1], "RECOVERY_REQUIRED")
        failure = manager.store.update.call_args.args[2]
        self.assertTrue(failure["recovery"]["required"])
        self.assertEqual(failure["failed_after"], "CATEGORY_APPLYING")

    def test_category_recovery_diagnosis_checks_every_selected_hash(self):
        operation = {
            "id": 8,
            "public_id": "C4T3G",
            "torrent_hash": "first",
            "app": "sonarr",
            "kind": "category",
            "state": "RECOVERY_REQUIRED",
            "detail": {
                "category_repairs": [
                    {"hash": "first", "category": "sonarr-pool3"},
                    {"hash": "second", "category": "sonarr-pool3"},
                ],
                "recovery": {"previous_state": "CATEGORY_APPLYING"},
            },
        }
        manager = Stowarr.__new__(Stowarr)
        manager.store = SimpleNamespace(
            operation_by_public_id=lambda public_id: operation
        )
        manager.qbit = SimpleNamespace(
            torrent=lambda torrent_hash: {
                "hash": torrent_hash,
                "category": "sonarr-pool3",
            }
        )

        result = manager.diagnose_recovery("C4T3G")

        diagnosis = result["diagnosis"]
        self.assertTrue(diagnosis["read_only"])
        self.assertEqual(diagnosis["qbittorrent"]["matching"], 2)
        self.assertEqual(
            diagnosis["recommendation"]["code"],
            "CATEGORY_BATCH_APPEARS_COMPLETE",
        )

    def test_qbittorrent_search_does_not_consult_arr(self):
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(torrents=lambda: [
            {"hash": "ABC123", "name": "Example Movie", "category": "radarr-p1", "save_path": "/p1/download", "state": "uploading", "progress": 1, "total_size": 42},
            {"hash": "DEF456", "name": "Different Series", "category": "sonarr-p3", "save_path": "/p3/download", "state": "pausedUP", "progress": 1, "total_size": 84},
        ])
        manager.config = SimpleNamespace(pool_for_path=lambda path: SimpleNamespace(name="p1") if path.startswith("/p1") else SimpleNamespace(name="p3"))
        manager.arr = SimpleNamespace()

        result = manager.qbit_search("example")

        self.assertEqual(result["matches"], 1)
        self.assertEqual(result["rows"][0]["hash"], "ABC123")
        self.assertEqual(result["rows"][0]["pool"], "p1")

    def test_qbittorrent_search_ranks_title_before_incidental_hash_match(self):
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(torrents=lambda: [
            {"hash": "2012abcdef", "name": "Unrelated", "category": "", "save_path": "/p1", "progress": 1},
            {"hash": "abcdef", "name": "2012 Movie", "category": "", "save_path": "/p1", "progress": 1},
        ])
        manager.config = SimpleNamespace(pool_for_path=lambda path: SimpleNamespace(name="p1"))
        self.assertEqual(manager.qbit_search("2012")["rows"][0]["name"], "2012 Movie")

    def test_qbittorrent_catalog_groups_by_pool_and_exact_save_path(self):
        p1 = SimpleNamespace(name="p1", prefix=Path("/p1"), download_roots=(Path("/p1/download"),), radarr_category="radarr-p1", sonarr_category="sonarr-p1", radarr_tag="radarr-p1", sonarr_tag="sonarr-p1", radarr_root=Path("/p1/movies"), sonarr_root=Path("/p1/series"))
        p3 = SimpleNamespace(name="p3", prefix=Path("/p3"), download_roots=(Path("/p3/download"),), radarr_category="radarr-p3", sonarr_category="sonarr-p3", radarr_tag="radarr-p3", sonarr_tag="sonarr-p3", radarr_root=Path("/p3/movies"), sonarr_root=Path("/p3/series"))
        manager = Stowarr.__new__(Stowarr)
        manager.qbit = SimpleNamespace(torrents=lambda: [
            {"hash": "A", "name": "Movie", "category": "radarr-p1", "save_path": "/p1/download", "progress": 1},
            {"hash": "B", "name": "Series", "save_path": "/p3/download/tv", "progress": 1},
            {"hash": "C", "name": "Legacy", "save_path": "/other", "progress": 1},
            {"hash": "D", "name": "Manual season", "save_path": "/p3/series/Show/Season 01", "progress": 1},
        ])
        manager.config = SimpleNamespace(
            pools=(p1, p3),
            pool_for_path=lambda path: p1 if path.startswith("/p1") else p3 if path.startswith("/p3") else None,
        )

        result = manager.qbit_catalog()

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["routes"][0]["count"], 1)
        self.assertEqual(result["routes"][0]["paths"][0]["torrents"][0]["route_status"], "aligned")
        self.assertEqual([group["pool"] for group in result["unmanaged"]], ["p3", None])
        self.assertEqual(result["unmanaged"][0]["paths"][0]["path"], "/p3/download/tv")
        self.assertEqual(result["unmanaged"][0]["paths"][0]["route"], "download")
        self.assertEqual(result["library_seeded"][0]["app"], "sonarr")
        self.assertEqual(result["library_seeded"][0]["paths"][0]["route"], "library")
        self.assertEqual(result["library_seeded"][0]["paths"][0]["torrents"][0]["route_status"], "library-seeded")

    def test_routing_audit_distinguishes_category_route_from_tag_restriction(self):
        pool = SimpleNamespace(
            name="p1", prefix=Path("/p1"), download_roots=(Path("/p1/download"),),
            radarr_category="radarr-p1", sonarr_category="sonarr-p1",
            radarr_tag="radarr-p1", sonarr_tag="sonarr-p1",
            radarr_root=Path("/p1/movies"), sonarr_root=Path("/p1/series"),
        )

        def arr_client(app):
            category_field = "movieCategory" if app == "radarr" else "tvCategory"
            category = f"{app}-p1"
            return SimpleNamespace(
                tags=lambda: [{"id": 7, "label": category}],
                root_folders=lambda: [{"path": f"/p1/{'movies' if app == 'radarr' else 'series'}"}],
                download_clients=lambda: [{
                    "id": 3, "name": "qBittorrent p1", "enable": True, "tags": [],
                    "fields": [{"name": category_field, "value": category}],
                }],
            )

        manager = Stowarr.__new__(Stowarr)
        manager.config = SimpleNamespace(pools=(pool,))
        manager.qbit = SimpleNamespace(categories=lambda: {
            "radarr-p1": {"savePath": "/p1/download"},
            "sonarr-p1": {"savePath": "/p1/download"},
        })
        manager.arr = {"radarr": arr_client("radarr"), "sonarr": arr_client("sonarr")}

        result = manager.routing_audit()

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["issue_count"], 2)
        self.assertIn("not restricted by tag", result["services"][0]["routes"][0]["issues"][0])
