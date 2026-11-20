import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from stowarr.server import handler, print_startup_credentials


class StartupCredentialTest(unittest.TestCase):
    @staticmethod
    def output(password=None, api_key=None, method="forms"):
        manager = SimpleNamespace(
            auth=SimpleNamespace(generated_password=password),
            generated_api_token=api_key,
            config=SimpleNamespace(auth_method=method),
        )
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            print_startup_credentials(manager)
        return stream.getvalue()

    def test_prints_each_generated_credential_independently(self):
        password_only = self.output(password="generated-password")
        api_only = self.output(api_key="generated-api-key")

        self.assertIn("generated-password", password_only)
        self.assertNotIn("API key created", password_only)
        self.assertIn("generated-api-key", api_only)
        self.assertNotIn("administrator account created", api_only)

    def test_external_auth_does_not_print_unused_admin_password(self):
        output = self.output(password="generated-password", api_key="generated-api-key", method="external")

        self.assertNotIn("generated-password", output)
        self.assertIn("generated-api-key", output)


class ProgressStreamTest(unittest.TestCase):
    def test_progress_events_are_flushed_as_line_delimited_json(self):
        request_handler = handler(SimpleNamespace())
        request = request_handler.__new__(request_handler)
        request.wfile = io.BytesIO()

        request.send_ndjson({
            "type": "progress",
            "stage": "reconciles",
            "current": 2,
            "total": 5,
        })
        request.send_ndjson({"type": "result", "result": {"safe_count": 3}})

        events = [
            json.loads(line)
            for line in request.wfile.getvalue().decode().splitlines()
        ]
        self.assertEqual(events[0]["current"], 2)
        self.assertEqual(events[1]["result"]["safe_count"], 3)


class ListenerAuthenticationTest(unittest.TestCase):
    @staticmethod
    def request(web_listener, headers, method="forms"):
        manager = SimpleNamespace(
            config=SimpleNamespace(
                api_token="test-key",
                auth_method=method,
                external_user_header="X-Forwarded-User",
            ),
            auth=SimpleNamespace(valid_session=lambda token: token == "browser-session"),
        )
        request_handler = handler(manager, web_listener=web_listener)
        request = request_handler.__new__(request_handler)
        request.headers = headers
        request.client_address = ("127.0.0.1", 0)
        return request

    def test_web_session_and_api_key_stay_on_their_own_listener(self):
        cookie = {"Cookie": "stowarr_session=browser-session"}
        api_key = {"X-Api-Key": "test-key"}
        self.assertTrue(self.request(True, cookie).authorized())
        self.assertFalse(self.request(True, api_key).authorized())
        self.assertTrue(self.request(False, api_key).authorized())
        self.assertFalse(self.request(False, {**cookie, "X-Stowarr-Web-Proxy": "1"}).authorized())
        self.assertFalse(self.request(True, cookie).csrf_valid())
        self.assertTrue(self.request(True, {**cookie, "X-Stowarr-CSRF": "1"}).csrf_valid())

    def test_direct_api_does_not_trust_external_browser_identity_headers(self):
        headers = {"X-Forwarded-User": "admin", "X-Stowarr-Web-Proxy": "1", "X-Real-IP": "192.0.2.1"}
        self.assertTrue(self.request(True, headers, method="external").authorized())
        self.assertFalse(self.request(False, headers, method="external").authorized())
        self.assertEqual(self.request(True, headers).client_identity(), "127.0.0.1")

    def test_web_request_size_limit_preserves_larger_direct_api_requests(self):
        payload = b'{"data":"' + b"x" * 1024**2 + b'"}'
        headers = {"Content-Length": str(len(payload))}
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            self.request(True, headers).read_json()
        direct = self.request(False, headers)
        direct.rfile = io.BytesIO(payload)
        self.assertEqual(len(direct.read_json()["data"]), 1024**2)
