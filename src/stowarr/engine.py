from __future__ import annotations

import hashlib
import os
import re
import shutil
import secrets
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .clients import ArrClient, QBittorrentClient
from .archive import ArchiveExtractor, is_archive_path, select_archive_entries
from .auth import AuthManager
from .config import Config, Pool, Service
from .store import Store


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".vtt"}
ARTWORK_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
TITLE_STOPWORDS = {"the", "and", "for", "with", "from", "part", "movie"}


def title_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in TITLE_STOPWORDS and not token.isdigit()
    }


def title_matches(item_title: str, *candidate_names: str) -> bool:
    expected = title_tokens(item_title)
    if not expected:
        return True
    actual = title_tokens(" ".join(candidate_names))
    return bool(expected & actual)


@dataclass
class FilePair:
    source_library: str
    target_library: str
    torrent_file: str
    size: int
    status: str
    strategy: str = "hardlink"


@dataclass
class AuxiliaryFile:
    source: str
    target: str
    size: int
    status: str
    origin: str
    operation: str
    kind: str


@dataclass
class Plan:
    torrent_hash: str
    torrent_name: str
    app: str
    target_pool: str
    item_id: int | None
    item_title: str | None
    current_item_path: str | None
    target_item_path: str | None
    pairs: list[FilePair]
    status: str
    reason: str = ""
    error_code: str | None = None
    error_details: dict | None = None
    auxiliary_files: list[AuxiliaryFile] | None = None
    managed_files: list[dict] | None = None

    def json(self) -> dict:
        return {
            **asdict(self),
            "pairs": [asdict(pair) for pair in self.pairs],
            "auxiliary_files": [asdict(item) for item in (self.auxiliary_files or [])],
            "managed_files": self.managed_files or [],
        }


@dataclass
class MovePlan:
    torrent_hash: str
    torrent_name: str
    app: str
    current_pool: str | None
    target_pool: str
    current_save_path: str
    target_save_path: str | None
    target_category: str | None
    item_id: int | None
    item_title: str | None
    managed_files: list[dict]
    torrent_size: int
    free_space: int | None
    status: str
    reason: str = ""
    content_mode: str = "unknown"
    archive_files: int = 0
    current_item_path: str | None = None
    target_item_path: str | None = None
    tracked_files: list[dict] | None = None
    additional_files: list[dict] | None = None
    current_content_root: str | None = None
    extraction_required: bool = False
    extraction_space: int = 0
    extraction_files: list[dict] | None = None

    def json(self) -> dict:
        return asdict(self)


def sha256(path: Path, chunk_size: int = 8 * 1024 * 1024, progress=None) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    completed = 0
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            completed += len(chunk)
            if progress:
                progress(completed, total)
    return digest.hexdigest()


