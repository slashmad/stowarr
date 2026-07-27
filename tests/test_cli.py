import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stowarr import cli


class CliTest(unittest.TestCase):
    def test_reconcile_preserves_execution_mode_through_confirmation(self):
        manager = SimpleNamespace(
            config=SimpleNamespace(apply=False),
            consume_confirmation=Mock(return_value={
                "payload": {"auxiliaryFiles": ["/library/subtitle.srt"]},
            }),
            reconcile=Mock(return_value={
                "operation_id": 12,
                "state": "DRY_RUN",
            }),
        )

        with (
            patch("sys.argv", [
                "stowarr", "reconcile", "ABC123",
                "--confirmation-token", "confirmed",
            ]),
            patch.object(cli, "load_config", return_value=SimpleNamespace()),
            patch.object(cli, "Stowarr", return_value=manager),
            redirect_stdout(io.StringIO()) as output,
        ):
            cli.main()

        manager.consume_confirmation.assert_called_once_with(
            "confirmed",
            "reconcile",
            "ABC123",
            {"auxiliaryFiles": []},
            False,
        )
        manager.reconcile.assert_called_once_with(
            "ABC123",
            {"/library/subtitle.srt"},
            write_enabled=False,
        )
        self.assertIn('"state": "DRY_RUN"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
