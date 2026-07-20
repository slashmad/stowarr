import unittest

from stowarr.clients import ArrClient
from stowarr.config import Service


class FakeHttp:
    def __init__(self):
        self.queries = []

    def request(self, method, path, query=None, **kwargs):
        if path == "/api/v3/history":
            self.queries.append(query["downloadId"])
            if query["downloadId"] == "ABC123":
                return {"records": [{"movieId": 42}]}
            return {"records": []}
        if path == "/api/v3/movie/42":
            return {"id": 42, "title": "masked"}
        raise AssertionError(path)


class ArrClientTest(unittest.TestCase):
    def test_download_id_falls_back_to_uppercase(self):
        client = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        client.http = FakeHttp()
        self.assertEqual(client.item_for_download("abc123")["id"], 42)
        self.assertEqual(client.http.queries, ["abc123", "ABC123"])

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
