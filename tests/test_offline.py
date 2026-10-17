import hashlib
import socket
import subprocess
import sys
import unittest

from tests.offline import INSTALLED
from tests.support import FIXTURES, fixture


class OfflineTest(unittest.TestCase):
    def test_fixture_hashes_are_pinned(self):
        manifest = fixture("captures")
        self.assertEqual(
            set(manifest),
            {path.name for path in FIXTURES.glob("*.json")} - {"captures.json"},
        )
        for filename, metadata in manifest.items():
            with self.subTest(filename=filename):
                self.assertEqual(
                    hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest(),
                    metadata["sha256"],
                )

    def test_external_dns_and_connections_are_blocked(self):
        self.assertTrue(INSTALLED)
        with self.assertRaisesRegex(AssertionError, "blocked DNS"):
            socket.getaddrinfo("github.com", 443)
        with socket.socket() as connection:
            with self.assertRaisesRegex(AssertionError, "blocked network"):
                connection.connect(("192.0.2.1", 443))

    def test_python_children_inherit_network_guard(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import socket; socket.create_connection(('192.0.2.1', 443))",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Offline tests blocked", result.stderr)

    def test_real_git_is_rejected_before_execution(self):
        with self.assertRaisesRegex(AssertionError, "blocked real git"):
            subprocess.run(
                ["git", "ls-remote", "https://github.com/o/r.git"], check=True
            )
