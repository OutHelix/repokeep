import base64
import io
import unittest
import urllib.error
from unittest.mock import patch

from repokeep import upstream
from repokeep.common import CheckError
from repokeep.upstream import Client


class UpstreamTest(unittest.TestCase):
    def test_http_errors_close_without_reading_or_retrying(self):
        url = "https://example.invalid/api"
        for status in (403, 404, 429):
            body = io.BytesIO(b"unused error body")
            error = urllib.error.HTTPError(url, status, "failure", {}, body)
            with (
                self.subTest(status=status),
                patch.object(
                    upstream.urllib.request, "urlopen", side_effect=error
                ) as request,
                patch.object(
                    error, "read", side_effect=AssertionError("unexpected body read")
                ),
                self.assertRaisesRegex(
                    CheckError, f"HTTP {status} from example.invalid"
                ),
            ):
                Client(15).request(url)
            request.assert_called_once()
            self.assertTrue(body.closed)

    def test_transient_http_error_still_retries(self):
        url = "https://example.invalid/api"
        body = io.BytesIO(b"temporary failure")
        error = urllib.error.HTTPError(url, 503, "unavailable", {}, body)
        response = io.BytesIO(b'{"packages": []}')
        with (
            patch.object(
                upstream.urllib.request, "urlopen", side_effect=[error, response]
            ) as request,
            patch.object(upstream.time, "sleep"),
        ):
            self.assertEqual(Client(15).request(url), '{"packages": []}')
        self.assertEqual(request.call_count, 2)
        self.assertTrue(body.closed)
        self.assertTrue(response.closed)

    def test_repository_and_aliases_use_one_spec_parse(self):
        package = {"name": "example", "version": "1.0"}
        spec = "%global oname project\nVCS: https://github.com/owner/%oname\n"
        response = {"specfile_content": base64.b64encode(spec.encode()).decode()}
        client = Client(15)
        with (
            patch.object(client, "json", return_value=response),
            patch.object(
                upstream, "spec_metadata", wraps=upstream.spec_metadata
            ) as parse,
        ):
            self.assertEqual(
                client.resolve_repo(package, {}, "sisyphus"), ("owner/project", "spec")
            )
        parse.assert_called_once_with(spec, package)
        self.assertEqual(client.spec_aliases, ["project"])
        self.assertEqual(
            client.source_fields["vcs"], ["https://github.com/owner/project"]
        )
        self.assertTrue(client.repo_confident)
