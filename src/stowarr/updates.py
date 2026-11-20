from __future__ import annotations

import platform
import re
from urllib.error import HTTPError

from . import __commit__, __version__, normalize_commit
from .http import JsonClient

MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def _digest(value: str) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("Invalid GHCR image digest")
    return value


def _published_revision(architecture: str) -> str:
    package = "slashmad/stowarr"
    registry = JsonClient("https://ghcr.io", timeout=5)
    token = registry.request("GET", "/token", query={
        "service": "ghcr.io", "scope": f"repository:{package}:pull",
    })["token"]
    registry.headers.update({"Authorization": f"Bearer {token}", "Accept": MANIFEST_TYPES})
    path = f"/v2/{package}"
    manifest = registry.request("GET", f"{path}/manifests/latest")
    if "manifests" in manifest:
        matching = [
            item for item in manifest["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == architecture
        ]
        if len(matching) != 1:
            raise ValueError("GHCR has no unique image for this platform")
        digest = _digest(matching[0]["digest"])
        manifest = registry.request("GET", f"{path}/manifests/{digest}")
    digest = _digest(manifest["config"]["digest"])
    config = registry.request("GET", f"{path}/blobs/{digest}")["config"]
    labels = config.get("Labels") or {}
    revision = normalize_commit(labels.get("org.opencontainers.image.revision"))
    if len(revision) != 40:
        raise ValueError("The published image has no full Git revision")
    return revision


def check_updates(installed: str = __commit__) -> dict[str, str]:
    """Check the public Stowarr image without changing the installation."""
    installed = normalize_commit(installed)
    machine = platform.machine().lower()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)
    published = _published_revision(architecture)
    result = {
        "installed_version": __version__,
        "installed_commit": installed,
        "published_commit": published,
    }
    if installed != "unknown" and published.startswith(installed):
        return {**result, "status": "current"}
    if len(installed) != 40:
        return {**result, "status": "unknown"}
    github = JsonClient("https://api.github.com", timeout=5)
    try:
        comparison = github.request(
            "GET", f"/repos/slashmad/stowarr/compare/{installed}...{published}",
            query={"per_page": 1},
        )
    except HTTPError as error:
        if error.code != 404:
            raise
        error.close()
        return {**result, "status": "different"}
    return {
        **result,
        "status": "update-available" if comparison.get("status") == "ahead" else "different",
    }
