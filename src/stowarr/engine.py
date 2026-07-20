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
from .archive import is_archive_path
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

    def json(self) -> dict:
        return asdict(self)


def sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
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
        try:
            self._activate_connections(config.qbittorrent, config.radarr, config.sonarr, validate=False)
        except Exception as error:
            self.connection_error = str(error)

    @property
    def connections_ready(self) -> bool:
        return self.qbit is not None and set(self.arr) == {"radarr", "sonarr"}

    def _activate_connections(self, qbittorrent: Service, radarr: Service, sonarr: Service, validate: bool = True) -> dict:
        if not qbittorrent.url or not qbittorrent.username or not qbittorrent.password:
            raise ValueError("qBittorrent URL, username, and password are required")
        if not radarr.url or not radarr.api_key:
            raise ValueError("Radarr URL and API key are required")
        if not sonarr.url or not sonarr.api_key:
            raise ValueError("Sonarr URL and API key are required")
        qbit = QBittorrentClient(qbittorrent)
        arr = {"radarr": ArrClient(radarr, "radarr"), "sonarr": ArrClient(sonarr, "sonarr")}
        versions = {}
        if validate:
            qbit.categories()
            versions["qbittorrent"] = "connected"
            for name, client in arr.items():
                versions[name] = client.status().get("version", "connected")
        self.qbit = qbit
        self.arr = arr
        self.connection_error = None
        return versions

    @staticmethod
    def _masked_service(service: Service, kind: str) -> dict:
        result = {"url": service.url}
        if kind == "qbittorrent":
            result.update({"username": service.username, "password_set": bool(service.password)})
        else:
            result["api_key_set"] = bool(service.api_key)
        return result

    def connection_settings(self) -> dict:
        return {
            "required": True,
            "status": "ready" if self.connections_ready else "incomplete",
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
            raw = services.get(name)
            if not isinstance(raw, dict):
                raise ValueError(f"{name} configuration is required")
            url = str(raw.get("url", "")).strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"{name} URL must start with http:// or https://")
            if name == "qbittorrent":
                username = str(raw.get("username", "")).strip()
                password = str(raw.get("password") or existing.password)
                if not username or not password:
                    raise ValueError("qBittorrent username and password are required")
                candidates[name] = Service(url=url, username=username, password=password)
            else:
                api_key = str(raw.get("api_key") or existing.api_key).strip()
                if not api_key:
                    raise ValueError(f"{name.capitalize()} API key is required")
                candidates[name] = Service(url=url, api_key=api_key)

        qbit = QBittorrentClient(candidates["qbittorrent"])
        qbit.categories()
        versions = {"qbittorrent": "connected"}
        arr = {}
        for name in ("radarr", "sonarr"):
            arr[name] = ArrClient(candidates[name], name)
            versions[name] = arr[name].status().get("version", "connected")

        self.store.set_setting("connections", {name: asdict(service) for name, service in candidates.items()})
        self.config = replace(self.config, **candidates)
        self.qbit = qbit
        self.arr = arr
        self.connection_error = None
        return {"status": "ready", "versions": versions, **self.connection_settings()}

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
            normalized = {"targetPool": target_pool}
            plan = self.move_plan(torrent_hash, target_pool).json()
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
            normalized = {"targetPool": payload.get("targetPool")}
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
        elif content_mode != "direct":
            status, reason = "blocked", "Archive-backed move execution remains locked until the extraction and *Arr import transaction is complete"
        elif float(torrent.get("progress", 0)) < 1:
            status, reason = "blocked", "Torrent download is incomplete"
        elif str(torrent.get("state", "")).casefold() == "moving" or str(torrent.get("state", "")).casefold().startswith("checking"):
            status, reason = "blocked", "qBittorrent is already moving or checking this torrent"
        elif free_space is not None and size > free_space:
            status, reason = "blocked", "Target pool does not have enough free space"
        item = mapping.get("item") if mapping else None
        managed_files = []
        if item and mapping:
            target_item = self._target_item_path(item, target_pool, app)
            for record in mapping.get("files", []):
                managed_files.append({**record, "targetPath": str(target_item / record["relativePath"])})
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
        )

    def _wait_for_torrent(self, torrent_hash: str, predicate, timeout: int = 1800) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            torrent = self.qbit.torrent(torrent_hash)
            if not torrent:
                raise RuntimeError("Torrent disappeared from qBittorrent during the operation")
            if predicate(torrent):
                return torrent
            time.sleep(2)
        raise RuntimeError(f"qBittorrent operation exceeded {timeout} seconds")

    def move(self, torrent_hash: str, target_pool_name: str) -> dict:
        if self.store.active(torrent_hash, kind="move"):
            raise RuntimeError("Another Move operation is already active for this torrent")
        plan = self.move_plan(torrent_hash, target_pool_name)
        operation_id = self.store.record(torrent_hash, plan.app, "MOVE_PLANNED", plan.json(), kind="move")
        if plan.status != "ready" or not self.config.apply:
            state = "BLOCKED" if plan.status != "ready" else "DRY_RUN"
            self.store.update(operation_id, state, plan.json())
            return {"operation_id": operation_id, "state": state, "plan": plan.json()}

        self.qbit.pause(torrent_hash)
        self.store.update(operation_id, "MOVE_PAUSED", plan.json())
        try:
            self.qbit.set_location(torrent_hash, plan.target_save_path or "")
            self.store.update(operation_id, "MOVE_RELOCATING", plan.json())
            self._wait_for_torrent(
                torrent_hash,
                lambda torrent: Path(torrent.get("save_path", "")) == Path(plan.target_save_path or "")
                and torrent.get("state") != "moving",
            )
            self.qbit.set_category(torrent_hash, plan.target_category or "")
            self.qbit.recheck(torrent_hash)
            self.store.update(operation_id, "MOVE_RECHECKING", plan.json())
            self._wait_for_torrent(
                torrent_hash,
                lambda torrent: not str(torrent.get("state", "")).casefold().startswith("checking")
                and float(torrent.get("progress", 0)) >= 1,
            )
            self.store.update(operation_id, "MOVE_QBIT_COMPLETE", plan.json())
            result = self.reconcile(torrent_hash)
            if result["state"] != "COMPLETE":
                raise RuntimeError(f'Reconciliation did not complete after qBittorrent move: {result["state"]}')
            self.qbit.resume(torrent_hash)
            self.store.update(operation_id, "COMPLETE", {**plan.json(), "reconcile_operation_id": result["operation_id"]})
            return {"operation_id": operation_id, "state": "COMPLETE", "plan": plan.json(), "reconcile": result}
        except Exception as error:
            self.store.update(operation_id, "FAILED", {**plan.json(), "error": str(error)})
            raise

    def plan(self, torrent_hash: str) -> Plan:
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

    def reconcile(self, torrent_hash: str, auxiliary_sources: set[str] | None = None) -> dict:
        plan = self.plan(torrent_hash)
        selected_auxiliary = auxiliary_sources or set()
        blocked_sidecars = {"target-conflict", "torrent-name-conflict"}
        allowed_auxiliary = {
            item.source for item in (plan.auxiliary_files or []) if item.status not in blocked_sidecars
        }
        invalid_auxiliary = selected_auxiliary - allowed_auxiliary
        if invalid_auxiliary:
            raise ValueError("One or more selected sidecar paths are not eligible in the current plan")
        operation_id = self.store.record(torrent_hash, plan.app, "PLANNED", plan.json())
        if plan.status != "ready" or not self.config.apply:
            state = "BLOCKED" if plan.status != "ready" else "DRY_RUN"
            self.store.update(operation_id, state, plan.json())
            return {"operation_id": operation_id, "state": state, "plan": plan.json()}

        created: list[Path] = []
        copied_auxiliary: list[tuple[Path, Path]] = []
        try:
            for pair in plan.pairs:
                source = Path(pair.source_library)
                target = Path(pair.target_library)
                if pair.status in {"linked", "already-on-target"}:
                    continue
                if pair.strategy == "verified-copy":
                    if not source.exists() or source.stat().st_size != pair.size:
                        raise RuntimeError(f"Derived media changed or missing: {source}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.stowarr-copy")
                    shutil.copy2(source, temporary)
                    if sha256(source) != sha256(temporary):
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
                if sha256(source) != sha256(torrent_file):
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
            self.store.update(operation_id, "LINKED", plan.json())
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
                self.store.update(operation_id, "AUXILIARY_COPIED", plan.json())
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
            self.store.update(operation_id, "ARR_UPDATED", plan.json())
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
            self.store.update(operation_id, "SOURCE_UNLINKED", plan.json())
            self.store.update(operation_id, "COMPLETE", plan.json())
            return {"operation_id": operation_id, "state": "COMPLETE", "plan": plan.json()}
        except Exception as error:
            self.store.update(operation_id, "FAILED", {**plan.json(), "error": str(error)})
            raise
