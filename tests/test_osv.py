import unittest
from types import SimpleNamespace

from repokeep.models import vulnerability_result
from repokeep.osv import (
    add_osv_record,
    affected_entries,
    check_vulnerable_package,
    fixed_releases,
    osv_package,
)
from repokeep.output import render_security, security_warning
from tests.support import fixture, streams

TARGET = {"ecosystem": "PyPI", "name": "example"}
PACKAGE = {"name": "python3-module-example", "version": "1.2", "release": "alt4"}


def record(identifier, aliases=(), severity="HIGH", fixed="1.3"):
    return {
        "id": identifier,
        "aliases": list(aliases),
        "summary": "An example issue",
        "database_specific": {"severity": severity},
        "affected": [
            {
                "package": TARGET,
                "ranges": [
                    {
                        "type": "ECOSYSTEM",
                        "events": [{"introduced": "0"}, {"fixed": fixed}],
                    }
                ],
            }
        ],
    }


class OsvTest(unittest.TestCase):
    def test_equal_severity_keeps_the_first_label(self):
        result = vulnerability_result(PACKAGE)
        add_osv_record(result, record("PYSEC-first", severity="MEDIUM"), TARGET, "1.2")
        add_osv_record(
            result, record("GHSA-second", severity="MODERATE"), TARGET, "1.2"
        )
        add_osv_record(
            result,
            record("CVE-2026-bridge", ["PYSEC-first", "GHSA-second"], "LOW"),
            TARGET,
            "1.2",
        )
        self.assertEqual(len(result.vulnerabilities), 1)
        self.assertEqual(result.vulnerabilities[0]["severity"], "MEDIUM")

    def test_alias_bridge_keeps_highest_severity_and_fixes(self):
        result = vulnerability_result(PACKAGE)
        for item in [
            record("PYSEC-first", ["GHSA-first"], "LOW", "1.3"),
            record("PYSEC-second", ["CVE-2026-0001"], "CRITICAL", "1.4"),
            record("GHSA-first", ["CVE-2026-0001"], "HIGH", "1.5"),
        ]:
            add_osv_record(result, item, TARGET, "1.2")
        self.assertEqual(len(result.vulnerabilities), 1)
        vuln = result.vulnerabilities[0]
        self.assertEqual(vuln["id"], "CVE-2026-0001")
        self.assertEqual(vuln["severity"], "CRITICAL")
        self.assertEqual(
            vuln["fixed_versions"], [f"1.{n} [PyPI:example]" for n in (3, 4, 5)]
        )
        compact = render_security(result)
        self.assertIn("CVE-2026-0001", compact)
        self.assertNotIn("PYSEC-first", compact)
        detail = render_security(result, details=True)
        self.assertIn("CRITICAL", detail)
        self.assertIn("Component: PyPI:example", detail)
        self.assertIn("1.2-alt4", detail)

    def test_fixed_boundaries_apply_to_current_series(self):
        events = [
            {"introduced": "0"},
            {"fixed": "1.3"},
            {"introduced": "2.0"},
            {"fixed": "2.1"},
        ]
        self.assertEqual(fixed_releases(events, "1.2"), ["1.3"])
        self.assertEqual(fixed_releases(events, "2.0"), ["2.1"])
        self.assertEqual(
            fixed_releases(
                [{"introduced": "0"}, {"last_affected": "1.3"}, {"limit": "1.4"}], "1.2"
            ),
            [],
        )
        self.assertEqual(
            fixed_releases([{"introduced": "unknown"}, {"fixed": "1.3"}], "1.2"),
            ["1.3"],
        )

    def test_components_and_fixing_commits(self):
        target = {"ecosystem": "GIT", "name": "https://github.com/o/r"}
        first = {
            "id": "CVE-2026-0002",
            "aliases": ["GHSA-example"],
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
        linked = record("GHSA-example", ["CVE-2026-0002"])
        linked["references"] = [{"type": "PACKAGE", "url": target["name"]}]
        result = vulnerability_result(PACKAGE)
        add_osv_record(result, first, target, "1.2")
        self.assertIn("Fix: commit aaaaaaaaaaaa", render_security(result, details=True))
        add_osv_record(result, linked, target, "1.2", related_repo=True)
        self.assertEqual(result.vulnerabilities[0]["fixed"], ["1.3 [PyPI:example]"])
        self.assertIn("Fixed upstream: 1.3", render_security(result, details=True))
        self.assertEqual(affected_entries(linked, target), [])

    def test_record_description_fallback_and_withdrawal(self):
        item = record("GHSA-example")
        item["details"] = item.pop("summary")
        result = vulnerability_result(PACKAGE)
        add_osv_record(result, item, TARGET, "1.2")
        self.assertIn("An example issue", render_security(result, details=True))
        withdrawn = record("PYSEC-withdrawn")
        withdrawn["withdrawn"] = "2026-01-01"
        add_osv_record(result, withdrawn, TARGET, "1.2")
        self.assertEqual(len(result.vulnerabilities), 1)

    def test_fixed_versions_remain_attached_to_each_component(self):
        target = {"ecosystem": "GIT", "name": "https://github.com/o/project"}
        item = {
            "id": "GHSA-components",
            "references": [{"type": "SOURCE", "url": target["name"]}],
            "affected": [
                {
                    "package": {"ecosystem": "Maven", "name": "org.example:core"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [{"introduced": "0"}, {"fixed": "1.3"}],
                        }
                    ],
                },
                {
                    "package": {"ecosystem": "Maven", "name": "org.example:cli"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [{"introduced": "0"}, {"fixed": "1.5"}],
                        }
                    ],
                },
            ],
        }

        result = vulnerability_result(PACKAGE)
        for component in ("core", "cli"):
            add_osv_record(
                result,
                item,
                {"ecosystem": "Maven", "name": "org.example:" + component},
                "1.2",
            )
        text = render_security(result, details=True)
        self.assertRegex(
            text, r"Fixed upstream: 1\.3\n\s+Component: Maven:org.example:core"
        )
        self.assertRegex(
            text, r"Fixed upstream: 1\.5\n\s+Component: Maven:org.example:cli"
        )

    def test_saved_real_response(self):
        result = vulnerability_result(dict(PACKAGE, version="3.1.0"))
        for item in fixture("osv-query")["vulns"]:
            add_osv_record(
                result, item, {"ecosystem": "PyPI", "name": "jinja2"}, "3.1.0"
            )
        self.assertEqual(
            {v["id"]: v["fixed_versions"] for v in result.vulnerabilities},
            {
                "CVE-2025-27516": ["3.1.6 [PyPI:jinja2]"],
                "CVE-2024-56201": ["3.1.5 [PyPI:jinja2]"],
                "CVE-2024-22195": ["3.1.3 [PyPI:jinja2]"],
                "CVE-2024-34064": ["3.1.4 [PyPI:jinja2]"],
                "CVE-2024-56326": ["3.1.5 [PyPI:jinja2]"],
            },
        )

    def test_pagination_and_invalid_identity_response(self):
        class Client:
            def __init__(self):
                self.payloads = []

            def osv(self, endpoint, payload):
                self.payloads.append(payload)
                if len(self.payloads) == 1:
                    return {
                        "vulns": [record("GHSA-example")],
                        "next_page_token": "next",
                    }
                return {"vulns": [record("PYSEC-example", ["GHSA-example"])]}

        client = Client()
        args = SimpleNamespace(package_timeout=15, branch="sisyphus")
        result = check_vulnerable_package(
            PACKAGE, {"osv_packages": [TARGET]}, client, args
        )
        self.assertEqual(result.status, "VULN")
        self.assertEqual(client.payloads[1]["page_token"], "next")
        self.assertEqual(len(result.vulnerabilities), 1)
        result = check_vulnerable_package(
            dict(PACKAGE, version="1.2.gitabcdef123"), {}, Client(), args
        )
        self.assertEqual(result.status, "SKIP")

    def test_validation_and_warning_stream(self):
        for identity in (
            "Maven:broken",
            "GIT:https://user:secret@github.com/o/r",
            "no-separator",
        ):
            with self.assertRaises(ValueError):
                osv_package(identity)
        self.assertEqual(
            osv_package("Maven:org.example:artifact")["name"], "org.example:artifact"
        )
        with streams() as (out, err):
            security_warning(SimpleNamespace(vulnerable_only=True, color="never"))
        self.assertEqual(out.getvalue(), "")
        warning = " ".join(err.getvalue().split())
        self.assertIn("backported patches without changing Version", warning)
        self.assertIn("does not prove", warning)
        self.assertNotIn("\033", err.getvalue())

    def test_repeated_page_token_retains_partial_matches_as_error(self):
        class RepeatingClient:
            def osv(self, endpoint, payload):
                return {"vulns": [record("GHSA-example")], "next_page_token": "loop"}

        result = check_vulnerable_package(
            PACKAGE,
            {"osv_packages": [TARGET]},
            RepeatingClient(),
            SimpleNamespace(package_timeout=15, branch="sisyphus"),
        )
        self.assertEqual(result.status, "ERR")
        self.assertEqual(len(result.vulnerabilities), 1)
        self.assertIn("repeated", result.note)
        self.assertIn("Check incomplete", render_security(result))
