from __future__ import annotations

import os
import re

__version__ = "1.0.0-beta.5"


def normalize_commit(value: str | None) -> str:
    """Return a trustworthy Git revision for diagnostics, or ``unknown``."""
    candidate = (value or "").strip().lower()
    return candidate if re.fullmatch(r"[0-9a-f]{7,64}", candidate) else "unknown"


__commit__ = normalize_commit(os.environ.get("STOWARR_COMMIT"))
