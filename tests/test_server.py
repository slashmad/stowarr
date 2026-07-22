import contextlib
import io
import unittest
from types import SimpleNamespace

from stowarr.server import print_startup_credentials


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
