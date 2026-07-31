import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stowarr.clients import ArrClient, QBittorrentClient
from stowarr.config import Service
from stowarr.mutations import ExternalMutationGuard

ALLOW_MUTATIONS = ExternalMutationGuard(lambda: False)


class FakeHttp:
    def __init__(self):
        self.queries = []

    def request(self, method, path, query=None, **kwargs):
        if path == "/api/v3/history":
            self.queries.append(query["downloadId"])
            if query["downloadId"] == "ABC123":
                return {"records": [{"downloadId": "ABC123", "movieId": 42}]}
            return {"records": []}
        if path == "/api/v3/movie/42":
            return {"id": 42, "title": "masked"}
        raise AssertionError(path)


class ArrClientTest(unittest.TestCase):
    def test_sonarr_managed_files_reads_current_episode_files(self):
        class EpisodeFileHttp:
            def request(self, method, path, query=None, **kwargs):
                self.requested = (method, path, query)
                return [{"id": 70, "path": "/series/Masked/episode.mkv"}]

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = EpisodeFileHttp()

        result = client.managed_files({"id": 7})

        self.assertEqual(result[0]["id"], 70)
        self.assertEqual(
            client.http.requested,
            ("GET", "/api/v3/episodefile", {"seriesId": 7}),
        )

    def test_radarr_managed_files_builds_path_from_relative_path(self):
        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")

        result = client.managed_files({
            "id": 4,
            "path": "/movies/Masked",
            "movieFile": {"id": 40, "relativePath": "Masked.mkv"},
        })

        self.assertEqual(result[0]["path"], "/movies/Masked/Masked.mkv")

    def test_sonarr_manual_import_preview_falls_back_to_exact_series_path(self):
        class ManualImportHttp:
            def __init__(self):
                self.queries = []

            def request(self, method, path, query=None, **kwargs):
                self.queries.append(query)
                if query.get("downloadId"):
                    return []
                return [{"path": "/downloads/Series.S01E01.mkv"}]

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = ManualImportHttp()

        result = client.manual_import_preview("/downloads/Series", "HASH", 7)

        self.assertEqual(result[0]["path"], "/downloads/Series.S01E01.mkv")
        self.assertEqual(client.http.queries[0]["downloadId"], "HASH")
        self.assertNotIn("downloadId", client.http.queries[1])
        self.assertEqual(client.http.queries[1]["seriesId"], 7)

    @patch("stowarr.clients.time.sleep")
    def test_sonarr_manual_import_uses_copy_mode_and_waits(self, sleep):
        class ManualImportHttp:
            def request(self, method, path, **kwargs):
                if method == "POST":
                    self.body = kwargs["body"]
                    return {"id": 23}
                return {"id": 23, "status": "completed"}

        client = ArrClient(
            Service("http://unused", api_key="unused"),
            "sonarr",
            ALLOW_MUTATIONS,
        )
        client.http = ManualImportHttp()
        files = [{"path": "/downloads/Series.S01E01.mkv", "episodeIds": [70]}]

        result = client.manual_import(files)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            client.http.body,
            {"name": "ManualImport", "files": files, "importMode": "Copy"},
        )
        sleep.assert_not_called()

    @patch("stowarr.clients.time.sleep")
    def test_rescan_waits_for_completed_arr_command(self, sleep):
        class CommandHttp:
            def __init__(self):
                self.statuses = iter(("queued", "completed"))

            def request(self, method, path, **kwargs):
                if method == "POST":
                    self.body = kwargs["body"]
                    return {"id": 19}
                return {"id": 19, "status": next(self.statuses)}

        client = ArrClient(
            Service("http://unused", api_key="unused"),
            "radarr",
            ALLOW_MUTATIONS,
        )
        client.http = CommandHttp()
        result = client.rescan(42)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(client.http.body, {"name": "RescanMovie", "movieId": 42})
        sleep.assert_called_once_with(2)

    def test_download_id_falls_back_to_uppercase(self):
        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        client.http = FakeHttp()
        self.assertEqual(client.item_for_download("abc123")["id"], 42)
        self.assertEqual(client.http.queries, ["abc123", "ABC123"])

    def test_download_mapping_rejects_unfiltered_records_for_other_hashes(self):
        class UnfilteredHttp:
            def request(self, method, path, query=None, **kwargs):
                if path == "/api/v3/history":
                    return {"records": [
                        {
                            "id": 1,
                            "downloadId": "S01-HASH",
                            "seriesId": 7,
                            "episodeId": 101,
                        },
                        {
                            "id": 2,
                            "downloadId": "S00-HASH",
                            "seriesId": 7,
                            "episodeId": 4,
                        },
                    ]}
                if path == "/api/v3/series/7":
                    return {"id": 7, "title": "Archer", "path": "/series/Archer"}
                if path == "/api/v3/episode":
                    return [
                        {"id": 4, "episodeFileId": 704},
                        {"id": 101, "episodeFileId": 7101},
                    ]
                if path == "/api/v3/episodefile":
                    return [
                        {"id": 704, "path": "/series/Archer/S00E04.mkv", "size": 40},
                        {"id": 7101, "path": "/series/Archer/S01E01.mkv", "size": 100},
                    ]
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = UnfilteredHttp()

        mapping = client.download_mapping("s00-hash")

        self.assertEqual([record["id"] for record in mapping["history"]], [2])
        self.assertEqual([record["id"] for record in mapping["files"]], [704])

    def test_bulk_history_matches_hashes_case_insensitively(self):
        class BulkHttp:
            def request(self, method, path, query=None, **kwargs):
                self.query = query
                return {"records": [
                    {"downloadId": "ABC123", "movieId": 42},
                    {"downloadId": "unrelated", "movieId": 99},
                ]}

        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        client.http = BulkHttp()
        self.assertEqual(client.history_for_downloads({"abc123"}), {"abc123": 42})
        self.assertEqual(client.http.query["sortDirection"], "descending")

    def test_download_mapping_returns_none_when_historical_item_was_deleted(self):
        class DeletedMovieHttp:
            def request(self, method, path, query=None, **kwargs):
                if path == "/api/v3/history":
                    return {
                        "records": [{
                            "downloadId": "HASH",
                            "movieId": 42,
                        }]
                    }
                if path == "/api/v3/movie/42":
                    raise HTTPError(
                        "http://unused/api/v3/movie/42",
                        404,
                        "Not Found",
                        {},
                        None,
                    )
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        client.http = DeletedMovieHttp()

        self.assertIsNone(client.download_mapping("hash"))

    def test_sonarr_mapping_includes_only_episode_files_owned_by_download(self):
        class SonarrHttp:
            def request(self, method, path, query=None, **kwargs):
                if path == "/api/v3/history":
                    return {"records": [
                        {"id": 1, "downloadId": "HASH", "seriesId": 7, "episodeId": 70},
                        {"id": 2, "downloadId": "HASH", "seriesId": 7, "episodeId": 71},
                    ]}
                if path == "/api/v3/series/7":
                    return {"id": 7, "title": "Series", "path": "/series/Series"}
                if path == "/api/v3/episode":
                    return [
                        {"id": 70, "episodeFileId": 700},
                        {"id": 71, "episodeFileId": 700},
                        {"id": 72, "episodeFileId": 701},
                    ]
                if path == "/api/v3/episodefile":
                    return [
                        {"id": 700, "path": "/series/Series/Season 01/S01E01-E02.mkv", "size": 100},
                        {"id": 701, "path": "/series/Series/Season 01/S01E03.mkv", "size": 50},
                    ]
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = SonarrHttp()
        mapping = client.download_mapping("hash")

        self.assertTrue(mapping["mappingComplete"])
        self.assertEqual([record["id"] for record in mapping["files"]], [700])
        self.assertEqual(mapping["files"][0]["episodeIds"], [70, 71])
        self.assertEqual(
            [record["id"] for record in mapping["allFiles"]],
            [700, 701],
        )
        self.assertEqual(mapping["allFiles"][1]["episodeIds"], [72])

    def test_sonarr_mapping_is_incomplete_without_episode_identity(self):
        class SonarrHttp:
            def request(self, method, path, query=None, **kwargs):
                if path == "/api/v3/history":
                    return {"records": [{"id": 1, "downloadId": "HASH", "seriesId": 7}]}
                if path == "/api/v3/series/7":
                    return {"id": 7, "title": "Series", "path": "/series/Series"}
                if path in {"/api/v3/episode", "/api/v3/episodefile"}:
                    return []
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = SonarrHttp()
        self.assertFalse(client.download_mapping("hash")["mappingComplete"])

    def test_sonarr_mapping_is_incomplete_when_history_episode_is_missing(self):
        class SonarrHttp:
            def request(self, method, path, query=None, **kwargs):
                if path == "/api/v3/history":
                    return {"records": [{
                        "id": 1,
                        "downloadId": "HASH",
                        "seriesId": 7,
                        "data": {"episodeIds": [70, 71]},
                    }]}
                if path == "/api/v3/series/7":
                    return {"id": 7, "title": "Series", "path": "/series/Series"}
                if path == "/api/v3/episode":
                    return [{"id": 70, "episodeFileId": 700}]
                if path == "/api/v3/episodefile":
                    return [{
                        "id": 700,
                        "path": "/series/Series/Season 01/S01E01.mkv",
                        "size": 100,
                    }]
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = SonarrHttp()

        self.assertFalse(client.download_mapping("hash")["mappingComplete"])

    def test_radarr_library_mapping_requires_an_exact_managed_file_path(self):
        class RadarrHttp:
            def request(self, method, path, **kwargs):
                if path == "/api/v3/movie":
                    return [
                        {
                            "id": 42,
                            "title": "Movie",
                            "path": "/movies/Movie (2020)",
                            "movieFile": {
                                "id": 420,
                                "path": "/movies/Movie (2020)/Movie.mkv",
                                "size": 100,
                            },
                        }
                    ]
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        client.http = RadarrHttp()

        mapping = client.library_mapping(["/movies/Movie (2020)/Movie.mkv"])
        self.assertEqual(mapping["item"]["id"], 42)
        self.assertEqual(mapping["mappingSource"], "exact-library-path")
        self.assertIsNone(client.library_mapping(["/movies/Movie (2020)/Other.mkv"]))

    def test_sonarr_library_mapping_requires_every_selected_video_path(self):
        class SonarrHttp:
            def request(self, method, path, **kwargs):
                if path == "/api/v3/series":
                    return [{"id": 7, "title": "Series", "path": "/series/Series"}]
                if path == "/api/v3/episodefile":
                    return [
                        {"id": 700, "path": "/series/Series/Season 01/S01E01.mkv", "size": 100},
                        {"id": 701, "path": "/series/Series/Season 01/S01E02.mkv", "size": 101},
                    ]
                if path == "/api/v3/episode":
                    return [
                        {"id": 70, "episodeFileId": 700},
                        {"id": 71, "episodeFileId": 701},
                    ]
                raise AssertionError(path)

        client = ArrClient(Service("http://unused", api_key="unused"), "sonarr")
        client.http = SonarrHttp()

        mapping = client.library_mapping(["/series/Series/Season 01/S01E01.mkv"])
        self.assertTrue(mapping["mappingComplete"])
        self.assertEqual(mapping["files"][0]["episodeIds"], [70])
        self.assertEqual(
            [record["id"] for record in mapping["allFiles"]], [700, 701]
        )
        self.assertEqual(mapping["allFiles"][1]["episodeIds"], [71])
        self.assertIsNone(
            client.library_mapping([
                "/series/Series/Season 01/S01E01.mkv",
                "/series/Series/Season 01/S01E03.mkv",
            ])
        )


