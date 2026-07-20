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
