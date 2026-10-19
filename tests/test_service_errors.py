import io
import json
import subprocess
import unittest
import urllib.error
from unittest.mock import Mock, patch

from repokeep import upstream, workers
from repokeep.common import CheckError
from repokeep.upstream import Client
from tests.support import arguments


class ServiceErrorsTest(unittest.TestCase):
    def test_http_403_and_429_do_not_retry_and_5xx_retry_is_bounded(self):
        for status in (403, 429, 500, 502, 503, 504):
            bodies = [io.BytesIO(b"untrusted body") for _ in range(3)]
            errors = [
                urllib.error.HTTPError(
                    "https://example.invalid/api", status, "failure", {}, body
                )
                for body in bodies
            ]
            with (
                self.subTest(status=status),
                patch.object(
                    upstream.urllib.request, "urlopen", side_effect=errors
                ) as request,
                patch.object(upstream.time, "sleep") as sleep,
            ):
                with self.assertRaisesRegex(
                    CheckError, f"HTTP {status} from example.invalid"
                ):
                    Client(15).request("https://example.invalid/api")
            expected = 1 if status in (403, 429) else 3
            self.assertEqual(request.call_count, expected)
            self.assertEqual(sleep.call_count, expected - 1)
            self.assertTrue(all(body.closed for body in bodies[:expected]))
            for error in errors:
                error.close()

    def test_retry_uses_remaining_budget_instead_of_starting_another_timeout(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/api", 503, "failure", {}, io.BytesIO()
        )
        with (
            patch.object(
                upstream.urllib.request, "urlopen", side_effect=error
            ) as request,
            patch.object(upstream.time, "monotonic", side_effect=[0, 0, 16, 16]),
            patch.object(upstream.time, "sleep"),
        ):
            with self.assertRaisesRegex(CheckError, "HTTP request timed out"):
                Client(15).request("https://example.invalid/api")
        self.assertEqual(request.call_count, 1)

    def test_network_failures_and_socket_timeouts_have_bounded_retries(self):
        for error in (
            urllib.error.URLError("connection refused"),
            TimeoutError("timed out"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(
                    upstream.urllib.request, "urlopen", side_effect=error
                ) as request,
                patch.object(upstream.time, "sleep"),
            ):
                with self.assertRaisesRegex(CheckError, "network error:"):
                    Client(15).request("https://example.invalid/api")
            self.assertEqual(request.call_count, 3)

    def test_invalid_json_and_non_objects_are_reported_for_both_services(self):
        for document in ("{broken", "[]", "null", '"text"'):
            for service in ("altrepo", "osv"):
                client = Client(15)
                with (
                    self.subTest(document=document, service=service),
                    patch.object(client, "request", return_value=document),
                    self.assertRaisesRegex(CheckError, "invalid"),
                ):
                    if service == "altrepo":
                        client.json("/site/maintainer_packages")
                    else:
                        client.osv("/query", {"version": "1.0"})

    def test_osv_request_is_tokenless_post_with_json_identity(self):
        response = io.BytesIO(b"{}")
        payload = {
            "package": {"ecosystem": "PyPI", "name": "example"},
            "version": "1.2",
        }
        with patch.object(
            upstream.urllib.request, "urlopen", return_value=response
        ) as request:
            self.assertEqual(Client(15).osv("/query", payload), {})
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://api.osv.dev/v1/query")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(json.loads(sent.data), payload)
        self.assertNotIn("authorization", {key.lower() for key in sent.headers})

    def test_git_failure_empty_refs_and_timeout_cleanup(self):
        for returncode, output, error, expected in (
            (1, "", "Repository not found", "repository unavailable"),
            (1, "", "fatal: connection refused", "git: fatal: connection refused"),
            (0, "not a tag", "", "has no tags"),
        ):
            process = Mock(
                stdout=io.StringIO(), stderr=io.StringIO(), returncode=returncode
            )
            process.communicate.return_value = (output, error)
            with (
                self.subTest(expected=expected),
                patch.object(upstream.subprocess, "Popen", return_value=process),
                self.assertRaisesRegex(CheckError, expected),
            ):
                Client(15).tags("owner/repo")
            self.assertTrue(process.stdout.closed and process.stderr.closed)
            process.wait.assert_called_once_with(timeout=1)
        process = Mock(stdout=io.StringIO(), stderr=io.StringIO())
        process.communicate.side_effect = subprocess.TimeoutExpired("git", 15)
        with (
            patch.object(upstream.subprocess, "Popen", return_value=process),
            self.assertRaisesRegex(CheckError, "tags request timed out"),
        ):
            Client(15).tags("owner/repo")
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=1)
        self.assertTrue(process.stdout.closed and process.stderr.closed)

    def test_worker_invalid_json_and_early_eof_are_errors(self):
        connection = Mock()
        with (
            patch.object(workers.signal, "signal"),
            patch.object(workers.os, "setsid"),
            patch.object(workers.Client, "request", return_value="{invalid"),
        ):
            workers.worker_entry(
                connection,
                "json-url",
                "https://example.invalid/tasks",
                arguments("task"),
            )
        connection.send.assert_called_once_with(
            (False, "invalid JSON from Taskoteka API")
        )
        connection.close.assert_called_once()
        connection = Mock()
        connection.recv.side_effect = EOFError
        with self.assertRaisesRegex(CheckError, "exited without a result"):
            workers.receive_worker({"connection": connection})
