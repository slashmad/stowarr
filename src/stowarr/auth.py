from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass

from .store import Store


@dataclass
class Session:
    token_hash: str
    expires_at: int
    created_at: int
    client: str


class AuthManager:
    """Persist an admin password hash and manage short-lived WebUI sessions."""

    SESSION_SECONDS = 12 * 60 * 60
    ATTEMPT_WINDOW = 5 * 60
    MAX_ATTEMPTS = 5

    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.attempts: dict[str, list[int]] = {}
        self.generated_password: str | None = None
        if not self.store.setting("web_auth"):
            password = os.getenv("STOWARR_ADMIN_PASSWORD") or secrets.token_urlsafe(18)
            self._save_password(password)
            if not os.getenv("STOWARR_ADMIN_PASSWORD"):
                self.generated_password = password
            self.store.security_event("administrator-created", "admin", "system")

    @staticmethod
    def _derive(password: str, salt: bytes) -> str:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()

    def _save_password(self, password: str) -> None:
        if len(password) < 12:
            raise ValueError("Admin password must contain at least 12 characters")
        salt = secrets.token_bytes(16)
        self.store.set_setting("web_auth", {
            "username": "admin",
            "salt": salt.hex(),
            "password_hash": self._derive(password, salt),
        })

    def authenticate(self, username: str, password: str, client: str) -> str:
        now = int(time.time())
        with self.lock:
            recent = [stamp for stamp in self.attempts.get(client, []) if stamp > now - self.ATTEMPT_WINDOW]
            if len(recent) >= self.MAX_ATTEMPTS:
                self.store.security_event("login-rate-limited", username, client)
                raise PermissionError("Too many login attempts; try again in five minutes")
            setting = self.store.setting("web_auth") or {}
            salt = bytes.fromhex(setting.get("salt", ""))
            candidate = self._derive(password, salt) if salt else ""
            valid = hmac.compare_digest(username, setting.get("username", "")) and hmac.compare_digest(
                candidate, setting.get("password_hash", "")
            )
            if not valid:
                recent.append(now)
                self.attempts[client] = recent
                self.store.security_event("login-failed", username, client)
                raise PermissionError("Invalid username or password")
            self.attempts.pop(client, None)
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            self.sessions[token_hash] = Session(token_hash, now + self.SESSION_SECONDS, now, client)
            self.store.security_event("login-succeeded", username, client)
            return token

    def valid_session(self, token: str) -> bool:
        if not token:
            return False
        now = int(time.time())
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            session = self.sessions.get(token_hash)
            if not session or session.expires_at < now:
                self.sessions.pop(token_hash, None)
                return False
            return True

    def logout(self, token: str, client: str = "") -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            self.sessions.pop(token_hash, None)
        self.store.security_event("logout", "admin", client)

    def session_summary(self, current_token: str = "") -> list[dict]:
        now = int(time.time())
        current_hash = hashlib.sha256(current_token.encode()).hexdigest() if current_token else ""
        with self.lock:
            self.sessions = {key: value for key, value in self.sessions.items() if value.expires_at >= now}
            return [
                {
                    "id": session.token_hash[:12],
                    "client": session.client,
                    "created_at": session.created_at,
                    "expires_at": session.expires_at,
                    "current": session.token_hash == current_hash,
                }
                for session in sorted(self.sessions.values(), key=lambda item: item.created_at, reverse=True)
            ]

    def revoke_sessions(self, client: str = "") -> None:
        with self.lock:
            count = len(self.sessions)
            self.sessions.clear()
        self.store.security_event("sessions-revoked", "admin", client, {"count": count})

    def change_password(self, current_password: str, new_password: str) -> None:
        setting = self.store.setting("web_auth") or {}
        salt = bytes.fromhex(setting.get("salt", ""))
        candidate = self._derive(current_password, salt) if salt else ""
        if not hmac.compare_digest(candidate, setting.get("password_hash", "")):
            raise PermissionError("Current admin password is incorrect")
        self._save_password(new_password)
        with self.lock:
            self.sessions.clear()
        self.store.security_event("password-changed", "admin", "webui")

    def reset_password(self, new_password: str) -> None:
        self._save_password(new_password)
        with self.lock:
            self.sessions.clear()
        self.store.security_event("password-reset", "admin", "command-line")