def sidecar_kind(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in SUBTITLE_EXTENSIONS:
        return "subtitle"
    if suffix in ARTWORK_EXTENSIONS:
        return "artwork"
    if suffix == ".nfo":
        return "metadata"
    return "other"


def is_archive(path: Path) -> bool:
    return is_archive_path(path)


class Stowarr:
    def __init__(self, config: Config):
        self.store = Store(config.database)
        self.auth = AuthManager(self.store)
        self.generated_api_token = None
        saved_api_auth = self.store.setting("api_auth") or {}
        if not config.api_token:
            api_token = str(saved_api_auth.get("token", ""))
            if not api_token:
                api_token = secrets.token_urlsafe(32)
                self.store.set_setting("api_auth", {"token": api_token})
                self.generated_api_token = api_token
            config = replace(config, api_token=api_token)
        runtime = self.store.setting("runtime")
        if runtime and isinstance(runtime.get("apply"), bool):
            config = replace(config, apply=runtime["apply"])
        saved = self.store.setting("connections")
        if saved:
            config = replace(
                config,
                qbittorrent=Service(**saved["qbittorrent"]),
                radarr=Service(**saved["radarr"]),
                sonarr=Service(**saved["sonarr"]),
            )
        self.config = config
        self.qbit = None
        self.arr = {}
        self.connection_error = None
        self.archive_extractor = ArchiveExtractor()
        try:
            self._activate_connections(config.qbittorrent, config.radarr, config.sonarr, validate=False)
        except Exception as error:
            self.connection_error = str(error)

    @property
    def connections_ready(self) -> bool:
        return self.qbit is not None and set(self.arr) == {"radarr", "sonarr"}

    def _activate_connections(self, qbittorrent: Service, radarr: Service, sonarr: Service, validate: bool = True) -> dict:
        qbit = QBittorrentClient(qbittorrent) if qbittorrent.url and (qbittorrent.api_key or (qbittorrent.username and qbittorrent.password)) else None
        arr = {
            name: ArrClient(service, name)
            for name, service in (("radarr", radarr), ("sonarr", sonarr))
            if service.url and service.api_key
        }
        versions = {}
        if validate:
            if qbit:
                try:
                    qbit.categories()
                except Exception as error:
                    raise ConnectionError(f"qBittorrent connection failed: {error}") from error
                versions["qbittorrent"] = "connected"
            for name, client in arr.items():
                try:
                    versions[name] = client.status().get("version", "connected")
                except Exception as error:
                    raise ConnectionError(f"{name.capitalize()} connection failed: {error}") from error
        self.qbit = qbit
        self.arr = arr
        self.connection_error = None
        return versions

    @staticmethod
    def _masked_service(service: Service, kind: str) -> dict:
        result = {"url": service.url}
        if kind == "qbittorrent":
            result.update({
                "api_key_set": bool(service.api_key),
                "auth_method": "api_key" if service.api_key else "login",
                "username": service.username,
                "password_set": bool(service.password),
            })
        else:
            result["api_key_set"] = bool(service.api_key)
        return result

    def connection_settings(self) -> dict:
        configured = {
            "qbittorrent": self.qbit is not None,
            "radarr": "radarr" in self.arr,
            "sonarr": "sonarr" in self.arr,
        }
        return {
            "required": False,
            "status": "ready" if self.connections_ready else "partial" if any(configured.values()) else "unconfigured",
            "configured": configured,
            "error": self.connection_error,
            "services": {
                "qbittorrent": self._masked_service(self.config.qbittorrent, "qbittorrent"),
                "radarr": self._masked_service(self.config.radarr, "radarr"),
                "sonarr": self._masked_service(self.config.sonarr, "sonarr"),
            },
        }

    def update_connections(self, payload: dict) -> dict:
        services = payload.get("services") if isinstance(payload, dict) else None
        if not isinstance(services, dict):
            raise ValueError("services is required")
        current = {"qbittorrent": self.config.qbittorrent, "radarr": self.config.radarr, "sonarr": self.config.sonarr}
        candidates = {}
        for name, existing in current.items():
            raw = services.get(name, {})
            if not isinstance(raw, dict):
                raise ValueError(f"{name} configuration must be an object")
            url = str(raw.get("url", "")).strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} URL must start with http:// or https://")
            if name == "qbittorrent":
                api_key = str(raw.get("api_key") or existing.api_key).strip()
                username = str(raw.get("username", "")).strip()
                password = str(raw.get("password") or existing.password)
                if url and not api_key and (not username or not password):
                    raise ValueError("qBittorrent API key or username and password are required")
                candidates[name] = Service(url=url, api_key=api_key if url else "", username=username if url else "", password=password if url else "")
            else:
                api_key = str(raw.get("api_key") or existing.api_key).strip()
                if url and not api_key:
                    raise ValueError(f"{name.capitalize()} API key is required")
                candidates[name] = Service(url=url, api_key=api_key if url else "")

        versions = self._activate_connections(
            candidates["qbittorrent"], candidates["radarr"], candidates["sonarr"], validate=True
        )

        self.store.set_setting("connections", {name: asdict(service) for name, service in candidates.items()})
        self.config = replace(self.config, **candidates)
        self.connection_error = None
        return {"versions": versions, **self.connection_settings()}

    def connection_discovery(self) -> dict:
        if not self.connections_ready:
            raise RuntimeError("Configure qBittorrent, Radarr, and Sonarr first")
        qbit_categories = [
            {"category": name, "save_path": value.get("savePath") or ""}
            for name, value in sorted(self.qbit.categories().items())
        ]
        services = []
        for app in ("radarr", "sonarr"):
            client = self.arr[app]
            tags = {int(item["id"]): item["label"] for item in client.tags()}
            category_field = "movieCategory" if app == "radarr" else "tvCategory"
            download_clients = []
            for item in client.download_clients():
                category = self._client_field(item, category_field)
                if not category:
                    continue
                download_clients.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "enabled": bool(item.get("enable")),
                    "category": category,
                    "tags": [tags.get(int(value), f"unknown:{value}") for value in item.get("tags", [])],
                })
            services.append({
                "app": app,
                "download_clients": download_clients,
                "root_folders": [item.get("path") for item in client.root_folders()],
            })
        return {"qbit_categories": qbit_categories, "services": services}

    def runtime_settings(self) -> dict:
        return {
            "apply": self.config.apply,
            "deployment": {
                "config_path": os.getenv("STOWARR_CONFIG", "/config/config.json"),
                "listen": self.config.listen,
                "port": self.config.port,
                "api_only": self.config.api_only,
                "auth_method": self.config.auth_method,
                "external_user_header": self.config.external_user_header,
                "api_token_set": bool(self.config.api_token),
                "media_mount_mode": os.getenv("STOWARR_MEDIA_MOUNT_MODE", "unknown"),
                "timezone": os.getenv("TZ", "UTC"),
                "configured_puid": os.getenv("PUID", "not set"),
                "configured_pgid": os.getenv("PGID", "not set"),
                "process_uid": os.getuid(),
                "process_gid": os.getgid(),
                "umask": os.getenv("UMASK", "not set"),
                "running_as_root": os.getuid() == 0,
                "pool_mounts": [
                    {
                        "name": pool.name,
                        "prefix": str(pool.prefix),
                        "writable": all(
                            path.exists() and os.access(path, os.W_OK)
                            for path in (*pool.download_roots, pool.radarr_root, pool.sonarr_root)
                        ),
                        "paths": [
                            {"path": str(path), "writable": path.exists() and os.access(path, os.W_OK)}
                            for path in (*pool.download_roots, pool.radarr_root, pool.sonarr_root)
                        ],
                    }
                    for pool in self.config.pools
                ],
            },
        }

    def update_runtime_settings(self, payload: dict) -> dict:
        apply = payload.get("apply")
        if not isinstance(apply, bool):
            raise ValueError("apply must be a boolean")
        if apply:
            for pool in self.config.pools:
                for root in (*pool.download_roots, pool.radarr_root, pool.sonarr_root):
                    probe = root / f".stowarr-write-test-{secrets.token_hex(6)}"
                    try:
                        probe.write_bytes(b"")
                        probe.unlink()
                    except OSError as error:
                        probe.unlink(missing_ok=True)
                        raise PermissionError(f"Required media path is not writable inside the API container: {root}") from error
        self.store.set_setting("runtime", {"apply": apply})
        self.config = replace(self.config, apply=apply)
        return self.runtime_settings()

    @staticmethod
    def _operation_fingerprint(kind: str, plan: dict, payload: dict) -> str:
        canonical = json.dumps({"kind": kind, "plan": plan, "payload": payload}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def issue_confirmation(self, kind: str, torrent_hash: str, payload: dict) -> dict:
        if kind == "reconcile":
            sources = payload.get("auxiliaryFiles", [])
            if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
                raise ValueError("auxiliaryFiles must be a list of paths")
            normalized = {"auxiliaryFiles": sorted(set(sources))}
            plan = self.plan(torrent_hash).json()
        elif kind == "move":
            target_pool = payload.get("targetPool")
            if not isinstance(target_pool, str) or not target_pool:
                raise ValueError("targetPool is required")
            plan = self.move_plan(torrent_hash, target_pool).json()
            actions = payload.get("additionalFiles", {})
            expected = {item["source"] for item in plan.get("additional_files", [])}
            if not isinstance(actions, dict) or set(actions) != expected:
                raise ValueError("Every additional file must have an explicit move or delete action")
            if any(action not in {"move", "delete"} for action in actions.values()):
                raise ValueError("Additional file actions must be move or delete")
            conflicts = {item["source"] for item in plan.get("additional_files", []) if item["status"] == "target-conflict"}
            if any(actions[source] == "move" for source in conflicts):
                raise ValueError("Conflicting additional files must be deleted or resolved manually")
            normalized = {"targetPool": target_pool, "additionalFiles": dict(sorted(actions.items()))}
        else:
            raise ValueError("kind must be reconcile or move")
        if plan.get("status") != "ready":
            raise RuntimeError(plan.get("reason") or "Operation plan is not ready")
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + 600
        fingerprint = self._operation_fingerprint(kind, plan, normalized)
        self.store.create_confirmation(token, kind, torrent_hash, fingerprint, expires_at)
        return {"token": token, "expires_at": expires_at, "kind": kind, "torrent_hash": torrent_hash, "plan": plan, "payload": normalized}

    def consume_confirmation(self, token: str, kind: str, torrent_hash: str, payload: dict) -> None:
        if not token:
            raise PermissionError("A confirmation token is required")
        if kind == "reconcile":
            normalized = {"auxiliaryFiles": sorted(set(payload.get("auxiliaryFiles", [])))}
            plan = self.plan(torrent_hash).json()
        else:
            actions = payload.get("additionalFiles", {})
            normalized = {"targetPool": payload.get("targetPool"), "additionalFiles": dict(sorted(actions.items()))}
            plan = self.move_plan(torrent_hash, normalized["targetPool"]).json()
        fingerprint = self._operation_fingerprint(kind, plan, normalized)
        self.store.consume_confirmation(token, kind, torrent_hash, fingerprint)

    @staticmethod
    def _torrent_paths(torrent: dict, files: list[dict]) -> list[tuple[Path, int]]:
        save_path = Path(torrent["save_path"])
        result = []
        for item in files:
            path = save_path / item["name"]
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                result.append((path, int(item["size"])))
        return result

    @staticmethod
    def _torrent_sidecars(torrent: dict, files: list[dict], target_item: Path) -> list[AuxiliaryFile]:
        save_path = Path(torrent["save_path"])
        result = []
        for item in files:
            source = save_path / item["name"]
            if source.suffix.lower() in VIDEO_EXTENSIONS or is_archive(source) or source.name.startswith(".!qB"):
                continue
            target = target_item / source.name
            if not target.exists():
                status = "torrent-sidecar"
            elif target.stat().st_size == int(item["size"]):
                status = "target-exists-same-size"
            else:
                status = "target-conflict"
            result.append(
                AuxiliaryFile(
                    str(source), str(target), int(item["size"]), status,
                    "qbittorrent", "hardlink", sidecar_kind(source),
                )
            )
        return result

    @staticmethod
    def _library_files(mapping: dict) -> list[tuple[Path, int]]:
        return [(Path(record["path"]), int(record["size"])) for record in mapping.get("files", [])]

    @staticmethod
    def _target_item_path(item: dict, pool: Pool, app: str) -> Path:
        root = pool.radarr_root if app == "radarr" else pool.sonarr_root
        return root / Path(item["path"]).name

    @staticmethod
    def _target_download_path(current_pool: Pool, target_pool: Pool, save_path: Path) -> Path:
        for root in current_pool.download_roots:
            try:
                relative = save_path.relative_to(root)
                return target_pool.download_roots[0] / relative
            except ValueError:
                continue
        return target_pool.download_roots[0]

    @staticmethod
    def _move_file_record(source: Path, target: Path, scope: str, status: str = "available") -> dict:
        item_size = source.stat().st_size if source.exists() and source.is_file() else 0
        return {
            "source": str(source),
            "target": str(target),
            "scope": scope,
            "kind": sidecar_kind(source),
            "size": item_size,
            "status": status,
            "default_action": "move",
            "sha256": sha256(source) if source.exists() and source.is_file() else None,
        }

    @staticmethod
    def _torrent_content_root(torrent: dict, torrent_files: list[dict]) -> Path | None:
        content_path = str(torrent.get("content_path") or "").strip()
        if content_path:
            return Path(content_path)
        names = [Path(record.get("name", "")) for record in torrent_files if record.get("name")]
        first_parts = {name.parts[0] for name in names if len(name.parts) > 1}
        if len(first_parts) == 1:
            return Path(torrent["save_path"]) / next(iter(first_parts))
        return None

    def _move_inventory(self, torrent: dict, torrent_files: list[dict], mapping: dict, target_pool: Pool, target_save: Path, app: str) -> tuple[list[dict], list[dict]]:
        save_path = Path(torrent["save_path"])
        tracked_paths = {
            save_path / record["name"]
            for record in torrent_files
            if int(record.get("priority", 1)) > 0 and record.get("name")
        }
        tracked = [
            {
                "path": str(path),
                "relative_path": str(path.relative_to(save_path)),
                "size": int(record.get("size", 0)),
                "kind": "archive" if is_archive(path) else "video" if path.suffix.casefold() in VIDEO_EXTENSIONS else sidecar_kind(path),
            }
            for record in torrent_files
            if int(record.get("priority", 1)) > 0 and record.get("name")
            for path in [save_path / record["name"]]
        ]
        additional: list[dict] = []
        content_root = self._torrent_content_root(torrent, torrent_files)
        if content_root and content_root.exists() and content_root.is_dir():
            for source in content_root.rglob("*"):
                if source.is_file() and source not in tracked_paths and not source.name.startswith(".!qB"):
                    target = target_save / source.relative_to(save_path)
                    status = "target-conflict" if target.exists() and sha256(source) != sha256(target) else "available"
                    additional.append(self._move_file_record(source, target, "download", status))
        item = mapping.get("item")
        if item:
            current_item = Path(item["path"])
            target_item = self._target_item_path(item, target_pool, app)
            managed = {Path(record["path"]) for record in mapping.get("files", [])}
            if current_item.exists() and current_item != target_item:
                for source in current_item.rglob("*"):
                    if not source.is_file() or source in managed:
                        continue
                    target = target_item / source.relative_to(current_item)
                    status = "target-conflict" if target.exists() and sha256(source) != sha256(target) else "available"
                    additional.append(self._move_file_record(source, target, "library", status))
        additional.sort(key=lambda item: (item["scope"], item["source"].casefold()))
        return tracked, additional

    def move_plan(self, torrent_hash: str, target_pool_name: str) -> MovePlan:
        torrent = self.qbit.torrent(torrent_hash)
        target_pool = next((pool for pool in self.config.pools if pool.name == target_pool_name), None)
        if not torrent:
            return MovePlan(torrent_hash, "", "unknown", None, target_pool_name, "", None, None, None, None, [], 0, None, "blocked", "Torrent not found")
        if not target_pool:
            return MovePlan(torrent_hash, torrent.get("name", ""), "unknown", None, target_pool_name, torrent.get("save_path", ""), None, None, None, None, [], int(torrent.get("total_size", 0)), None, "blocked", "Target pool is not configured")
        current_pool = self.config.pool_for_path(torrent.get("save_path", ""))
        category_match = self.config.pool_for_category(torrent.get("category", ""))
        app = category_match[1] if category_match else ("sonarr" if "sonarr" in torrent.get("category", "").casefold() else "radarr")
        mapping = self.arr[app].download_mapping(torrent_hash)
        torrent_files = self.qbit.files(torrent_hash)
        archive_count = sum(is_archive(Path(record.get("name", ""))) for record in torrent_files if int(record.get("priority", 1)) > 0)
        video_count = sum(Path(record.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS for record in torrent_files if int(record.get("priority", 1)) > 0)
        content_mode = "mixed" if archive_count and video_count else "archive" if archive_count else "direct" if video_count else "unknown"
        target_category = target_pool.radarr_category if app == "radarr" else target_pool.sonarr_category
        if not current_pool:
            return MovePlan(torrent_hash, torrent["name"], app, None, target_pool.name, torrent.get("save_path", ""), None, target_category, None, None, [], int(torrent.get("total_size", 0)), None, "blocked", "Current qBittorrent save path is outside configured pools")
        target_save = self._target_download_path(current_pool, target_pool, Path(torrent["save_path"]))
        free_space = shutil.disk_usage(target_pool.prefix).free if target_pool.prefix.exists() else None
        size = int(torrent.get("total_size", 0))
        reason = ""
        status = "ready"
        if current_pool.name == target_pool.name:
            status, reason = "blocked", "Torrent is already on the selected pool"
        elif not mapping:
            status, reason = "blocked", "No unique Radarr/Sonarr mapping was found for this torrent"
        elif app == "sonarr" and not mapping.get("mappingComplete"):
            status, reason = "blocked", "Sonarr episode-to-file mapping is incomplete"
        elif content_mode == "unknown":
            status, reason = "blocked", "Torrent contains no supported video or archive content"
        elif archive_count and not self.archive_extractor.available():
            status, reason = "blocked", "Archive extraction requires the 7z executable"
        elif float(torrent.get("progress", 0)) < 1:
            status, reason = "blocked", "Torrent download is incomplete"
        elif str(torrent.get("state", "")).casefold() == "moving" or str(torrent.get("state", "")).casefold().startswith("checking"):
            status, reason = "blocked", "qBittorrent is already moving or checking this torrent"
        extraction_files = []
        if mapping and archive_count:
            save_path = Path(torrent["save_path"])
            direct_records = [
                (save_path / record["name"], int(record.get("size", 0)))
                for record in torrent_files
                if int(record.get("priority", 1)) > 0 and Path(record.get("name", "")).suffix.casefold() in VIDEO_EXTENSIONS
            ]
            for record in mapping.get("files", []):
                source = Path(record["path"])
                candidates = [path for path, candidate_size in direct_records if candidate_size == int(record.get("size", 0))]
                direct_match = source.exists() and any(path.exists() and sha256(path) == sha256(source) for path in candidates)
                if not direct_match:
                    extraction_files.append(record)
        extraction_space = sum(int(record.get("size", 0)) for record in extraction_files)
        required_space = size + extraction_space
        if status == "ready" and free_space is not None and required_space > free_space:
            status, reason = "blocked", "Target pool does not have enough free space"
        item = mapping.get("item") if mapping else None
        managed_files = []
        tracked_files = []
        additional_files = []
        target_item = None
        if item and mapping:
            target_item = self._target_item_path(item, target_pool, app)
            for record in mapping.get("files", []):
                managed_files.append({**record, "targetPath": str(target_item / record["relativePath"])})
            tracked_files, additional_files = self._move_inventory(torrent, torrent_files, mapping, target_pool, target_save, app)
        return MovePlan(
            torrent_hash=torrent_hash,
            torrent_name=torrent.get("name", ""),
            app=app,
            current_pool=current_pool.name,
            target_pool=target_pool.name,
            current_save_path=torrent.get("save_path", ""),
            target_save_path=str(target_save),
            target_category=target_category,
            item_id=item.get("id") if item else None,
            item_title=item.get("title") if item else None,
            managed_files=managed_files,
            torrent_size=size,
            free_space=free_space,
            status=status,
            reason=reason,
            content_mode=content_mode,
            archive_files=archive_count,
            current_item_path=item.get("path") if item else None,
            target_item_path=str(target_item) if target_item else None,
            tracked_files=tracked_files,
            additional_files=additional_files,
            current_content_root=str(self._torrent_content_root(torrent, torrent_files) or "") or None,
            extraction_required=bool(extraction_files),
            extraction_space=extraction_space,
            extraction_files=extraction_files,
        )

    def _wait_for_torrent(self, torrent_hash: str, predicate, timeout: int = 1800, progress=None) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            torrent = self.qbit.torrent(torrent_hash)
            if not torrent:
                raise RuntimeError("Torrent disappeared from qBittorrent during the operation")
            if progress:
                progress(torrent)
            if predicate(torrent):
                return torrent
            time.sleep(2)
        raise RuntimeError(f"qBittorrent operation exceeded {timeout} seconds")

    def _wait_for_recheck(self, torrent_hash: str, progress=None, timeout: int = 1800) -> dict:
        """Wait for qBittorrent's asynchronous check to start and then finish."""
        deadline = time.monotonic() + timeout
        observed_checking = False
        last_state = "unknown"
        while time.monotonic() < deadline:
            torrent = self.qbit.torrent(torrent_hash)
            if not torrent:
                raise RuntimeError("Torrent disappeared while qBittorrent was rechecking it")
            last_state = str(torrent.get("state", ""))
            checking = last_state.casefold().startswith("checking")
            observed_checking = observed_checking or checking
            if progress:
                progress(torrent, observed_checking)
            if observed_checking and not checking:
                if float(torrent.get("progress", 0)) < 1:
                    raise RuntimeError("qBittorrent recheck completed with missing torrent data")
                return torrent
            time.sleep(1)
        if not observed_checking:
            raise RuntimeError(f"qBittorrent never entered a checking state (last state: {last_state})")
        raise RuntimeError(f"qBittorrent recheck exceeded {timeout} seconds (last state: {last_state})")

    @staticmethod
    def _is_seeding_state(torrent: dict) -> bool:
        state = str(torrent.get("state", "")).casefold()
        return float(torrent.get("progress", 0)) >= 1 and (
            state == "uploading" or state.endswith("up")
        ) and not state.startswith(("paused", "stopped", "checking", "moving", "error"))

    def _wait_for_visible_torrent_files(self, torrent_hash: str, timeout: int = 60) -> None:
        deadline = time.monotonic() + timeout
        missing: list[Path] = []
        while time.monotonic() < deadline:
            torrent = self.qbit.torrent(torrent_hash)
            if not torrent:
                raise RuntimeError("Torrent disappeared from qBittorrent after recheck")
            missing = [
                Path(torrent["save_path"]) / record["name"]
                for record in self.qbit.files(torrent_hash)
                if int(record.get("priority", 1)) > 0
                and ".pad" not in Path(record.get("name", "")).parts
                and not (Path(torrent["save_path"]) / record["name"]).is_file()
            ]
            if not missing:
                return
            time.sleep(2)
        sample = ", ".join(str(path) for path in missing[:3])
        raise RuntimeError(
            f"qBittorrent completed recheck but {len(missing)} tracked file(s) are not visible inside Stowarr: {sample}"
        )

    @staticmethod
    def _remove_empty_tree(root: Path | None) -> None:
        if not root or not root.exists() or not root.is_dir():
            return
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda path: len(path.parts), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
        if root.exists() and not any(root.iterdir()):
            root.rmdir()

    @staticmethod
    def _copy_verified(source: Path, target: Path, expected_hash: str) -> None:
        if not source.exists() or sha256(source) != expected_hash:
            raise RuntimeError(f"Additional source changed or disappeared: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256(target) != expected_hash:
                raise RuntimeError(f"Additional file target conflict: {target}")
            return
        temporary = target.with_name(f".{target.name}.stowarr-copy")
        temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        if sha256(temporary) != expected_hash:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"Additional file copy verification failed: {source}")
        os.replace(temporary, target)

    def _extract_managed_media(self, torrent_hash: str, plan: MovePlan, progress=None) -> list[dict]:
        """Regenerate archive-derived *Arr files on the destination pool."""
        torrent = self.qbit.torrent(torrent_hash)
        if not torrent:
            raise RuntimeError("Torrent disappeared before archive extraction")
        records = self.qbit.files(torrent_hash)
        save_path = Path(torrent["save_path"])
        paths = [
            save_path / record["name"] for record in records
            if int(record.get("priority", 1)) > 0 and is_archive(Path(record["name"]))
        ]
        entries = select_archive_entries(paths)
        target_item = Path(plan.target_item_path or "")
        if not target_item.is_absolute():
            raise RuntimeError("Archive extraction has no valid destination library folder")
        staging = target_item.parent / f".stowarr-extract-{torrent_hash.casefold()}-{secrets.token_hex(4)}"
        extracted: list[Path] = []
        published: list[dict] = []
        try:
            declared_members = []
            for entry in entries:
                declared_members.extend(self.archive_extractor.members(entry))
            if len(declared_members) > 100_000:
                raise RuntimeError("Archive extraction is blocked because the manifest contains more than 100,000 files")
            declared_size = sum(member.size for member in declared_members)
            free_budget = max(0, int(plan.free_space or 0) - int(plan.torrent_size)) if plan.free_space is not None else None
            expected_budget = max(int(plan.extraction_space) * 4, int(plan.extraction_space) + 2 * 1024**3)
            if declared_size > expected_budget or (free_budget is not None and declared_size > free_budget):
                raise RuntimeError(
                    f"Archive extraction is blocked because its declared size ({declared_size} bytes) exceeds the safe budget"
                )
            for index, entry in enumerate(entries):
                output = staging / f"archive-{index:04d}"
                archive_progress = lambda percent, index=index, entry=entry: progress and progress({
                        "percent": round((index + percent / 100) / len(entries) * 55),
                        "current": entry.name,
                        "message": f"Extracting archive {index + 1} of {len(entries)}",
                    })
                files = (
                    self.archive_extractor.extract(entry, output, archive_progress)
                    if progress else self.archive_extractor.extract(entry, output)
                )
                extracted.extend(item.path for item in files)
            videos = [path for path in extracted if path.suffix.casefold() in VIDEO_EXTENSIONS]
            if not videos:
                raise RuntimeError("Archive extraction produced no supported media files")
            used: set[Path] = set()
            extraction_ids = {record.get("id") for record in (plan.extraction_files or [])}
            media_records = [record for record in plan.managed_files if record.get("id") in extraction_ids]
            for media_index, record in enumerate(media_records):
                if record.get("id") not in extraction_ids:
                    continue
                source = Path(record["path"])
                target = Path(record["targetPath"])
                expected_size = int(record.get("size", 0))
                if not source.exists():
                    raise RuntimeError(f"Cannot verify regenerated media because the current *Arr file is missing: {source}")
                def hash_progress(completed, total, label="current library media"):
                    if progress:
                        progress({
                            "percent": 55 + round((media_index + (completed / max(total, 1)) * .25) / max(len(media_records), 1) * 35),
                            "completed_bytes": completed, "total_bytes": total,
                            "current": source.name, "message": f"Hash-verifying {label}",
                        })
                expected_hash = sha256(source, progress=hash_progress)
                candidates = []
                for candidate in videos:
                    if candidate in used or candidate.stat().st_size != expected_size:
                        continue
                    if sha256(candidate, progress=lambda done, total: hash_progress(done, total, "extracted media")) == expected_hash:
                        candidates.append(candidate)
                if len(candidates) != 1:
                    raise RuntimeError(
                        f"Archive extraction did not produce exactly one verified match for {source}; found {len(candidates)}"
                    )
                candidate = candidates[0]
                used.add(candidate)
                target.parent.mkdir(parents=True, exist_ok=True)
                existed_before = target.exists()
                if existed_before:
                    if sha256(target) != expected_hash:
                        raise RuntimeError(f"Existing archive-derived target differs from verified media: {target}")
                else:
                    temporary = target.with_name(f".{target.name}.stowarr-extract")
                    temporary.unlink(missing_ok=True)
                    shutil.copy2(candidate, temporary)
                    if sha256(temporary, progress=lambda done, total: hash_progress(done, total, "published media")) != expected_hash:
                        temporary.unlink(missing_ok=True)
                        raise RuntimeError(f"Published archive-derived media failed verification: {target}")
                    os.replace(temporary, target)
                published.append({
                    "source": str(source), "target": str(target), "sha256": expected_hash,
                    "created": not existed_before,
                })
                if progress:
                    progress({"percent": 90 + round((media_index + 1) / max(len(media_records), 1) * 10),
                              "current": target.name, "message": "Published verified archive media"})
            return published
        except Exception:
            for item in published:
                if item.get("created"):
                    target = Path(item["target"])
                    if target.exists() and sha256(target) == item["sha256"]:
                        target.unlink()
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _cleanup_verified_unpackerr_derivatives(self, torrent_hash: str, extracted: list[dict], progress=None) -> list[str]:
        """Remove only recognizable Unpackerr output whose media matches published library media."""
        if not extracted:
            return []
        torrent = self.qbit.torrent(torrent_hash)
        if not torrent:
            raise RuntimeError("Torrent disappeared before Unpackerr derivative cleanup")
        root = Path(torrent["save_path"])
        torrent_name = str(torrent.get("name", ""))
        expected = {(Path(item["target"]).stat().st_size, item["sha256"]) for item in extracted}
        candidates = [
            path for path in root.glob(f"{torrent_name}*_unpackerr*")
            if path.is_dir() and path.name.casefold().endswith(("_unpackerrred", "_unpacked"))
        ]
        removed: list[str] = []
        for index, directory in enumerate(candidates):
            files = [path for path in directory.rglob("*") if path.is_file()]
            markers = [path for path in files if path.name.casefold().startswith("_unpackerr") and path.suffix.casefold() == ".txt"]
            videos = [path for path in files if path.suffix.casefold() in VIDEO_EXTENSIONS]
            unknown = [path for path in files if path not in markers and path not in videos]
            if not markers or not videos or unknown:
                continue
            for video in videos:
                possible = {digest for size, digest in expected if size == video.stat().st_size}
                if not possible:
                    raise RuntimeError(f"Unpackerr derivative has no matching published library file: {video}")
                digest = sha256(video, progress=lambda done, total, video=video: progress and progress({
                    "percent": round((index + done / max(total, 1)) / max(len(candidates), 1) * 100),
                    "completed_bytes": done, "total_bytes": total, "current": video.name,
                    "message": "Hash-verifying Unpackerr derivative before cleanup",
                }))
                if digest not in possible:
                    raise RuntimeError(f"Unpackerr derivative differs from published library media: {video}")
            shutil.rmtree(directory)
            removed.append(str(directory))
        return removed

    def move(self, torrent_hash: str, target_pool_name: str, additional_actions: dict[str, str] | None = None) -> dict:
        if self.store.active(torrent_hash, kind="move"):
            raise RuntimeError("Another Move operation is already active for this torrent")
        plan = self.move_plan(torrent_hash, target_pool_name)
        actions = additional_actions or {}
        expected_actions = {item["source"] for item in (plan.additional_files or [])}
        if set(actions) != expected_actions or any(action not in {"move", "delete"} for action in actions.values()):
            raise ValueError("Every additional file must have an explicit move or delete action")
        conflicts = {item["source"] for item in (plan.additional_files or []) if item["status"] == "target-conflict"}
        if any(actions[source] == "move" for source in conflicts):
            raise ValueError("Conflicting additional files must be deleted or resolved manually")
        detail = {**plan.json(), "additional_actions": dict(sorted(actions.items()))}
        operation_id = self.store.record(torrent_hash, plan.app, "MOVE_PLANNED", detail, kind="move")
        if plan.status != "ready" or not self.config.apply:
            state = "BLOCKED" if plan.status != "ready" else "DRY_RUN"
            self.store.update(operation_id, state, detail)
            return {"operation_id": operation_id, "state": state, "plan": plan.json()}

        extracted: list[dict] = []
        temporary_category = f"{plan.app}-stowarr-moving-{torrent_hash[:12].casefold()}"
        last_progress: tuple[str, int, str, str] | None = None
        def report(state: str, percent: float, **values) -> None:
            nonlocal last_progress
            percent_value = max(0, min(100, round(percent)))
            marker = (state, percent_value, str(values.get("current", "")), str(values.get("message", "")))
            if marker == last_progress:
                return
            last_progress = marker
            payload = {
                **detail, "temporary_category": temporary_category,
                "progress": {"state": state, "percent": percent_value, **values},
            }
            self.store.update(operation_id, state, payload)
        self.qbit.pause(torrent_hash)
        report("MOVE_PAUSED", 100, message="Torrent writes are paused")
        try:
            self.qbit.ensure_category(temporary_category, plan.target_save_path or "")
            self.qbit.set_category(torrent_hash, temporary_category)
            report("MOVE_ISOLATED", 100, message="Temporary category assigned")
            self.qbit.set_location(torrent_hash, plan.target_save_path or "")
            report("MOVE_RELOCATING", 0, message="qBittorrent is relocating tracked data")
            self._wait_for_torrent(
                torrent_hash,
                lambda torrent: Path(torrent.get("save_path", "")) == Path(plan.target_save_path or "")
                and torrent.get("state") != "moving",
                progress=lambda torrent: report(
                    "MOVE_RELOCATING", 100 if Path(torrent.get("save_path", "")) == Path(plan.target_save_path or "")
                    and torrent.get("state") != "moving" else 0,
                    qbit_state=torrent.get("state", ""), message="qBittorrent is relocating tracked data",
                ),
            )
            self.qbit.recheck(torrent_hash)
            report("MOVE_RECHECKING", 0, message="Waiting for qBittorrent to start rechecking")
            self._wait_for_recheck(
                torrent_hash,
                progress=lambda torrent, started: report(
                    "MOVE_RECHECKING", float(torrent.get("progress", 0)) * 100 if started else 0,
                    qbit_state=torrent.get("state", ""),
                    message="qBittorrent is checking torrent pieces" if started else "Waiting for qBittorrent to enter checking state",
                ),
            )
            self._wait_for_visible_torrent_files(torrent_hash)
            report("MOVE_QBIT_COMPLETE", 100, message="Every selected qBittorrent file is visible at the destination")
            if plan.extraction_required:
                report("MOVE_EXTRACTING", 0, message="Inspecting archive manifests")
                extracted = self._extract_managed_media(torrent_hash, plan, lambda value: report("MOVE_EXTRACTING", **value))
                report("MOVE_EXTRACTED", 100, message="Archive media extracted and hash-verified")
            download_records = [item for item in (plan.additional_files or []) if item["scope"] == "download"]
            report("MOVE_ADDITIONAL_VERIFYING", 0, total_files=len(download_records), message="Verifying additional download files")
            for item_index, item in enumerate(download_records):
                source, target = Path(item["source"]), Path(item["target"])
                expected_hash = item.get("sha256") or ""
                if actions[item["source"]] == "move":
                    if source.exists():
                        self._copy_verified(source, target, expected_hash)
                    elif not target.exists() or sha256(target) != expected_hash:
                        raise RuntimeError(f"Additional download file was not preserved by qBittorrent: {source}")
                report("MOVE_ADDITIONAL_VERIFYING", (item_index + 1) / max(len(download_records), 1) * 100,
                       completed_files=item_index + 1, total_files=len(download_records), current=source.name,
                       message="Verified additional download file")
            report("MOVE_ADDITIONAL_VERIFIED", 100, completed_files=len(download_records), total_files=len(download_records),
                   message="All selected additional download files are verified")
            report("MOVE_LIBRARY_VERIFYING", 0, message="Resolving the destination library plan")
            verified_derived = {item["target"] for item in extracted}
            post_plan = self.plan(torrent_hash, verified_derived_paths=verified_derived)
            selected_auxiliary = {
                item.source
                for item in (post_plan.auxiliary_files or [])
                if item.origin == "qbittorrent" or actions.get(item.source) == "move"
            }
            result = self.reconcile(torrent_hash, selected_auxiliary, operation_id=operation_id,
                                    verified_derived_paths=verified_derived, progress_callback=report)
            if result["state"] != "COMPLETE":
                raise RuntimeError(f'Reconciliation did not complete after qBittorrent move: {result["state"]}')
            report("MOVE_DERIVATIVE_CLEANUP", 0, message="Checking for verified Unpackerr derivatives")
            removed_derivatives = self._cleanup_verified_unpackerr_derivatives(
                torrent_hash, extracted, lambda value: report("MOVE_DERIVATIVE_CLEANUP", **value)
            )
            report("MOVE_DERIVATIVE_CLEANUP", 100, completed_files=len(removed_derivatives),
                   total_files=len(removed_derivatives), message="Verified Unpackerr derivatives cleaned")
            for item in plan.additional_files or []:
                source, target = Path(item["source"]), Path(item["target"])
                if actions[item["source"]] == "delete":
                    source.unlink(missing_ok=True)
                    if item["scope"] == "download":
                        target.unlink(missing_ok=True)
                elif item["scope"] == "download" and source.exists():
                    if not target.exists() or sha256(source) != sha256(target):
                        raise RuntimeError(f"Final additional file verification failed: {source}")
                    source.unlink()
            self._remove_empty_tree(Path(plan.current_item_path) if plan.current_item_path else None)
            content_root = Path(plan.current_content_root) if plan.current_content_root else None
            if content_root and content_root != Path(plan.current_save_path):
                self._remove_empty_tree(content_root)
            old_item = Path(plan.current_item_path) if plan.current_item_path else None
            if old_item and old_item.exists():
                raise RuntimeError(f"Old library folder still contains unclassified files: {old_item}")
            self.qbit.set_category(torrent_hash, plan.target_category or "")
            self.store.update(operation_id, "MOVE_ROUTE_COMMITTED", detail)
            self.qbit.resume(torrent_hash)
            self.store.update(operation_id, "MOVE_RESUMING", detail)
            resumed = self._wait_for_torrent(torrent_hash, self._is_seeding_state, timeout=120)
            self.store.update(operation_id, "MOVE_SEEDING", {**detail, "qbittorrent_state": resumed.get("state", "")})
            self.qbit.delete_category(temporary_category)
            self.store.update(operation_id, "COMPLETE", {**detail, "extracted_files": extracted,
                              "removed_unpackerr_derivatives": removed_derivatives,
                              "reconcile_operation_id": result["operation_id"]})
            return {"operation_id": operation_id, "state": "COMPLETE", "plan": plan.json(), "reconcile": result}
        except Exception as error:
            for item in extracted:
                if item.get("created"):
                    target = Path(item["target"])
                    if target.exists() and sha256(target) == item["sha256"]:
                        target.unlink()
            previous = next((item for item in self.store.recent() if item["id"] == operation_id), None)
            self.store.update(operation_id, "FAILED", {
                **detail,
                "error": str(error),
                "failed_after": previous["state"] if previous else "MOVE_PLANNED",
                "temporary_category": temporary_category,
                "recovery": "Torrent is intentionally left paused and isolated in qBittorrent for manual recovery.",
            })
            raise

    def plan(self, torrent_hash: str, verified_derived_paths: set[str] | None = None) -> Plan:
        verified_derived_paths = verified_derived_paths or set()
        torrent = next((t for t in self.qbit.torrents() if t["hash"].lower() == torrent_hash.lower()), None)
        if not torrent:
            return Plan(torrent_hash, "", "unknown", "", None, None, None, None, [], "blocked", "Torrent not found")
        pool = self.config.pool_for_path(torrent["save_path"])
        category_match = self.config.pool_for_category(torrent.get("category", ""))
        if not pool:
            return Plan(torrent_hash, torrent["name"], "unknown", "", None, None, None, None, [], "blocked", "Save path is outside configured pools")
        app = category_match[1] if category_match else ("sonarr" if torrent.get("category", "").startswith("sonarr") else "radarr")
        mapping = self.arr[app].download_mapping(torrent_hash)
        if not mapping:
            return Plan(torrent_hash, torrent["name"], app, pool.name, None, None, None, None, [], "blocked", "No matching *Arr history item")
        item = mapping["item"]
        if app == "sonarr" and not mapping.get("mappingComplete"):
            return Plan(
                torrent_hash, torrent["name"], app, pool.name, item["id"], item.get("title"),
                item.get("path"), None, [], "blocked",
                "Sonarr history does not provide a complete episode-to-file mapping for this download",
                "SONARR_DOWNLOAD_MAPPING_INCOMPLETE",
                {
                    "series_id": item["id"],
                    "episode_ids": [episode["id"] for episode in mapping.get("episodes", [])],
                    "episode_file_ids": [record["id"] for record in mapping.get("files", [])],
                    "action": "Refresh or rescan the series in Sonarr, then analyze the torrent again.",
                },
            )
        target_item = self._target_item_path(item, pool, app)
        torrent_records = self.qbit.files(torrent_hash)
        torrent_files = self._torrent_paths(torrent, torrent_records)
        has_archives = any(
            int(record.get("priority", 1)) > 0 and is_archive(Path(record["name"]))
            for record in torrent_records
        )
        library_files = self._library_files(mapping)
        if not title_matches(item.get("title", ""), Path(item["path"]).name):
            item_title = item.get("title") or "<unknown>"
            folder_name = Path(item["path"]).name
            return Plan(
                torrent_hash, torrent["name"], app, pool.name, item["id"], item_title,
                item["path"], str(target_item), [], "blocked",
                (
                    f'{app.capitalize()} item "{item_title}" points to library folder '
                    f'"{folder_name}", whose name does not match the title. Correct the '
                    f'movie/series path in {app.capitalize()} before retrying.'
                ),
                "ARR_LIBRARY_FOLDER_TITLE_MISMATCH",
                {
                    "arr_title": item_title,
                    "arr_item_id": item["id"],
                    "current_library_path": item["path"],
                    "current_folder_name": folder_name,
                    "torrent_name": torrent["name"],
                    "action": f"Correct or rename the item path in {app.capitalize()}, then run the plan again.",
                },
            )
        names_for_validation = [torrent["name"], *(path.name for path, _ in library_files)]
        if not title_matches(item.get("title", ""), *names_for_validation):
            item_title = item.get("title") or "<unknown>"
            return Plan(
                torrent_hash, torrent["name"], app, pool.name, item["id"], item_title,
                item["path"], str(target_item), [], "blocked",
                (
                    f'{app.capitalize()} item "{item_title}" does not match torrent '
                    f'"{torrent["name"]}" or its library filenames. Verify the download '
                    f'association in {app.capitalize()} before retrying.'
                ),
                "ARR_DOWNLOAD_TITLE_MISMATCH",
                {
                    "arr_title": item_title,
                    "arr_item_id": item["id"],
                    "current_library_path": item["path"],
                    "torrent_name": torrent["name"],
                    "library_files": [str(path) for path, _ in library_files],
                    "action": f"Verify the download association in {app.capitalize()}, then run the plan again.",
                },
            )
        pairs: list[FilePair] = []
        used: set[Path] = set()
        for source, size in library_files:
            candidates = [(path, item_size) for path, item_size in torrent_files if item_size == size and path not in used]
            relative = source.relative_to(Path(item["path"]))
            target = target_item / relative
            if not candidates and has_archives:
                if source == target and source.exists():
                    state = "already-on-target"
                elif str(target) in verified_derived_paths and target.exists() and target.stat().st_size == size:
                    state = "verified-derived"
                elif source.exists() and target.exists() and source.stat().st_size == target.stat().st_size and sha256(source) == sha256(target):
                    state = "verified-derived"
                elif not source.exists():
                    state = "missing-derived-media"
                else:
                    source_pool = self.config.pool_for_path(source)
                    if source_pool and source_pool.name != pool.name and source.stat().st_nlink > 1:
                        state = "unknown-hardlinks"
                    else:
                        state = "reextract-required"
                pairs.append(FilePair(str(source), str(target), "", size, state, "archive-reextract"))
                continue
            if len(candidates) != 1:
                pairs.append(FilePair(str(source), "", "", size, "ambiguous", "unresolved"))
                continue
            torrent_file, _ = candidates[0]
            used.add(torrent_file)
            if target.exists() and torrent_file.exists():
                target_stat, torrent_stat = target.stat(), torrent_file.stat()
                state = "linked" if (target_stat.st_dev, target_stat.st_ino) == (torrent_stat.st_dev, torrent_stat.st_ino) else "duplicate"
            elif torrent_file.exists():
                source_pool = self.config.pool_for_path(source)
                if source.exists() and source_pool and source_pool.name != pool.name and source.stat().st_nlink > 1:
                    state = "unknown-hardlinks"
                else:
                    state = "repairable"
            else:
                state = "missing-torrent-data"
            pairs.append(FilePair(str(source), str(target), str(torrent_file), size, state, "hardlink"))
        blocked = next(
            (
                pair.status for pair in pairs
                if pair.status in {
                    "ambiguous", "missing-torrent-data", "missing-derived-media",
                    "unknown-hardlinks", "reextract-required",
                }
            ),
            None,
        )
        blocked_reasons = {
            "ambiguous": "No unique media mapping could be established",
            "missing-torrent-data": "The qBittorrent media file is missing",
            "missing-derived-media": "The imported media derived from the archive set is missing",
            "unknown-hardlinks": "The old library file has additional unknown hardlinks",
            "reextract-required": (
                "Packed torrent media is on the wrong pool and must be regenerated from "
                "qBittorrent-owned archives with Stowarr's verified extractor"
            ),
        }
        source_videos = {path for path, _ in library_files}
        auxiliary_files = self._torrent_sidecars(torrent, torrent_records, target_item)
        torrent_targets = {Path(sidecar.target) for sidecar in auxiliary_files}
        current_root = Path(item["path"])
        if current_root.exists() and current_root != target_item:
            for source in current_root.rglob("*"):
                if not source.is_file() or source in source_videos:
                    continue
                relative = source.relative_to(current_root)
                target = target_item / relative
                if target in torrent_targets:
                    status = "torrent-name-conflict"
                elif not target.exists():
                    status = "missing-target"
                elif target.stat().st_size == source.stat().st_size:
                    status = "target-exists-same-size"
                else:
                    status = "target-conflict"
                auxiliary_files.append(
                    AuxiliaryFile(
                        str(source), str(target), source.stat().st_size, status,
                        "library", "copy", sidecar_kind(source),
                    )
                )
        return Plan(
            torrent_hash=torrent_hash,
            torrent_name=torrent["name"], app=app, target_pool=pool.name,
            item_id=item["id"], item_title=item.get("title"), current_item_path=item["path"], target_item_path=str(target_item),
            pairs=pairs, status="blocked" if blocked else "ready", reason=blocked_reasons.get(blocked, ""),
            auxiliary_files=auxiliary_files,
            managed_files=mapping.get("files", []),
        )

    def sync_audit(self, app: str) -> dict:
        if app not in self.arr:
            raise ValueError(f"Unsupported application: {app}")
        category_names = {
            pool.radarr_category if app == "radarr" else pool.sonarr_category
            for pool in self.config.pools
        }
        torrents = [
            torrent for torrent in self.qbit.torrents()
            if torrent.get("category", "") in category_names
            or app in torrent.get("category", "").casefold()
        ]
        hashes = {torrent["hash"].casefold() for torrent in torrents}
        history = self.arr[app].history_for_downloads(hashes)
        items = {int(item["id"]): item for item in self.arr[app].all_items()}
        rows = []
        for torrent in torrents:
            torrent_hash = torrent["hash"].casefold()
            pool = self.config.pool_for_path(torrent.get("save_path", ""))
            category_pool = self.config.pool_for_category(torrent.get("category", ""))
            item_id = history.get(torrent_hash)
            item = items.get(item_id) if item_id else None
            expected_root = None
            if pool:
                expected_root = pool.radarr_root if app == "radarr" else pool.sonarr_root
            if not item_id:
                status, reason = "missing-history", "Hash was not found in recent *Arr history"
            elif not item:
                status, reason = "missing-item", "History points to an item that no longer exists"
            elif not pool:
                status, reason = "outside-pool", "qBittorrent save path is outside configured pools"
            elif category_pool and category_pool[0].name != pool.name:
                status, reason = "category-mismatch", "qBittorrent category and save path select different pools"
            elif not Path(item.get("path", "")).is_relative_to(expected_root):
                status, reason = "root-mismatch", f"*Arr path is not below the {pool.name} root"
            else:
                status, reason = "in-sync", "Hash, category, save path and *Arr root agree"
            rows.append({
                "hash": torrent["hash"],
                "torrent_name": torrent.get("name", ""),
                "category": torrent.get("category", ""),
                "save_path": torrent.get("save_path", ""),
                "qbit_pool": pool.name if pool else None,
                "item_id": item_id,
                "item_title": item.get("title") if item else None,
                "arr_path": item.get("path") if item else None,
                "expected_root": str(expected_root) if expected_root else None,
                "status": status,
                "reason": reason,
            })
        rows.sort(key=lambda row: (row["status"] == "in-sync", row["torrent_name"].casefold()))
        return {
            "app": app,
            "scanned": len(rows),
            "matched_history": len(history),
            "in_sync": sum(row["status"] == "in-sync" for row in rows),
            "issues": sum(row["status"] != "in-sync" for row in rows),
            "rows": rows,
        }

    def qbit_search(self, query: str, limit: int = 100) -> dict:
        """Search qBittorrent directly without consulting Radarr or Sonarr."""
        needle = query.strip().casefold()
        if len(needle) < 2:
            raise ValueError("Search must contain at least two characters")
        rows = []
        for torrent in self.qbit.torrents():
            torrent_hash = str(torrent.get("hash", ""))
            name = str(torrent.get("name", ""))
            category = str(torrent.get("category", ""))
            if needle not in torrent_hash.casefold() and needle not in name.casefold() and needle not in category.casefold():
                continue
            pool = self.config.pool_for_path(torrent.get("save_path", ""))
            rows.append({
                "hash": torrent_hash,
                "name": name,
                "category": category,
                "save_path": torrent.get("save_path", ""),
                "pool": pool.name if pool else None,
                "state": torrent.get("state", "unknown"),
                "progress": float(torrent.get("progress", 0)),
                "size": int(torrent.get("total_size", 0)),
                "_rank": 0 if name.casefold().startswith(needle) else 1 if needle in name.casefold() else 2 if needle in category.casefold() else 3,
            })
        rows.sort(key=lambda row: (row["_rank"], row["name"].casefold()))
        for row in rows:
            row.pop("_rank", None)
        return {"query": query, "matches": len(rows), "rows": rows[:limit]}

    def qbit_catalog(self) -> dict:
        """Group torrents by configured *Arr category routes, then by actual save path."""
        routes = []
        route_by_category = {}
        for app in ("radarr", "sonarr"):
            for pool in self.config.pools:
                category = pool.radarr_category if app == "radarr" else pool.sonarr_category
                tag = pool.radarr_tag if app == "radarr" else pool.sonarr_tag
                root = pool.radarr_root if app == "radarr" else pool.sonarr_root
                route = {
                    "app": app, "pool": pool.name, "category": category,
                    "tag": tag, "root": str(root), "paths": {},
                }
                routes.append(route)
                route_by_category[category] = route
        groups: dict[str, dict] = {
            pool.name: {"pool": pool.name, "prefix": str(pool.prefix), "download_roots": pool.download_roots, "paths": {}}
            for pool in self.config.pools
        }
        groups["outside"] = {"pool": None, "prefix": None, "download_roots": (), "paths": {}}
        total = 0
        for torrent in self.qbit.torrents():
            total += 1
            save_path = str(torrent.get("save_path", ""))
            pool = self.config.pool_for_path(save_path)
            row = {
                "hash": str(torrent.get("hash", "")),
                "name": str(torrent.get("name", "")),
                "category": str(torrent.get("category", "")),
                "save_path": save_path,
                "pool": pool.name if pool else None,
                "state": torrent.get("state", "unknown"),
                "progress": float(torrent.get("progress", 0)),
                "size": int(torrent.get("total_size", 0)),
            }
            route = route_by_category.get(row["category"])
            if route:
                row["route_status"] = "aligned" if pool and pool.name == route["pool"] else "path-mismatch"
                route["paths"].setdefault(save_path or "<empty save path>", []).append(row)
            else:
                key = pool.name if pool else "outside"
                row["route_status"] = "unmanaged"
                groups[key]["paths"].setdefault(save_path or "<empty save path>", []).append(row)
        route_result = []
        for route in routes:
            paths = [
                {"path": path, "count": len(rows), "torrents": sorted(rows, key=lambda row: row["name"].casefold())}
                for path, rows in sorted(route["paths"].items())
            ]
            route_result.append({**route, "count": sum(path["count"] for path in paths), "paths": paths})
        result = []
        for group in groups.values():
            paths = []
            for path, rows in group["paths"].items():
                candidate = Path(path)
                route = "download" if any(candidate == root or candidate.is_relative_to(root) for root in group["download_roots"]) else "other"
                paths.append({"path": path, "route": route, "count": len(rows), "torrents": sorted(rows, key=lambda row: row["name"].casefold())})
            paths.sort(key=lambda item: (item["route"] != "download", item["path"].casefold()))
            group_total = sum(path["count"] for path in paths)
            if group_total:
                result.append({"pool": group["pool"], "prefix": group["prefix"], "count": group_total, "paths": paths})
        return {"total": total, "routes": route_result, "unmanaged": result}

    @staticmethod
    def _client_field(client: dict, name: str):
        return next((field.get("value") for field in client.get("fields", []) if field.get("name") == name), None)

    def routing_audit(self) -> dict:
        """Compare Stowarr routing expectations with live *Arr and qBittorrent configuration."""
        qbit_categories = self.qbit.categories()
        services = []
        issues = []
        for app in ("radarr", "sonarr"):
            client = self.arr[app]
            tags = client.tags()
            tags_by_label = {record["label"]: int(record["id"]) for record in tags}
            tags_by_id = {int(record["id"]): record["label"] for record in tags}
            roots = client.root_folders()
            root_paths = {record.get("path") for record in roots}
            download_clients = client.download_clients()
            category_field = "movieCategory" if app == "radarr" else "tvCategory"
            routes = []
            for pool in self.config.pools:
                category = pool.radarr_category if app == "radarr" else pool.sonarr_category
                tag = pool.radarr_tag if app == "radarr" else pool.sonarr_tag
                root = str(pool.radarr_root if app == "radarr" else pool.sonarr_root)
                expected_tag_id = tags_by_label.get(tag)
                matching_clients = [record for record in download_clients if self._client_field(record, category_field) == category]
                sanitized_clients = [{
                    "id": record.get("id"),
                    "name": record.get("name"),
                    "enabled": bool(record.get("enable")),
                    "category": self._client_field(record, category_field),
                    "tags": [tags_by_id.get(int(value), f"unknown:{value}") for value in record.get("tags", [])],
                } for record in matching_clients]
                route_issues = []
                if expected_tag_id is None:
                    route_issues.append(f"Missing {app.capitalize()} tag: {tag}")
                if root not in root_paths:
                    route_issues.append(f"Missing {app.capitalize()} root folder: {root}")
                if len(matching_clients) != 1:
                    route_issues.append(f"Expected exactly one download client with category {category}")
                else:
                    selected = matching_clients[0]
                    if not selected.get("enable"):
                        route_issues.append(f"Download client {selected.get('name')} is disabled")
                    if expected_tag_id is not None and expected_tag_id not in selected.get("tags", []):
                        route_issues.append(f"Download client {selected.get('name')} is not restricted by tag {tag}")
                qbit_category = qbit_categories.get(category)
                if qbit_category is None:
                    route_issues.append(f"Missing qBittorrent category: {category}")
                elif not qbit_category.get("savePath"):
                    route_issues.append(f"qBittorrent category {category} has no save path")
                elif Path(qbit_category["savePath"]) not in pool.download_roots:
                    route_issues.append(f"qBittorrent category {category} points to {qbit_category['savePath']}")
                route = {
                    "app": app, "pool": pool.name, "category": category, "tag": tag, "root": root,
                    "download_roots": [str(path) for path in pool.download_roots],
                    "download_clients": sanitized_clients,
                    "qbit_save_path": qbit_category.get("savePath") if qbit_category else None,
                    "status": "ready" if not route_issues else "incomplete",
                    "issues": route_issues,
                }
                routes.append(route)
                issues.extend(route_issues)
            services.append({"app": app, "routes": routes})
        return {"status": "ready" if not issues else "incomplete", "issue_count": len(issues), "services": services}

    def verify(self, torrent_hash: str) -> dict:
        plan = self.plan(torrent_hash)
        if plan.status == "blocked":
            return {"status": "blocked", "plan": plan.json(), "video_files": [], "sidecar_files": []}
        digest_cache: dict[tuple[int, int, int], str] = {}

        def digest(path_value: str) -> dict:
            path = Path(path_value)
            if not path.exists():
                return {"path": path_value, "exists": False, "sha256": None}
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino, stat.st_size)
            value = digest_cache.setdefault(identity, sha256(path))
            return {
                "path": path_value,
                "exists": True,
                "sha256": value,
                "size": stat.st_size,
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "links": stat.st_nlink,
            }

        videos = []
        mismatches = []
        for pair in plan.pairs:
            old_library = digest(pair.source_library)
            new_library = digest(pair.target_library)
            torrent = digest(pair.torrent_file) if pair.torrent_file else None
            if pair.strategy == "hardlink":
                old_matches = bool(torrent["sha256"] and torrent["sha256"] == old_library["sha256"])
                new_matches = None if not new_library["exists"] else torrent["sha256"] == new_library["sha256"]
            else:
                old_matches = None
                new_matches = None if not new_library["exists"] else old_library["sha256"] == new_library["sha256"]
            if old_matches is False or new_matches is False:
                mismatches.append(pair.source_library)
            videos.append({
                "strategy": pair.strategy,
                "torrent": torrent,
                "old_library": old_library,
                "new_library": new_library,
                "old_matches_torrent": old_matches,
                "new_matches_torrent": new_matches,
            })
        sidecars = []
        for item in plan.auxiliary_files or []:
            source = digest(item.source)
            target = digest(item.target)
            matches = None if not target["exists"] else source["sha256"] == target["sha256"]
            sidecars.append({**asdict(item), "source_hash": source, "target_hash": target, "matches_target": matches})
        return {
            "status": "mismatch" if mismatches else "verified",
            "torrent_hash": torrent_hash,
            "video_files": videos,
            "sidecar_files": sidecars,
            "mismatches": mismatches,
        }

    def reconcile(
        self,
        torrent_hash: str,
        auxiliary_sources: set[str] | None = None,
        operation_id: int | None = None,
        verified_derived_paths: set[str] | None = None,
        progress_callback=None,
    ) -> dict:
        plan = self.plan(torrent_hash, verified_derived_paths=verified_derived_paths)
        selected_auxiliary = auxiliary_sources or set()
        blocked_sidecars = {"target-conflict", "torrent-name-conflict"}
        allowed_auxiliary = {
            item.source for item in (plan.auxiliary_files or []) if item.status not in blocked_sidecars
        }
        invalid_auxiliary = selected_auxiliary - allowed_auxiliary
        if invalid_auxiliary:
            raise ValueError("One or more selected sidecar paths are not eligible in the current plan")
        nested_move = operation_id is not None
        if operation_id is None:
            operation_id = self.store.record(torrent_hash, plan.app, "PLANNED", plan.json())
        state_name = lambda standalone, move: move if nested_move else standalone
        if plan.status != "ready" or not self.config.apply:
            state = "BLOCKED" if plan.status != "ready" else "DRY_RUN"
            if nested_move:
                raise RuntimeError(f"Move library reconciliation is blocked: {plan.reason or state}")
            self.store.update(operation_id, state, plan.json())
            return {"operation_id": operation_id, "state": state, "plan": plan.json()}

        created: list[Path] = []
        copied_auxiliary: list[tuple[Path, Path]] = []
        try:
            pair_total = max(len(plan.pairs), 1)
            for pair_index, pair in enumerate(plan.pairs):
                source = Path(pair.source_library)
                target = Path(pair.target_library)
                def pair_progress(completed, total, label="library media"):
                    if progress_callback:
                        progress_callback(
                            "MOVE_LIBRARY_VERIFYING",
                            (pair_index + completed / max(total, 1)) / pair_total * 100,
                            completed_bytes=completed, total_bytes=total, current=source.name,
                            message=f"Hash-verifying {label}",
                        )
                if pair.status in {"linked", "already-on-target", "verified-derived"}:
                    continue
                if pair.strategy == "verified-copy":
                    if not source.exists() or source.stat().st_size != pair.size:
                        raise RuntimeError(f"Derived media changed or missing: {source}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.stowarr-copy")
                    shutil.copy2(source, temporary)
                    if sha256(source, progress=lambda done, total: pair_progress(done, total, "derived source")) != sha256(
                        temporary, progress=lambda done, total: pair_progress(done, total, "derived destination")
                    ):
                        temporary.unlink(missing_ok=True)
                        raise RuntimeError(f"Derived media copy verification failed: {source}")
                    if target.exists() and sha256(target) != sha256(source):
                        temporary.unlink(missing_ok=True)
                        raise RuntimeError(f"Existing library target differs from derived media: {target}")
                    os.replace(temporary, target)
                    created.append(target)
                    continue
                if pair.strategy == "archive-reextract":
                    raise RuntimeError("Packed media must complete Stowarr's verified extraction workflow first")
                torrent_file = Path(pair.torrent_file)
                if not source.exists() or source.stat().st_size != torrent_file.stat().st_size:
                    raise RuntimeError(f"Source changed or missing: {source}")
                if sha256(source, progress=lambda done, total: pair_progress(done, total, "current library media")) != sha256(
                    torrent_file, progress=lambda done, total: pair_progress(done, total, "qBittorrent media")
                ):
                    raise RuntimeError(f"Hash mismatch: {source} != {torrent_file}")
                if target.exists():
                    target_stat, torrent_stat = target.stat(), torrent_file.stat()
                    same_file = (target_stat.st_dev, target_stat.st_ino) == (torrent_stat.st_dev, torrent_stat.st_ino)
                    if not same_file and sha256(target) != sha256(torrent_file):
                        raise RuntimeError(f"Existing library target differs from torrent data: {target}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.stowarr-link")
                if temporary.exists():
                    temporary.unlink()
                os.link(torrent_file, temporary)
                if target.exists():
                    target.unlink()
                os.replace(temporary, target)
                created.append(target)
                if progress_callback:
                    progress_callback("MOVE_LIBRARY_VERIFYING", (pair_index + 1) / pair_total * 100,
                                      completed_files=pair_index + 1, total_files=len(plan.pairs), current=target.name,
                                      message="Created verified library file")
            self.store.update(operation_id, state_name("LINKED", "MOVE_LIBRARY_LINKED"), plan.json())
            if selected_auxiliary:
                for item in plan.auxiliary_files or []:
                    source, target = Path(item.source), Path(item.target)
                    if item.source not in selected_auxiliary:
                        continue
                    if item.origin == "qbittorrent":
                        if target.exists():
                            target_stat, source_stat = target.stat(), source.stat()
                            if (target_stat.st_dev, target_stat.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                                continue
                            if sha256(source) != sha256(target):
                                raise RuntimeError(f"Sidecar target conflict: {source} != {target}")
                            target.unlink()
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(f".{target.name}.stowarr-link")
                        temporary.unlink(missing_ok=True)
                        os.link(source, temporary)
                        os.replace(temporary, target)
                        continue
                    if item.status == "target-exists-same-size":
                        if sha256(source) == sha256(target):
                            copied_auxiliary.append((source, target))
                            continue
                        raise RuntimeError(f"Sidecar files have equal size but different content: {source} != {target}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.stowarr-copy")
                    shutil.copy2(source, temporary)
                    if sha256(source) != sha256(temporary):
                        temporary.unlink(missing_ok=True)
                        raise RuntimeError(f"Auxiliary file verification failed: {source}")
                    os.replace(temporary, target)
                    copied_auxiliary.append((source, target))
                self.store.update(operation_id, state_name("AUXILIARY_COPIED", "MOVE_LIBRARY_AUXILIARY"), plan.json())
            pool = next(pool for pool in self.config.pools if pool.name == plan.target_pool)
            mapping = self.arr[plan.app].download_mapping(torrent_hash)
            if not mapping:
                raise RuntimeError("The *Arr download mapping disappeared during reconciliation")
            item = mapping["item"]
            root = pool.radarr_root if plan.app == "radarr" else pool.sonarr_root
            tag = pool.radarr_tag if plan.app == "radarr" else pool.sonarr_tag
            pool_tags = [
                candidate.radarr_tag if plan.app == "radarr" else candidate.sonarr_tag
                for candidate in self.config.pools
            ]
            self.arr[plan.app].sync_pool(item, str(root), tag, pool_tags)
            self.store.update(operation_id, state_name("ARR_UPDATED", "MOVE_ARR_UPDATED"), plan.json())
            if progress_callback:
                progress_callback("MOVE_ARR_RESCANNING", 0, message=f"Waiting for {plan.app.capitalize()} rescan")
            self.arr[plan.app].rescan(int(item["id"]))
            if progress_callback:
                progress_callback("MOVE_ARR_RESCANNING", 100, message=f"{plan.app.capitalize()} rescan completed")
            self.store.update(operation_id, state_name("ARR_RESCANNED", "MOVE_ARR_RESCANNED"), plan.json())
            refreshed = self.arr[plan.app].download_mapping(torrent_hash)
            refreshed_files = {record.get("id"): Path(record.get("path", "")) for record in (refreshed or {}).get("files", [])}
            for record in plan.managed_files or []:
                expected = Path(plan.target_item_path or "") / record["relativePath"]
                if refreshed_files.get(record.get("id")) != expected:
                    raise RuntimeError(
                        f"{plan.app.capitalize()} did not confirm the managed file on its new path: {expected}"
                    )
            for pair in plan.pairs:
                source, target = Path(pair.source_library), Path(pair.target_library)
                if source != target and source.exists():
                    source.unlink()
            for source, _ in copied_auxiliary:
                if source.exists():
                    source.unlink()
            old_root = Path(plan.current_item_path) if plan.current_item_path else None
            if old_root and old_root != Path(plan.target_item_path or "") and old_root.exists():
                directories = (path for path in old_root.rglob("*") if path.is_dir())
                for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
                    directory.rmdir() if not any(directory.iterdir()) else None
                old_root.rmdir() if not any(old_root.iterdir()) else None
            self.store.update(operation_id, state_name("SOURCE_UNLINKED", "MOVE_OLD_LIBRARY_REMOVED"), plan.json())
            if not nested_move:
                self.store.update(operation_id, "COMPLETE", plan.json())
            return {"operation_id": operation_id, "state": "COMPLETE", "plan": plan.json()}
        except Exception as error:
            if not nested_move:
                self.store.update(operation_id, "FAILED", {**plan.json(), "error": str(error)})
            raise
