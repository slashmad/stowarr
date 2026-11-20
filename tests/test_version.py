import unittest
from unittest.mock import patch

from stowarr import __version__, normalize_commit
from stowarr.updates import check_updates


class BuildVersionTest(unittest.TestCase):
    def test_normalizes_valid_git_revisions(self):
        self.assertEqual(normalize_commit(" A1B2C3D4\n"), "a1b2c3d4")
        self.assertEqual(
            normalize_commit("d3b9315b1a2c3d4e5f60718293a4b5c6d7e8f901"),
            "d3b9315b1a2c3d4e5f60718293a4b5c6d7e8f901",
        )

    def test_rejects_missing_or_untrusted_build_values(self):
        self.assertEqual(normalize_commit(None), "unknown")
        self.assertEqual(normalize_commit(""), "unknown")
        self.assertEqual(normalize_commit("main"), "unknown")
        self.assertEqual(normalize_commit("abc; unexpected"), "unknown")

    @patch("stowarr.updates.JsonClient")
    @patch("stowarr.updates._published_revision")
    def test_update_check_requires_a_newer_revision(self, published, json_client):
        installed, latest = "a" * 40, "b" * 40
        published.return_value = latest
        self.assertEqual(check_updates("unknown")["status"], "unknown")
        self.assertEqual(check_updates(latest)["status"], "current")
        json_client.assert_not_called()

        json_client.return_value.request.return_value = {"status": "ahead"}
        self.assertEqual(check_updates(installed), {
            "installed_version": __version__,
            "installed_commit": installed,
            "published_commit": latest,
            "status": "update-available",
        })
        json_client.return_value.request.assert_called_once_with(
            "GET", f"/repos/slashmad/stowarr/compare/{installed}...{latest}",
            query={"per_page": 1},
        )
        json_client.return_value.request.return_value = {"status": "diverged"}
        self.assertEqual(check_updates(installed)["status"], "different")

    @patch("stowarr.updates.JsonClient")
    def test_update_check_reads_the_unified_linux_image_and_skips_attestations(self, json_client):
        registry = json_client.return_value
        registry.headers = {}
        latest = "b" * 40
        manifest_digest = "sha256:" + "1" * 64
        config_digest = "sha256:" + "2" * 64
        registry.request.side_effect = [
            {"token": "public-token"},
            {"manifests": [
                {"platform": {"os": "unknown", "architecture": "unknown"}, "digest": "attestation"},
                {"platform": {"os": "linux", "architecture": "arm64"}, "digest": manifest_digest},
            ]},
            {"config": {"digest": config_digest}},
            {"config": {"Labels": {"org.opencontainers.image.revision": latest}}},
        ]

        with patch("stowarr.updates.platform.machine", return_value="aarch64"):
            self.assertEqual(check_updates(latest)["status"], "current")

        self.assertEqual(registry.request.call_count, 4)
        registry.request.assert_any_call("GET", f"/v2/slashmad/stowarr/manifests/{manifest_digest}")
        registry.request.assert_any_call("GET", f"/v2/slashmad/stowarr/blobs/{config_digest}")
