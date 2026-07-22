from __future__ import annotations

import time

from .config import Service
from .http import JsonClient


class ArrClient:
    def __init__(self, service: Service, kind: str):
        self.kind = kind
        self.http = JsonClient(service.url, {"X-Api-Key": service.api_key})

    def tags(self) -> list[dict]:
        return self.http.request("GET", "/api/v3/tag")

    def root_folders(self) -> list[dict]:
        return self.http.request("GET", "/api/v3/rootfolder")

    def download_clients(self) -> list[dict]:
        return self.http.request("GET", "/api/v3/downloadclient")

    def status(self) -> dict:
        return self.http.request("GET", "/api/v3/system/status")

    def history_for_download(self, download_id: str) -> list[dict]:
        records: list[dict] = []
        seen: set[tuple] = set()
        variants = dict.fromkeys((download_id, download_id.upper(), download_id.lower()))
        for candidate in variants:
            result = self.http.request(
                "GET", "/api/v3/history",
                query={"page": 1, "pageSize": 1000, "downloadId": candidate, "sortKey": "date", "sortDirection": "ascending"},
            )
            for record in result.get("records", result if isinstance(result, list) else []):
                identity = (record.get("id"), record.get("eventType"), record.get("date"), record.get("episodeId"), record.get("movieId"))
                if identity not in seen:
                    seen.add(identity)
                    records.append(record)
            if records:
                break
        return records

    def ensure_tag(self, label: str) -> int:
        existing = next((tag for tag in self.tags() if tag["label"] == label), None)
        return existing["id"] if existing else self.http.request("POST", "/api/v3/tag", body={"label": label})["id"]

    def item_for_download(self, download_id: str) -> dict | None:
        key = "movieId" if self.kind == "radarr" else "seriesId"
        record = next((entry for entry in self.history_for_download(download_id) if entry.get(key)), None)
        if not record:
            return None
        endpoint = "movie" if self.kind == "radarr" else "series"
        return self.http.request("GET", f"/api/v3/{endpoint}/{record[key]}")

    def download_mapping(self, download_id: str) -> dict | None:
        """Resolve a download to concrete movie or episode file identities."""
        records = self.history_for_download(download_id)
        item_key = "movieId" if self.kind == "radarr" else "seriesId"
        item_ids = {int(record[item_key]) for record in records if record.get(item_key)}
        if len(item_ids) != 1:
            return None
        item_id = next(iter(item_ids))
        endpoint = "movie" if self.kind == "radarr" else "series"
        item = self.http.request("GET", f"/api/v3/{endpoint}/{item_id}")
        if self.kind == "radarr":
            movie_file = item.get("movieFile")
            files = [] if not movie_file else [{
                "id": movie_file.get("id"),
                "path": movie_file.get("path") or f'{item["path"].rstrip("/")}/{movie_file.get("relativePath", "")}',
                "relativePath": movie_file.get("relativePath") or str(movie_file.get("path", "")).rsplit("/", 1)[-1],
                "size": int(movie_file.get("size", 0)),
                "episodeIds": [],
            }]
            return {"app": self.kind, "item": item, "history": records, "episodes": [], "files": files}

        episode_ids: set[int] = set()
        for record in records:
            if record.get("episodeId"):
                episode_ids.add(int(record["episodeId"]))
            data = record.get("data") or {}
            for value in data.get("episodeIds", []):
                episode_ids.add(int(value))
        episodes = self.http.request("GET", "/api/v3/episode", query={"seriesId": item_id})
        selected_episodes = [episode for episode in episodes if int(episode["id"]) in episode_ids]
        episode_file_ids = {
            int(episode["episodeFileId"])
            for episode in selected_episodes
            if episode.get("episodeFileId")
        }
        episode_files = self.http.request("GET", "/api/v3/episodefile", query={"seriesId": item_id})
        file_episode_ids: dict[int, list[int]] = {}
        for episode in selected_episodes:
            if episode.get("episodeFileId"):
                file_episode_ids.setdefault(int(episode["episodeFileId"]), []).append(int(episode["id"]))
        files = [{
            "id": record.get("id"),
            "path": record.get("path"),
            "relativePath": record.get("relativePath") or str(record.get("path", "")).removeprefix(item["path"].rstrip("/") + "/"),
            "size": int(record.get("size", 0)),
            "episodeIds": file_episode_ids.get(int(record.get("id", 0)), []),
        } for record in episode_files if int(record.get("id", 0)) in episode_file_ids]
        return {
            "app": self.kind,
            "item": item,
            "history": records,
            "episodes": selected_episodes,
            "files": files,
            "mappingComplete": bool(episode_ids and selected_episodes and episode_file_ids and files),
        }

    def all_items(self) -> list[dict]:
        endpoint = "movie" if self.kind == "radarr" else "series"
        return self.http.request("GET", f"/api/v3/{endpoint}")

    def history_for_downloads(self, download_ids: set[str], page_size: int = 250, max_pages: int = 40) -> dict[str, int]:
        """Resolve qBittorrent hashes to *Arr item ids using paged history."""
        key = "movieId" if self.kind == "radarr" else "seriesId"
        wanted = {value.casefold() for value in download_ids}
        found: dict[str, int] = {}
        for page in range(1, max_pages + 1):
            result = self.http.request(
                "GET",
                "/api/v3/history",
                query={
                    "page": page,
                    "pageSize": page_size,
                    "sortKey": "date",
                    "sortDirection": "descending",
                },
            )
            records = result.get("records", result if isinstance(result, list) else [])
            for record in records:
                download_id = str(record.get("downloadId") or "").casefold()
                item_id = record.get(key)
                if download_id in wanted and item_id and download_id not in found:
                    found[download_id] = int(item_id)
            if wanted.issubset(found) or len(records) < page_size:
                break
        return found

    def sync_pool(self, item: dict, root: str, tag_label: str, pool_tag_labels: list[str]) -> dict:
        tag_id = self.ensure_tag(tag_label)
        known = {tag["label"]: tag["id"] for tag in self.tags()}
        pool_tag_ids = {known[label] for label in pool_tag_labels if label in known}
        item["tags"] = [value for value in item.get("tags", []) if value not in pool_tag_ids]
        if tag_id not in item["tags"]:
            item["tags"].append(tag_id)
        if self.kind == "radarr":
            title_folder = item["path"].rstrip("/").split("/")[-1]
            item["path"] = f"{root.rstrip('/')}/{title_folder}"
        else:
            title_folder = item["path"].rstrip("/").split("/")[-1]
            item["path"] = f"{root.rstrip('/')}/{title_folder}"
        endpoint = "movie" if self.kind == "radarr" else "series"
        return self.http.request("PUT", f"/api/v3/{endpoint}/{item['id']}", query={"moveFiles": "false"}, body=item)

    def rescan(self, item_id: int, timeout: int = 1800) -> dict:
        name = "RescanMovie" if self.kind == "radarr" else "RescanSeries"
        key = "movieId" if self.kind == "radarr" else "seriesId"
        command = self.http.request("POST", "/api/v3/command", body={"name": name, key: item_id})
        command_id = command.get("id")
        if not command_id:
            raise RuntimeError(f"{self.kind.capitalize()} did not return a command id for {name}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.http.request("GET", f"/api/v3/command/{command_id}")
            status = str(current.get("status", "")).casefold()
            if status == "completed":
                return current
            if status in {"failed", "aborted", "cancelled"}:
                raise RuntimeError(f"{self.kind.capitalize()} {name} failed: {current.get('message') or status}")
            time.sleep(2)
        raise RuntimeError(f"{self.kind.capitalize()} {name} exceeded {timeout} seconds")


class QBittorrentClient:
    def __init__(self, service: Service):
        headers = {"Authorization": f"Bearer {service.api_key}"} if service.api_key else None
        self.http = JsonClient(service.url, headers)
        if not service.api_key:
            self.http.request("POST", "/api/v2/auth/login", form={"username": service.username, "password": service.password})

    def torrents(self) -> list[dict]:
        return self.http.request("GET", "/api/v2/torrents/info")

    def torrent(self, torrent_hash: str) -> dict | None:
        records = self.http.request("GET", "/api/v2/torrents/info", query={"hashes": torrent_hash})
        return next((record for record in records if record.get("hash", "").casefold() == torrent_hash.casefold()), None)

    def files(self, torrent_hash: str) -> list[dict]:
        return self.http.request("GET", "/api/v2/torrents/files", query={"hash": torrent_hash})

    def categories(self) -> dict[str, dict]:
        return self.http.request("GET", "/api/v2/torrents/categories")

    def version(self) -> str:
        return self.http.request("GET", "/api/v2/app/version")

    def pause(self, torrent_hash: str) -> None:
        self.http.request("POST", "/api/v2/torrents/stop", form={"hashes": torrent_hash})

    def resume(self, torrent_hash: str) -> None:
        self.http.request("POST", "/api/v2/torrents/start", form={"hashes": torrent_hash})

    def recheck(self, torrent_hash: str) -> None:
        self.http.request("POST", "/api/v2/torrents/recheck", form={"hashes": torrent_hash})

    def set_location(self, torrent_hash: str, location: str) -> None:
        self.http.request("POST", "/api/v2/torrents/setLocation", form={"hashes": torrent_hash, "location": location})

    def set_category(self, torrent_hash: str, category: str) -> None:
        self.http.request("POST", "/api/v2/torrents/setCategory", form={"hashes": torrent_hash, "category": category})