class QBittorrentClientTest(unittest.TestCase):
    @patch("stowarr.clients.JsonClient")
    def test_version_uses_qbittorrent_plain_text_endpoint(self, json_client):
        json_client.return_value.request_text.return_value = "v5.2.1"
        client = QBittorrentClient(
            Service("http://qbit", api_key="key"), ALLOW_MUTATIONS
        )

        self.assertEqual(client.version(), "v5.2.1")
        json_client.return_value.request_text.assert_called_once_with("GET", "/api/v2/app/version")

    @patch("stowarr.clients.JsonClient")
    def test_api_key_is_preferred_and_skips_login(self, json_client):
        QBittorrentClient(Service("http://qbit", api_key="key", username="user", password="password"))

        json_client.assert_called_once_with("http://qbit", {"Authorization": "Bearer key"})
        json_client.return_value.request.assert_not_called()

    @patch("stowarr.clients.JsonClient")
    def test_username_password_login_is_used_without_api_key(self, json_client):
        QBittorrentClient(Service("http://qbit", username="user", password="password"))

        json_client.assert_called_once_with("http://qbit", None)
        json_client.return_value.request.assert_called_once_with(
            "POST", "/api/v2/auth/login", form={"username": "user", "password": "password"}
        )

    @patch("stowarr.clients.JsonClient")
    def test_temporary_category_is_created_with_destination_path(self, json_client):
        json_client.return_value.request.side_effect = [{}, None]
        client = QBittorrentClient(
            Service("http://qbit", api_key="key"), ALLOW_MUTATIONS
        )

        client.ensure_category("radarr-stowarr-moving-hash", "/p1/download")

        self.assertEqual(
            json_client.return_value.request.call_args_list[-1].args,
            ("POST", "/api/v2/torrents/createCategory"),
        )
        self.assertEqual(
            json_client.return_value.request.call_args_list[-1].kwargs["form"],
            {"category": "radarr-stowarr-moving-hash", "savePath": "/p1/download"},
        )
