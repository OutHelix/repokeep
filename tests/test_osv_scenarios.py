import copy
import json
import unittest
from unittest.mock import patch

from repokeep import osv, updates
from repokeep.cli import parser
from repokeep.common import CheckError
from repokeep.output import render_security
from repokeep.upstream import Client
from tests.support import arguments, fixture, streams

PACKAGE = {"name": "example-java", "version": "1.2", "release": "alt99"}
CORE = {"ecosystem": "Maven", "name": "org.example:core"}
CLI = {"ecosystem": "Maven", "name": "org.example:cli"}


class OsvScenariosTest(unittest.TestCase):
    def test_saved_aliases_merge_across_components_and_keep_fixes_separate(self):
        records = fixture("osv-components")
        client = Client(15)
        with patch.object(
            client, "osv", side_effect=[{"vulns": records}, {"vulns": [records[1]]}]
        ) as request:
            result = osv.check_vulnerable_package(
                PACKAGE,
                {"osv_packages": [CORE, CLI]},
                client,
                arguments("updates", "--vulnerable-only"),
            )
        self.assertEqual(result.status, "VULN")
        self.assertEqual(len(result.vulnerabilities), 1)
        item = result.vulnerabilities[0]
        self.assertEqual(item["id"], "CVE-2026-0001")
        self.assertEqual(
            item["aliases"], ["CVE-2026-0001", "GHSA-fixture", "PYSEC-fixture"]
        )
        self.assertEqual(item["severity"], "CRITICAL")
        self.assertEqual(
            item["fixed_versions"],
            [
                "1.3 [Maven:org.example:core]",
                "1.4 [Maven:org.example:core]",
                "1.5 [Maven:org.example:cli]",
            ],
        )
        self.assertEqual(
            [call.args[1]["package"] for call in request.call_args_list], [CORE, CLI]
        )
        compact = render_security(result, width=80)
        self.assertEqual(compact.count("CVE-2026-0001"), 1)
        self.assertNotIn("GHSA-fixture", compact)
        self.assertNotIn("Fixed upstream:", compact)
        detailed = render_security(result, details=True, width=80)
        self.assertIn("ALT RPM: 1.2-alt99", detailed)
        self.assertIn("Fixture component issue", detailed)
        self.assertIn("Severity: CRITICAL", detailed)
        self.assertRegex(
            detailed,
            r"Fixed upstream: 1\.3, 1\.4\n\s+Component: Maven:org.example:core",
        )
        self.assertRegex(
            detailed, r"Fixed upstream: 1\.5\n\s+Component: Maven:org.example:cli"
        )

    def test_partial_page_failure_preserves_matches_and_progress(self):
        client = Client(15)
        progress = []
        with patch.object(
            client,
            "osv",
            side_effect=[
                {"vulns": [fixture("osv-components")[0]], "next_page_token": "next"},
                CheckError("HTTP 429 from api.osv.dev"),
            ],
        ):
            result = osv.check_vulnerable_package(
                PACKAGE,
                {"osv_packages": [CORE]},
                client,
                arguments("updates", "--vulnerable-only"),
                progress=lambda value: progress.append(copy.deepcopy(value)),
            )
        self.assertEqual([r.status for r in progress], ["VULN"])
        self.assertEqual(result.status, "ERR")
        self.assertEqual(result.vulnerabilities[0]["id"], "GHSA-fixture")
        self.assertIn("HTTP 429", result.note)
        self.assertIn("Check incomplete:", render_security(result, details=True))

    def test_alias_detail_failure_preserves_git_advisory(self):
        target = {"ecosystem": "GIT", "name": "https://github.com/owner/project"}
        record = {
            "id": "CVE-fixture",
            "aliases": ["GHSA-fixture"],
            "affected": [
                {
                    "ranges": [
                        {
                            "type": "GIT",
                            "repo": target["name"],
                            "events": [{"fixed": "a" * 40}],
                        }
                    ]
                }
            ],
        }
        client = Client(15)
        with patch.object(
            client,
            "osv",
            side_effect=[{"vulns": [record]}, CheckError("HTTP 503 from api.osv.dev")],
        ) as request:
            result = osv.check_vulnerable_package(
                PACKAGE,
                {"osv_packages": [target]},
                client,
                arguments("updates", "--vulnerable-only"),
            )
        self.assertEqual(request.call_args_list[1].args, ("/vulns/GHSA-fixture",))
        self.assertEqual(result.status, "ERR")
        self.assertEqual(
            result.vulnerabilities[0]["fixed_commits"], ["commit aaaaaaaaaaaa"]
        )

    def test_malformed_osv_records_produce_errors_instead_of_clean_results(self):
        for response in (
            {"vulns": {}},
            {"vulns": [{}]},
            {"vulns": [{"id": "CVE-fixture", "affected": "bad"}]},
            {"vulns": [{"id": "CVE-fixture", "affected": [{"package": "bad"}]}]},
            {
                "vulns": [
                    {
                        "id": "CVE-fixture",
                        "affected": [{"package": CORE, "ranges": "bad"}],
                    }
                ]
            },
            {"vulns": [dict(fixture("osv-components")[0], aliases="bad")]},
        ):
            client = Client(15)
            with (
                self.subTest(response=response),
                patch.object(client, "osv", return_value=response),
            ):
                result = osv.check_vulnerable_package(
                    PACKAGE,
                    {"osv_packages": [CORE]},
                    client,
                    arguments("updates", "--vulnerable-only"),
                )
            self.assertEqual(result.status, "ERR")
            self.assertEqual(result.vulnerabilities, [])

    def test_security_json_and_backport_warning_survive_partial_failure(self):
        client = Client(15)
        with patch.object(
            client, "osv", side_effect=CheckError("HTTP 403 from api.osv.dev")
        ):
            result = osv.check_vulnerable_package(
                PACKAGE,
                {"osv_packages": [CORE]},
                client,
                arguments("updates", "--vulnerable-only"),
            )
        result.vulnerabilities = [
            {"id": "CVE-fixture", "aliases": ["CVE-fixture"], "fixed_versions": []}
        ]
        args = arguments("updates", "--vulnerable-only", "--json", "--no-color")
        with (
            patch.object(updates, "load_rules", return_value={}),
            patch.object(updates, "package_list", return_value={"packages": [PACKAGE]}),
            patch.object(
                updates, "package_results", return_value=iter_results([result])
            ),
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 2)
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]["status"], "ERR")
        self.assertEqual(rows[0]["vulnerabilities"][0]["id"], "CVE-fixture")
        warning = " ".join(err.getvalue().split())
        self.assertIn("backported patches without changing Version", warning)
        self.assertIn("No OSV match does not prove that a package is safe", warning)
        self.assertIn("Failed or incomplete checks: 1", warning)
        self.assertNotIn("\033", out.getvalue() + err.getvalue())


def iter_results(results):
    yield from results
