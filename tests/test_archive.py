import tempfile
import unittest
from pathlib import Path

from stowarr.archive import is_archive_path, safe_member_path, select_archive_entry


class ArchiveTest(unittest.TestCase):
    def test_selects_rar_entry_in_multipart_set(self):
        paths = [Path("release.r01"), Path("release.r00"), Path("release.rar")]
        self.assertEqual(select_archive_entry(paths), Path("release.rar"))

    def test_selects_first_numbered_volume(self):
        paths = [Path("release.003"), Path("release.002"), Path("release.001")]
        self.assertEqual(select_archive_entry(paths), Path("release.001"))

    def test_recognizes_supported_archives_and_volumes(self):
        for name in ("release.rar", "release.r00", "release.zip", "release.7z.001", "release.iso"):
            self.assertTrue(is_archive_path(Path(name)))
        self.assertFalse(is_archive_path(Path("release.mkv")))

    def test_rejects_paths_that_escape_staging(self):
        safe_member_path("folder/movie.mkv")
        for name in ("../movie.mkv", "/etc/passwd", "folder/../../movie.mkv"):
            with self.assertRaises(ValueError):
                safe_member_path(name)
