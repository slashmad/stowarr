from __future__ import annotations

import os
import re
import selectors
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


ARCHIVE_SUFFIXES = {".rar", ".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".tbz2", ".iso"}
VOLUME_SUFFIX = re.compile(r"(?:\.r\d{2,3}|\.\d{3})$", re.IGNORECASE)
RAR_PART = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)


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


def select_archive_entries(paths: list[Path]) -> list[Path]:
    """Return one extractor entry for every independent archive set."""
    entries: list[Path] = []
    for path in sorted(paths, key=lambda item: item.as_posix().casefold()):
        suffix = path.suffix.casefold()
        part = RAR_PART.search(path.name)
        if suffix == ".rar" and (not part or int(part.group(1)) == 1):
            entries.append(path)
        elif suffix in {".zip", ".7z", ".tar", ".tgz", ".gz", ".bz2", ".tbz2", ".iso"}:
            entries.append(path)
        elif suffix == ".001":
            entries.append(path)
    if not entries:
        raise ValueError("No supported archive entry file was found")
    return entries


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


@dataclass(frozen=True)
class ArchiveMember:
    relative_path: str
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

    def members(self, entry: Path) -> list[ArchiveMember]:
        result = self._run(["l", "-slt", "-ba", "-bsp0", "-bse1", "--", str(entry)])
        members: list[ArchiveMember] = []
        current: dict[str, str] = {}
        for line in (*result.stdout.splitlines(), ""):
            if not line.strip():
                path = current.get("Path")
                size = current.get("Size")
                attributes = current.get("Attributes", "")
                if path and size is not None and not attributes.startswith("D"):
                    safe_member_path(path)
                    members.append(ArchiveMember(path.replace("\\", "/"), int(size)))
                current = {}
                continue
            if " = " in line:
                key, value = line.split(" = ", 1)
                current[key] = value
        if not members:
            raise RuntimeError("Archive manifest contains no regular files")
        return members

    def extract(self, entry: Path, destination: Path, progress=None) -> list[ExtractedFile]:
        if destination.exists() and any(destination.iterdir()):
            raise RuntimeError(f"Archive staging directory is not empty: {destination}")
        destination.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.test(entry)
        command = [self.executable, "x", "-y", "-snl-", "-snh-", "-bso0", "-bsp1", "-bse1", f"-o{destination}", "--", str(entry)]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except FileNotFoundError as error:
            raise RuntimeError(f"Archive extractor is unavailable: {self.executable}") from error
        started = time.monotonic()
        output = b""
        assert process.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stderr, selectors.EVENT_READ)
        while process.poll() is None:
            if time.monotonic() - started > self.timeout:
                process.kill()
                raise RuntimeError(f"Archive operation exceeded {self.timeout} seconds")
            events = selector.select(timeout=0.25)
            if events:
                chunk = os.read(process.stderr.fileno(), 4096)
                output = (output + chunk)[-2000:]
                matches = re.findall(rb"(\d{1,3})%", chunk)
                if matches and progress:
                    progress(min(100, int(matches[-1])))
        selector.close()
        output = (output + process.stderr.read())[-2000:]
        if process.returncode:
            detail = output.decode(errors="replace").strip() or "unknown extractor error"
            raise RuntimeError(f"Archive operation failed: {detail}")
        if progress:
            progress(100)

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
