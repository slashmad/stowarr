from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pool:
    name: str
    prefix: Path
    download_roots: tuple[Path, ...]
    radarr_root: Path
    sonarr_root: Path
    radarr_category: str
    sonarr_category: str
    radarr_tag: str
    sonarr_tag: str

    def contains(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.prefix.resolve())
            return True
        except ValueError:
            return False


@dataclass(frozen=True)
class Service:
    url: str
    api_key: str = ""
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class Config:
    pools: tuple[Pool, ...]
    qbittorrent: Service
    radarr: Service
    sonarr: Service
    database: Path
    apply: bool
    listen: str
    port: int
    api_token: str
    api_only: bool
    auth_method: str
    external_user_header: str

    def pool_for_path(self, path: str | Path) -> Pool | None:
        candidate = Path(path)
        matches = [pool for pool in self.pools if pool.contains(candidate)]
        return max(matches, key=lambda pool: len(str(pool.prefix)), default=None)

    def pool_for_category(self, category: str) -> tuple[Pool, str] | None:
        for pool in self.pools:
            if category == pool.radarr_category:
                return pool, "radarr"
            if category == pool.sonarr_category:
                return pool, "sonarr"
        return None


def _service(raw: dict, name: str) -> Service:
    value = raw.get(name, {})
    return Service(
        url=str(value.get("url", "")).rstrip("/"),
        api_key=os.getenv(f"{name.upper()}_API_KEY", value.get("api_key", "")),
        username=os.getenv(f"{name.upper()}_USERNAME", value.get("username", "")),
        password=os.getenv(f"{name.upper()}_PASSWORD", value.get("password", "")),
    )


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path or os.getenv("STOWARR_CONFIG", "/config/config.json"))
    raw = json.loads(config_path.read_text())
    pools = tuple(
        Pool(
            name=name,
            prefix=Path(value["prefix"]),
            download_roots=tuple(Path(item) for item in value["download_roots"]),
            radarr_root=Path(value["radarr_root"]),
            sonarr_root=Path(value["sonarr_root"]),
            radarr_category=value["radarr_category"],
            sonarr_category=value["sonarr_category"],
            radarr_tag=value["radarr_tag"],
            sonarr_tag=value["sonarr_tag"],
        )
        for name, value in raw["pools"].items()
    )
    auth_method = os.getenv("STOWARR_AUTH_METHOD", raw.get("auth_method", "forms")).strip().casefold()
    if auth_method not in {"forms", "external"}:
        raise ValueError("STOWARR_AUTH_METHOD must be forms or external")
    return Config(
        pools=pools,
        qbittorrent=_service(raw, "qbittorrent"),
        radarr=_service(raw, "radarr"),
        sonarr=_service(raw, "sonarr"),
        database=Path(raw.get("database", "/state/stowarr.sqlite3")),
        apply=os.getenv("STOWARR_APPLY", str(raw.get("apply", False))).lower() == "true",
        listen=os.getenv("STOWARR_LISTEN", raw.get("listen", "0.0.0.0")),
        port=int(os.getenv("STOWARR_PORT", raw.get("port", 8787))),
        api_token=os.getenv("STOWARR_API_TOKEN", raw.get("api_token", "")),
        api_only=os.getenv("STOWARR_API_ONLY", str(raw.get("api_only", False))).lower() == "true",
        auth_method=auth_method,
        external_user_header=os.getenv("STOWARR_EXTERNAL_USER_HEADER", "X-Forwarded-User").strip(),
    )
