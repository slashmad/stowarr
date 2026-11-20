import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stowarr.config import load_config


class ConfigTest(unittest.TestCase):
    def test_path_and_category_select_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            raw = {
                "qbittorrent": {"url": "http://qbit"},
                "radarr": {"url": "http://radarr"},
                "sonarr": {"url": "http://sonarr"},
                "database": str(tmp_path / "state.db"),
                "pools": {
                    "p1": {
                        "prefix": "/data/p1", "download_roots": ["/data/p1/download"],
                        "radarr_root": "/data/p1/movies", "sonarr_root": "/data/p1/series",
                        "radarr_category": "radarr-p1", "sonarr_category": "sonarr-p1",
                        "radarr_tag": "radarr-p1", "sonarr_tag": "sonarr-p1"
                    }
                }
            }
            path = tmp_path / "config.json"
            path.write_text(json.dumps(raw))
            with patch.dict(os.environ, {"QBITTORRENT_API_KEY": "preferred", "QBITTORRENT_PASSWORD": "secret"}):
                config = load_config(path)
            self.assertEqual(config.pool_for_path("/data/p1/download/release/file.mkv").name, "p1")
            self.assertIsNone(config.pool_for_path("/other/file.mkv"))
            self.assertEqual(config.pool_for_category("sonarr-p1")[1], "sonarr")
            self.assertEqual(config.qbittorrent.password, "secret")
            self.assertEqual(config.qbittorrent.api_key, "preferred")
            self.assertEqual(config.auth_method, "forms")

    def test_rejects_unknown_authentication_method(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"pools": {}}))
            with patch.dict(os.environ, {"STOWARR_AUTH_METHOD": "unsafe"}, clear=True):
                with self.assertRaisesRegex(ValueError, "forms or external"):
                    load_config(path)

    def test_web_listener_port_is_optional_and_must_differ_from_api_port(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"pools": {}}))
            with patch.dict(os.environ, {"STOWARR_PORT": "8788"}, clear=True):
                self.assertIsNone(load_config(path).web_port)
                with patch.dict(os.environ, {"STOWARR_WEB_PORT": "8080"}):
                    self.assertEqual(load_config(path).web_port, 8080)
                with patch.dict(os.environ, {"STOWARR_WEB_PORT": "8788"}):
                    with self.assertRaisesRegex(ValueError, "distinct"):
                        load_config(path)
