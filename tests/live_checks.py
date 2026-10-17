import json
import os
import shutil
import unittest
import urllib.parse

from repokeep import tasks
from repokeep.common import CheckError
from repokeep.osv import affected_entries
from repokeep.upstream import API, OSV_API, Client

if os.environ.get("REPOKEEP_LIVE_TESTS") != "1":
    raise RuntimeError(
        "Live checks require REPOKEEP_LIVE_TESTS=1 and explicit tests.live_checks selection"
    )


class ExternalServiceError(RuntimeError):
    pass


class LiveContractTest(unittest.TestCase):
    def request_json(self, url, payload=None):
        try:
            text = Client(15).request(url, payload)
        except CheckError as exc:
            if str(exc).startswith(("HTTP ", "network error:")):
                raise ExternalServiceError(url + ": " + str(exc)) from exc
            raise
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            self.fail(url + ": invalid API JSON: " + str(exc))

    def test_taskoteka_documented_routes(self):
        document = self.request_json(tasks.API + "/openapi.json")
        self.assertIsInstance(document, dict)
        self.assertIn("/tasks", document["paths"])
        self.assertIn("/tasks/{id}", document["paths"])

    def test_task_owners_and_current_details(self):
        for owner in ("cas", "lav", "rider"):
            with self.subTest(owner=owner):
                query = urllib.parse.urlencode(
                    {"user": owner, "repo": "ALL", "state": "ALL"}
                )
                values = self.request_json(tasks.API + "/tasks?" + query)
                self.assertIsInstance(values, list)
                rows = [tasks.normalize(value) for value in values]
                self.assertTrue(all(row["owner"] == owner for row in rows))
                for row in sorted(rows, key=lambda value: value["id"], reverse=True)[
                    :2
                ]:
                    detail = tasks.normalize(
                        self.request_json(tasks.API + "/tasks/" + str(row["id"]))
                    )
                    self.assertEqual(detail["id"], row["id"])
                    self.assertEqual(detail["owner"], owner)
                    self.assertEqual(detail["repo"], row["repo"])
                    for number, name, subtask in tasks.package_rows(detail):
                        self.assertTrue(number)
                        self.assertIsInstance(name, str)
                        for arch in tasks.ARCHITECTURES:
                            status = tasks.arch_status(subtask, arch)
                            if status is tasks.ArchStatus.SUCCESS:
                                self.assertIn(
                                    subtask["build"][arch], ("built", "cached")
                                )

    def test_maintainer_package_contract(self):
        for owner in ("scala", "grenka", "cas"):
            with self.subTest(owner=owner):
                query = urllib.parse.urlencode(
                    {
                        "branch": "sisyphus",
                        "maintainer_nickname": owner,
                        "by_acl": "none",
                    }
                )
                response = self.request_json(API + "/site/maintainer_packages?" + query)
                self.assertIsInstance(response, dict)
                self.assertIsInstance(response["packages"], list)
                for package in response["packages"]:
                    for field in ("name", "version", "release"):
                        self.assertIsInstance(package[field], str)
                    self.assertTrue(package["name"])

    def test_osv_identity_contract(self):
        target = {"ecosystem": "PyPI", "name": "Jinja2"}
        response = self.request_json(
            OSV_API + "/query", {"package": target, "version": "3.1.0"}
        )
        self.assertIsInstance(response, dict)
        self.assertIsInstance(response.get("vulns", []), list)
        for record in response.get("vulns", []):
            self.assertIsInstance(record["id"], str)
            if not record.get("withdrawn"):
                self.assertTrue(affected_entries(record, target))

    def test_github_tag_transport_contract(self):
        self.assertIsNotNone(
            shutil.which("git"), "git is required for live GitHub checks"
        )
        try:
            tags = Client(15).tags("vsch/flexmark-java")
        except CheckError as exc:
            message = str(exc)
            if message.startswith(
                (
                    "GitHub repository unavailable",
                    "GitHub tags request timed out",
                    "GitHub repository has no tags",
                )
            ) or any(
                text in message.lower()
                for text in (
                    "could not resolve host",
                    "failed to connect",
                    "connection timed out",
                    "ssl certificate",
                    "returned error: 403",
                    "returned error: 429",
                    "returned error: 500",
                    "returned error: 502",
                    "returned error: 503",
                    "returned error: 504",
                )
            ):
                raise ExternalServiceError("GitHub: " + str(exc)) from exc
            raise
        self.assertEqual(tags, sorted(set(tags)))
        self.assertTrue(
            all(
                isinstance(tag, str) and tag and not tag.endswith("^{}") for tag in tags
            )
        )
