import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from stowarr.archive import ArchiveExtractor, is_archive_path, safe_member_path, select_archive_entries, select_archive_entry


class ArchiveTest(unittest.TestCase):
    @patch.object(ArchiveExtractor, "_run")
    def test_reads_and_validates_archive_manifest(self, run):
        run.return_value = CompletedProcess([], 0, "Path = folder/movie.mkv\nSize = 12\nAttributes = A\n\n", "")

        members = ArchiveExtractor().members(Path("release.rar"))

        self.assertEqual([(item.relative_path, item.size) for item in members], [("folder/movie.mkv", 12)])

    def test_selects_rar_entry_in_multipart_set(self):
        paths = [Path("release.r01"), Path("release.r00"), Path("release.rar")]
        self.assertEqual(select_archive_entry(paths), Path("release.rar"))

    def test_selects_one_entry_per_independent_archive_set(self):
        paths = [
            Path("S01E01.part01.rar"), Path("S01E01.part02.rar"),
            Path("S01E02.rar"), Path("S01E02.r00"), Path("extras.7z.001"), Path("extras.7z.002"),
        ]
        self.assertEqual(
            select_archive_entries(paths),
            [Path("extras.7z.001"), Path("S01E01.part01.rar"), Path("S01E02.rar")],
        )

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
