#!/usr/bin/env python3
"""Reject direct filesystem writes that bypass Stowarr's recovery guard."""

from __future__ import annotations

import ast
from pathlib import Path

ENGINE = Path("src/stowarr/engine.py")
DIRECT_MODULE_WRITES = {
    ("os", "link"),
    ("os", "replace"),
    ("shutil", "copy2"),
    ("shutil", "rmtree"),
}
DIRECT_PATH_WRITES = {"mkdir", "rmdir", "unlink", "write_bytes", "write_text"}


def dotted_name(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


def uses_guarded_filesystem(node: ast.AST) -> bool:
    return any(
        isinstance(part, ast.Attribute) and part.attr == "_filesystem"
        for part in ast.walk(node)
    )


tree = ast.parse(ENGINE.read_text(), filename=str(ENGINE))
violations: list[str] = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    name = dotted_name(node.func)
    if name in DIRECT_MODULE_WRITES:
        violations.append(f"{ENGINE}:{node.lineno}: direct {'.'.join(name)}")
        continue
    if (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in DIRECT_PATH_WRITES
        and not uses_guarded_filesystem(node.func.value)
    ):
        violations.append(
            f"{ENGINE}:{node.lineno}: direct Path.{node.func.attr}"
        )

if violations:
    raise SystemExit(
        "External filesystem mutations must pass through GuardedFilesystem:\n"
        + "\n".join(violations)
    )
