import unittest

from stowarr import normalize_commit


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
