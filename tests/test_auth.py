import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stowarr.auth import AuthManager
from stowarr.store import Store


class AuthManagerTest(unittest.TestCase):
    def test_generates_admin_password_and_stores_only_hash(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            store = Store(Path(directory) / "state.sqlite3")
            auth = AuthManager(store)

            self.assertTrue(auth.generated_password)
            setting = store.setting("web_auth")
            self.assertEqual(setting["username"], "admin")
            self.assertNotIn(auth.generated_password, setting.values())
            token = auth.authenticate("admin", auth.generated_password, "client")
            self.assertTrue(auth.valid_session(token))

    def test_password_change_invalidates_existing_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"STOWARR_ADMIN_PASSWORD": "initial-password-123"}, clear=True
        ):
            auth = AuthManager(Store(Path(directory) / "state.sqlite3"))
            token = auth.authenticate("admin", "initial-password-123", "client")

            auth.change_password("initial-password-123", "replacement-password-456")

            self.assertFalse(auth.valid_session(token))
            self.assertTrue(auth.valid_session(auth.authenticate("admin", "replacement-password-456", "client")))

    def test_rejects_repeated_invalid_logins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"STOWARR_ADMIN_PASSWORD": "initial-password-123"}, clear=True
        ):
            auth = AuthManager(Store(Path(directory) / "state.sqlite3"))
            for _ in range(auth.MAX_ATTEMPTS):
                with self.assertRaisesRegex(PermissionError, "Invalid"):
                    auth.authenticate("admin", "wrong-password", "client")
            with self.assertRaisesRegex(PermissionError, "Too many"):
                auth.authenticate("admin", "initial-password-123", "client")

    def test_lists_and_revokes_sessions(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"STOWARR_ADMIN_PASSWORD": "initial-password-123"}, clear=True
        ):
            auth = AuthManager(Store(Path(directory) / "state.sqlite3"))
            token = auth.authenticate("admin", "initial-password-123", "10.0.0.8")

            sessions = auth.session_summary(token)
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0]["current"])
            self.assertEqual(sessions[0]["client"], "10.0.0.8")

            auth.revoke_sessions("10.0.0.8")
            self.assertFalse(auth.valid_session(token))

    def test_session_survives_auth_manager_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"STOWARR_ADMIN_PASSWORD": "initial-password-123"}, clear=True
        ):
            database = Path(directory) / "state.sqlite3"
            first = AuthManager(Store(database))
            token = first.authenticate("admin", "initial-password-123", "10.0.0.8")

            restarted = AuthManager(Store(database))

            self.assertTrue(restarted.valid_session(token))
            sessions = restarted.session_summary(token)
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0]["current"])

    def test_security_events_do_not_contain_passwords(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"STOWARR_ADMIN_PASSWORD": "initial-password-123"}, clear=True
        ):
            store = Store(Path(directory) / "state.sqlite3")
            auth = AuthManager(store)
            with self.assertRaises(PermissionError):
                auth.authenticate("admin", "secret-that-must-not-be-logged", "client")

            serialized = str(store.recent_security_events())
            self.assertNotIn("secret-that-must-not-be-logged", serialized)
            self.assertIn("login-failed", serialized)
