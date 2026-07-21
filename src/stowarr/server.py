from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from .engine import Stowarr


def handler(manager: Stowarr):
    class Handler(BaseHTTPRequestHandler):
        WEB_TYPES = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }

        def send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status: int, value) -> None:
            payload = json.dumps(value, indent=2).encode()
            self.send_bytes(status, payload, "application/json")

        def send_web(self, name: str) -> None:
            resource = files("stowarr").joinpath("web", name)
            if not resource.is_file():
                self.send_json(404, {"error": "not found"})
                return
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            self.send_bytes(200, resource.read_bytes(), self.WEB_TYPES.get(suffix, "application/octet-stream"))

        def authorized(self) -> bool:
            if not manager.config.api_token:
                return True
            bearer = self.headers.get("Authorization", "")
            proxy_token = self.headers.get("X-Stowarr-Proxy-Token", "")
            return bearer == f"Bearer {manager.config.api_token}" or proxy_token == manager.config.api_token

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length)) if length else {}
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object")
            return value

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if manager.config.api_only and not path.startswith("/api/"):
                self.send_json(404, {"error": "API service"})
                return
            if path.startswith("/api/") and path != "/api/health" and not self.authorized():
                self.send_json(401, {"error": "Valid API bearer token required"})
                return
            if path == "/api/health":
                self.send_json(200, {"status": "ok" if manager.connections_ready else "setup-required", "apply": manager.config.apply})
            elif path == "/api/config":
                self.send_json(200, {
                    "apply": manager.config.apply,
                    "pools": [
                        {
                            "name": pool.name,
                            "prefix": str(pool.prefix),
                            "download_roots": [str(root) for root in pool.download_roots],
                            "radarr_root": str(pool.radarr_root),
                            "sonarr_root": str(pool.sonarr_root),
                            "radarr_category": pool.radarr_category,
                            "sonarr_category": pool.sonarr_category,
                            "radarr_tag": pool.radarr_tag,
                            "sonarr_tag": pool.sonarr_tag,
                        }
                        for pool in manager.config.pools
                    ],
                })
            elif path == "/api/settings/connections":
                self.send_json(200, manager.connection_settings())
            elif path == "/api/settings/runtime":
                self.send_json(200, manager.runtime_settings())
            elif path == "/api/settings/discovery":
                try:
                    self.send_json(200, manager.connection_discovery())
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            elif path == "/api/operations":
                self.send_json(200, manager.store.recent())
            elif not manager.connections_ready and path.startswith(("/api/plan/", "/api/move/", "/api/qbittorrent/", "/api/routing/", "/api/sync/")):
                self.send_json(503, {"error": "Configure qBittorrent, Radarr, and Sonarr in Settings first"})
            elif path.startswith("/api/plan/"):
                self.send_json(200, manager.plan(path.rsplit("/", 1)[-1]).json())
            elif path.startswith("/api/move/plan/"):
                target_pool = parse_qs(parsed.query).get("targetPool", [""])[0]
                self.send_json(200, manager.move_plan(path.rsplit("/", 1)[-1], target_pool).json())
            elif path == "/api/qbittorrent/search":
                try:
                    query = parse_qs(parsed.query).get("q", [""])[0]
                    self.send_json(200, manager.qbit_search(query))
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
            elif path == "/api/qbittorrent/torrents":
                self.send_json(200, manager.qbit_catalog())
            elif path == "/api/routing/audit":
                self.send_json(200, manager.routing_audit())
            elif path.startswith("/api/sync/"):
                try:
                    self.send_json(200, manager.sync_audit(path.rsplit("/", 1)[-1].casefold()))
                except ValueError as error:
                    self.send_json(400, {"error": str(error)})
            elif path in {"/", "/index.html"}:
                self.send_web("index.html")
            elif path.startswith("/assets/") and ".." not in path:
                self.send_web(path.removeprefix("/assets/"))
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if not self.authorized():
                self.send_json(401, {"error": "Valid API bearer token required"})
                return
            if path == "/api/settings/connections":
                try:
                    self.send_json(200, manager.update_connections(self.read_json()))
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
            elif path == "/api/settings/runtime":
                try:
                    self.send_json(200, manager.update_runtime_settings(self.read_json()))
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
            elif not manager.connections_ready:
                self.send_json(503, {"error": "Configure qBittorrent, Radarr, and Sonarr in Settings first"})
            elif path == "/api/confirmations":
                try:
                    body = self.read_json()
                    kind = body.get("kind")
                    torrent_hash = body.get("torrentHash")
                    payload = body.get("payload", {})
                    if not isinstance(torrent_hash, str) or not torrent_hash:
                        raise ValueError("torrentHash is required")
                    if not isinstance(payload, dict):
                        raise ValueError("payload must be an object")
                    self.send_json(201, manager.issue_confirmation(kind, torrent_hash, payload))
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            elif path.startswith("/api/reconcile/"):
                try:
                    body = self.read_json()
                    raw_sources = body.get("auxiliaryFiles", [])
                    if not isinstance(raw_sources, list) or not all(isinstance(item, str) for item in raw_sources):
                        raise ValueError("auxiliaryFiles must be a list of paths")
                    torrent_hash = path.rsplit("/", 1)[-1]
                    manager.consume_confirmation(body.get("confirmationToken", ""), "reconcile", torrent_hash, {"auxiliaryFiles": raw_sources})
                    self.send_json(200, manager.reconcile(torrent_hash, set(raw_sources)))
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            elif path.startswith("/api/move/apply/"):
                try:
                    body = self.read_json()
                    target_pool = body.get("targetPool")
                    if not isinstance(target_pool, str) or not target_pool:
                        raise ValueError("targetPool is required")
                    additional_files = body.get("additionalFiles", {})
                    if not isinstance(additional_files, dict):
                        raise ValueError("additionalFiles must be an object of source paths and actions")
                    torrent_hash = path.rsplit("/", 1)[-1]
                    payload = {"targetPool": target_pool, "additionalFiles": additional_files}
                    manager.consume_confirmation(body.get("confirmationToken", ""), "move", torrent_hash, payload)
                    self.send_json(200, manager.move(torrent_hash, target_pool, additional_files))
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            elif path.startswith("/api/verify/"):
                try:
                    self.send_json(200, manager.verify(path.rsplit("/", 1)[-1]))
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            else:
                self.send_json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            print(f"stowarr: {fmt % args}")

    return Handler


def serve(manager: Stowarr) -> None:
    server = ThreadingHTTPServer((manager.config.listen, manager.config.port), handler(manager))
    print(f"stowarr listening on {manager.config.listen}:{manager.config.port}; apply={manager.config.apply}")
    server.serve_forever()
