from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

RECOVERY_BLOCKED_MESSAGE = (
    "A previous operation was interrupted. Review Recovery in Queue "
    "before starting or queuing more write operations."
)


class RecoveryBlockedError(RuntimeError):
    """Raised when recovery intentionally blocks an external mutation."""


class ExternalMutationGuard:
    """Single enforcement point for writes outside Stowarr's SQLite state."""

    def __init__(self, recovery_required: Callable[[], bool]):
        self._recovery_required = recovery_required

    def require_allowed(self) -> None:
        if self._recovery_required():
            raise RecoveryBlockedError(RECOVERY_BLOCKED_MESSAGE)

    def execute(
        self,
        boundary: str,
        action: str,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        del boundary, action
        self.require_allowed()
        return operation(*args, **kwargs)


class GuardedFilesystem:
    """Filesystem mutations that cannot bypass the recovery invariant."""

    def __init__(self, guard: ExternalMutationGuard):
        self._guard = guard

    def execute(
        self,
        action: str,
        operation: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return self._guard.execute(
            "filesystem", action, operation, *args, **kwargs
        )

    def mkdir(self, path: Path, **kwargs: Any) -> None:
        self._guard.execute("filesystem", "mkdir", path.mkdir, **kwargs)

    def unlink(self, path: Path, *, missing_ok: bool = False) -> None:
        self._guard.execute(
            "filesystem", "unlink", path.unlink, missing_ok=missing_ok
        )

    def rmdir(self, path: Path) -> None:
        self._guard.execute("filesystem", "rmdir", path.rmdir)

    def copy2(self, source: Path, target: Path) -> None:
        self._guard.execute("filesystem", "copy", shutil.copy2, source, target)

    def replace(self, source: Path, target: Path) -> None:
        self._guard.execute("filesystem", "replace", os.replace, source, target)

    def link(self, source: Path, target: Path) -> None:
        self._guard.execute("filesystem", "hardlink", os.link, source, target)

    def rmtree(self, path: Path, *, ignore_errors: bool = False) -> None:
        self._guard.execute(
            "filesystem",
            "remove tree",
            shutil.rmtree,
            path,
            ignore_errors=ignore_errors,
        )
