from __future__ import annotations

import hmac
import json
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlparse

from .engine import Stowarr
from .queue import MoveQueueWorker
from . import __version__


def handler(manager: Stowarr):
    class Handler(BaseHTTPRequestHandler):
        WEB_TYPES = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }

        def send_bytes(self, status: int, payload: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def send_json(self, status: int, value, headers: dict[str, str] | None = None) -> None:
            payload = json.dumps(value, indent=2).encode()
            self.send_bytes(status, payload, "application/json", headers)

        def send_web(self, name: str) -> None:
            resource = files("stowarr").joinpath("web", name)
            if not resource.is_file():
                self.send_json(404, {"error": "not found"})
                return
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            self.send_bytes(200, resource.read_bytes(), self.WEB_TYPES.get(suffix, "application/octet-stream"))

        def session_token(self) -> str:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            return cookie["stowarr_session"].value if "stowarr_session" in cookie else ""

        def web_request(self) -> bool:
            return self.headers.get("X-Stowarr-Web-Proxy") == "1"

        def client_identity(self) -> str:
            return self.headers.get("X-Real-IP") or self.client_address[0]

        def authorized(self) -> bool:
            if self.web_request():
                if manager.config.auth_method == "external":
                    return bool(self.headers.get(manager.config.external_user_header, "").strip())
                return manager.auth.valid_session(self.session_token())
            if not manager.config.api_token:
                return True
            bearer = self.headers.get("Authorization", "")
            api_key = self.headers.get("X-Api-Key", "")
            return hmac.compare_digest(bearer, f"Bearer {manager.config.api_token}") or hmac.compare_digest(
                api_key, manager.config.api_token
            )

        def csrf_valid(self) -> bool:
            return not self.web_request() or self.headers.get("X-Stowarr-CSRF") == "1"

        def session_cookie(self, token: str, expired: bool = False) -> str:
            parts = [f"stowarr_session={token}", "Path=/", "HttpOnly", "SameSite=Strict"]
            if expired:
                parts.extend(("Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"))
            else:
                parts.append(f"Max-Age={manager.auth.SESSION_SECONDS}")
            if self.headers.get("X-Forwarded-Proto") == "https":
                parts.append("Secure")
            return "; ".join(parts)

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
            public = {"/api/health", "/api/auth/status"}
            if path.startswith("/api/") and path not in public and not self.authorized():
                self.send_json(401, {"error": "Authentication required"})
                return
            if path == "/api/health":
                self.send_json(200, {
                    "status": "ok" if manager.connections_ready else "setup-required",
                    "apply": manager.config.apply,
                    "version": __version__,
                })
            elif path == "/api/auth/status":
                external_user = self.headers.get(manager.config.external_user_header, "").strip()
                self.send_json(200, {
                    "authenticated": self.authorized(),
                    "username": external_user if manager.config.auth_method == "external" else ("admin" if self.authorized() else None),
                    "method": manager.config.auth_method,
                })
            elif path == "/api/auth/sessions":
                self.send_json(200, {"sessions": manager.auth.session_summary(self.session_token())})
            elif path == "/api/security/events":
                self.send_json(200, {"events": manager.store.recent_security_events()})
            elif path == "/api/config":
                self.send_json(200, {
                    "apply": manager.config.apply,
                    "version": __version__,
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
            elif path == "/api/status":
                self.send_json(200, manager.service_status())
            elif path == "/api/settings/runtime":
                self.send_json(200, manager.runtime_settings())
            elif path == "/api/settings/discovery":
                try:
                    self.send_json(200, manager.connection_discovery())
                except Exception as error:
                    self.log_operation_error("reconcile", error)
                    self.send_json(409, {"error": str(error)})
            elif path == "/api/operations":
                self.send_json(200, manager.store.recent())
            elif path == "/api/queue":
                self.send_json(200, manager.store.move_queue())
            elif path.startswith("/api/operations/") and path.endswith("/events"):
                try:
                    operation_id = int(path.split("/")[3])
                    self.send_json(200, {"events": manager.store.operation_events(operation_id)})
                except (KeyError, ValueError) as error:
                    self.send_json(404, {"error": str(error)})
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
            if path == "/api/auth/login":
                if manager.config.auth_method != "forms":
                    self.send_json(409, {"error": "Form login is disabled while external authentication is active"})
                    return
                try:
                    body = self.read_json()
                    token = manager.auth.authenticate(
                        str(body.get("username", "")), str(body.get("password", "")),
                        self.client_identity(),
                    )
                    self.send_json(200, {"authenticated": True, "username": "admin"}, {
                        "Set-Cookie": self.session_cookie(token),
                    })
                except PermissionError as error:
                    self.send_json(401, {"error": str(error)})
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
                return
            if not self.authorized():
                manager.store.security_event("request-denied", "", self.client_identity(), {"method": "POST", "path": path})
                self.send_json(401, {"error": "Authentication required"})
                return
            if not self.csrf_valid():
                self.send_json(403, {"error": "Valid CSRF header required"})
                return
            if path == "/api/auth/logout":
                manager.auth.logout(self.session_token(), self.client_identity())
                self.send_json(200, {"authenticated": False}, {"Set-Cookie": self.session_cookie("", expired=True)})
            elif path == "/api/auth/sessions/revoke":
                manager.auth.revoke_sessions(self.client_identity())
                self.send_json(200, {"revoked": True}, {"Set-Cookie": self.session_cookie("", expired=True)})
            elif path == "/api/auth/password":
                try:
                    body = self.read_json()
                    manager.auth.change_password(str(body.get("currentPassword", "")), str(body.get("newPassword", "")))
                    self.send_json(200, {"changed": True}, {"Set-Cookie": self.session_cookie("", expired=True)})
                except PermissionError as error:
                    self.send_json(403, {"error": str(error)})
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
            elif path == "/api/settings/connections":
                try:
                    self.send_json(200, manager.update_connections(self.read_json()))
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
            elif path == "/api/settings/runtime":
                try:
                    self.send_json(200, manager.update_runtime_settings(self.read_json()))
                except Exception as error:
                    self.send_json(400, {"error": str(error)})
            elif path.startswith("/api/queue/") and path.endswith("/cancel"):
                try:
                    queue_id = int(path.split("/")[3])
                    if not manager.store.cancel_queued_move(queue_id):
                        raise ValueError("Only a waiting queued Move can be cancelled")
                    self.send_json(200, {"id": queue_id, "state": "CANCELLED"})
                except (ValueError, IndexError) as error:
                    self.send_json(409, {"error": str(error)})
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
                    self.log_operation_error("reconcile", error)
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
                    self.log_operation_error("move", error)
                    self.send_json(409, {"error": str(error)})
            elif path == "/api/queue":
                try:
                    body = self.read_json()
                    torrent_hash = body.get("torrentHash")
                    target_pool = body.get("targetPool")
                    additional_files = body.get("additionalFiles", {})
                    if not isinstance(torrent_hash, str) or not torrent_hash:
                        raise ValueError("torrentHash is required")
                    if not isinstance(target_pool, str) or not target_pool:
                        raise ValueError("targetPool is required")
                    if not isinstance(additional_files, dict):
                        raise ValueError("additionalFiles must be an object")
                    payload = {"targetPool": target_pool, "additionalFiles": additional_files}
                    queued = manager.enqueue_move(
                        body.get("confirmationToken", ""), torrent_hash, payload
                    )
                    self.send_json(202, queued)
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            elif path.startswith("/api/verify/"):
                try:
                    self.send_json(200, manager.verify(path.rsplit("/", 1)[-1]))
                except Exception as error:
                    self.send_json(409, {"error": str(error)})
            else:
                self.send_json(404, {"error": "not found"})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if not self.authorized():
                manager.store.security_event("request-denied", "", self.client_identity(), {"method": "DELETE", "path": path})
                self.send_json(401, {"error": "Authentication required"})
                return
            if not self.csrf_valid():
                self.send_json(403, {"error": "Valid CSRF header required"})
                return
            if path != "/api/operations":
                self.send_json(404, {"error": "not found"})
                return
            try:
                body = self.read_json()
                clear_all = body.get("all") is True
                operation_ids = body.get("operationIds", [])
                if not clear_all and (
                    not isinstance(operation_ids, list)
                    or not all(isinstance(value, int) and not isinstance(value, bool) for value in operation_ids)
                ):
                    raise ValueError("operationIds must be a list of integer operation IDs")
                deleted = manager.store.delete_operations(None if clear_all else operation_ids)
                manager.store.security_event(
                    "history-deleted", "admin", self.client_identity(),
                    {"all": clear_all, "operation_ids": operation_ids if not clear_all else [], "deleted": deleted},
                )
                self.send_json(200, {"deleted": deleted})
            except ValueError as error:
                self.send_json(409, {"error": str(error)})

        def log_message(self, fmt, *args):
            request = str(args[0]) if args else ""
            status = str(args[1]) if len(args) > 1 else ""
            if status.startswith("2") and (request.startswith("GET /api/operations ") or request.startswith("GET /api/health ")):
                return
            print(f"stowarr request: {fmt % args}", flush=True)

        def log_operation_error(self, operation: str, error: Exception) -> None:
            print(f"stowarr {operation} failed: {type(error).__name__}: {error}", flush=True)
            traceback.print_exc()

    return Handler


def print_startup_credentials(manager: Stowarr) -> None:
    if manager.auth.generated_password and manager.config.auth_method == "forms":
        print("=" * 72, flush=True)
        print("Stowarr WebUI administrator account created", flush=True)
        print("Username: admin", flush=True)
        print(f"Password: {manager.auth.generated_password}", flush=True)
        print("Save this password now. It will not be displayed again.", flush=True)
        print("=" * 72, flush=True)
    if manager.generated_api_token:
        print("=" * 72, flush=True)
        print("Stowarr API key created", flush=True)
        print(f"API key: {manager.generated_api_token}", flush=True)
        print("Save this API key now. It will not be displayed again.", flush=True)
        print("=" * 72, flush=True)


def serve(manager: Stowarr) -> None:
    server = ThreadingHTTPServer((manager.config.listen, manager.config.port), handler(manager))
    queue_worker = MoveQueueWorker(manager)
    queue_worker.start()
    print_startup_credentials(manager)
    print(f"stowarr listening on {manager.config.listen}:{manager.config.port}; apply={manager.config.apply}", flush=True)
    try:
        server.serve_forever()
    finally:
        if not queue_worker.stop():
            print(
                "stowarr queue worker is still finishing an active Move during shutdown",
                flush=True,
            )
        server.server_close()
