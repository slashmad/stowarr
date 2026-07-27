import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from stowarr.clients import ArrClient, QBittorrentClient
from stowarr.config import Service
from stowarr.mutations import ExternalMutationGuard, GuardedFilesystem


class ExternalMutationInvariantTest(unittest.TestCase):
    def setUp(self):
        self.guard = ExternalMutationGuard(lambda: True)

    def test_every_qbittorrent_mutation_is_blocked_at_the_client_boundary(self):
        client = QBittorrentClient(
            Service("http://unused", api_key="unused"), self.guard
        )
        client.http = Mock()
        client.categories = Mock(return_value={})
        operations = (
            lambda: client.pause("hash"),
            lambda: client.resume("hash"),
            lambda: client.recheck("hash"),
            lambda: client.set_location("hash", "/pool/download"),
            lambda: client.set_category("hash", "radarr-p1"),
            lambda: client.ensure_category("radarr-p1", "/pool/download"),
            lambda: client.delete_category("radarr-p1"),
        )

        for operation in operations:
            with self.subTest(operation=operation), self.assertRaisesRegex(
                RuntimeError, "Recovery"
            ):
                operation()

        client.http.request.assert_not_called()

    def test_every_arr_mutation_is_blocked_at_the_client_boundary(self):
        client = ArrClient(
            Service("http://unused", api_key="unused"),
            "radarr",
            self.guard,
        )
        client.http = Mock()
        item = {"id": 42, "path": "/old/Movie", "tags": []}

        client.tags = Mock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "Recovery"):
            client.ensure_tag("radarr-p1")

        client.tags = Mock(return_value=[{"id": 7, "label": "radarr-p1"}])
        with self.assertRaisesRegex(RuntimeError, "Recovery"):
            client.sync_pool(
                item, "/new", "radarr-p1", ["radarr-p1", "radarr-p3"]
            )

        with self.assertRaisesRegex(RuntimeError, "Recovery"):
            client.rescan(42)

        client.http.request.assert_not_called()

    def test_filesystem_mutations_are_blocked_at_the_boundary(self):
        filesystem = GuardedFilesystem(self.guard)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_bytes(b"content")
            target = root / "target"
            child = root / "child"
            operations = (
                lambda: filesystem.mkdir(child),
                lambda: filesystem.unlink(source),
                lambda: filesystem.rmdir(root),
                lambda: filesystem.copy2(source, target),
                lambda: filesystem.replace(source, target),
                lambda: filesystem.link(source, target),
                lambda: filesystem.rmtree(root),
                lambda: filesystem.execute(
                    "write probe", target.write_bytes, b"content"
                ),
            )

            for operation in operations:
                with self.subTest(operation=operation), self.assertRaisesRegex(
                    RuntimeError, "Recovery"
                ):
                    operation()

            self.assertTrue(source.exists())
            self.assertFalse(target.exists())
            self.assertFalse(child.exists())

    def test_mutations_are_forwarded_when_recovery_is_clear(self):
        operation = Mock(return_value="done")
        guard = ExternalMutationGuard(lambda: False)

        result = guard.execute("qbittorrent", "set category", operation, 1)

        self.assertEqual(result, "done")
        operation.assert_called_once_with(1)

    def test_external_clients_fail_closed_without_a_guard(self):
        qbit = QBittorrentClient(Service("http://unused", api_key="unused"))
        qbit.http = Mock()
        arr = ArrClient(Service("http://unused", api_key="unused"), "radarr")
        arr.http = Mock()

        with self.assertRaisesRegex(RuntimeError, "guard is required"):
            qbit.pause("hash")
        with self.assertRaisesRegex(RuntimeError, "guard is required"):
            arr.rescan(42)

        qbit.http.request.assert_not_called()
        arr.http.request.assert_not_called()
