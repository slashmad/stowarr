import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from stowarr.archive import ArchiveMember, ExtractedFile
from stowarr.config import Pool
from stowarr.engine import MovePlan, Plan, Stowarr, is_archive, sha256, title_matches


class EngineTest(unittest.TestCase):
    def test_move_requires_a_completed_upload_state_before_success(self):
        self.assertTrue(Stowarr._is_seeding_state({"state": "stalledUP", "progress": 1}))
        self.assertTrue(Stowarr._is_seeding_state({"state": "uploading", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "pausedUP", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "stoppedUP", "progress": 1}))
        self.assertFalse(Stowarr._is_seeding_state({"state": "stalledUP", "progress": 0.9}))

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

    def test_archive_detection_covers_multi_part_releases(self):
        self.assertTrue(is_archive(Path("movie.rar")))
        self.assertTrue(is_archive(Path("movie.r00")))
        self.assertTrue(is_archive(Path("movie.001")))
        self.assertTrue(is_archive(Path("movie.7z")))
        self.assertFalse(is_archive(Path("movie.mkv")))

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

    def test_verified_additional_copy_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.srt"
            target = root / "target.srt"
            source.write_bytes(b"original")
            expected = sha256(source)
            source.write_bytes(b"changed")

            with self.assertRaises(RuntimeError):
                Stowarr._copy_verified(source, target, expected)

            self.assertFalse(target.exists())

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
        ])
        manager.config = SimpleNamespace(
            pools=(p1, p3),
            pool_for_path=lambda path: p1 if path.startswith("/p1") else p3 if path.startswith("/p3") else None,
        )

        result = manager.qbit_catalog()

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["routes"][0]["count"], 1)
        self.assertEqual(result["routes"][0]["paths"][0]["torrents"][0]["route_status"], "aligned")
        self.assertEqual([group["pool"] for group in result["unmanaged"]], ["p3", None])
        self.assertEqual(result["unmanaged"][0]["paths"][0]["path"], "/p3/download/tv")
        self.assertEqual(result["unmanaged"][0]["paths"][0]["route"], "download")

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
