from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ARCHIVE_SUFFIXES = {".rar", ".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".tbz2", ".iso"}
VOLUME_SUFFIX = re.compile(r"(?:\.r\d{2,3}|\.\d{3})$", re.IGNORECASE)


def is_archive_path(path: Path) -> bool:
    return path.suffix.casefold() in ARCHIVE_SUFFIXES or bool(VOLUME_SUFFIX.search(path.name))


def select_archive_entry(paths: list[Path]) -> Path:
    """Select the file that an extractor should open for a multipart archive set."""
    candidates = sorted(paths, key=lambda path: path.name.casefold())
    preferred = [path for path in candidates if path.suffix.casefold() == ".rar"]
    preferred += [path for path in candidates if path.suffix.casefold() in {".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".tbz2", ".iso"}]
    preferred += [path for path in candidates if path.suffix.casefold() == ".001"]
    if not preferred:
        raise ValueError("No supported archive entry file was found")
    return preferred[0]


def safe_member_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe archive member path: {value}")
    return path


@dataclass(frozen=True)
class ExtractedFile:
    relative_path: str
    path: Path
    size: int


class ArchiveExtractor:
    """Run 7-Zip in an isolated directory and return only verified regular files."""

    def __init__(self, executable: str = "7z", timeout: int = 1800):
        self.executable = executable
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def _run(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.executable, *arguments],
                check=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"Archive extractor is unavailable: {self.executable}") from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Archive operation exceeded {self.timeout} seconds") from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "unknown extractor error").strip()
            raise RuntimeError(f"Archive operation failed: {detail[-2000:]}") from error

    def test(self, entry: Path) -> None:
        self._run(["t", "-bso0", "-bsp0", "-bse1", "--", str(entry)])

    def extract(self, entry: Path, destination: Path) -> list[ExtractedFile]:
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Archive staging directory is not empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.test(entry)
        self._run(["x", "-y", "-snl-", "-snh-", "-bso0", "-bsp0", "-bse1", f"-o{destination}", "--", str(entry)])

        files: list[ExtractedFile] = []
        for path in destination.rglob("*"):
            relative = path.relative_to(destination)
            safe_member_path(relative.as_posix())
            if path.is_symlink():
                raise RuntimeError(f"Archive created a symbolic link: {relative}")
            if path.is_file():
                files.append(ExtractedFile(relative.as_posix(), path, path.stat().st_size))
        if not files:
            raise RuntimeError("Archive extraction produced no regular files")
        return files
