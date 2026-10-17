import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repokeep import updates
from repokeep.cli import parser
from repokeep.common import CheckError
from repokeep.models import Result
from repokeep.upstream import Client, github_repo, repo_from_spec
from tests.support import arguments, fixture, streams


class UpdatesTest(unittest.TestCase):
    def setUp(self):
        git = patch.object(updates.shutil, "which", return_value="/test/git")
        git.start()
        self.addCleanup(git.stop)

    def test_original_api_contract_and_acl_modes(self):
        for mode in updates.ACL_MODES:
            args = arguments("updates", "--acl", mode)
            with patch.object(updates, "request_data", return_value={}) as request:
                updates.package_list(args)
            self.assertEqual(request.call_args.args[1], "/site/maintainer_packages")
            self.assertEqual(
                request.call_args.kwargs,
                {"branch": "sisyphus", "maintainer_nickname": "scala", "by_acl": mode},
            )

    def test_saved_spec_and_nonexecuting_macros(self):
        response = fixture("altrepo-spec")
        spec = base64.b64decode(response["specfile_content"]).decode()
        package = next(
            p
            for p in fixture("altrepo-packages")["packages"]
            if p["name"] == "flexmark-java"
        )
        self.assertEqual(repo_from_spec(spec, package), "vsch/flexmark-java")
        custom = "%global oname project\nVCS: https://github.com/owner/%oname\nSource: %(touch /tmp/must-not-run)\n%prep\nURL: https://github.com/wrong/repo"
        self.assertEqual(repo_from_spec(custom, package), "owner/project")
        self.assertIsNone(
            repo_from_spec(
                "VCS: https://example.org/project\nURL: https://github.com/o/r", package
            )
        )
        self.assertEqual(github_repo("git@github.com:owner/repo.git"), "owner/repo")

    def test_category_request_and_intersection(self):
        args = arguments("updates", "-g", "python")
        self.assertEqual(args.group, ["Development/Python", "Development/Python3"])
        values = [
            {"name": "one", "version": "1.0", "release": "alt1"},
            {"name": "two", "version": "2.0", "release": "alt1"},
        ]
        with patch.object(
            updates,
            "request_data",
            side_effect=[
                {
                    "packages": [
                        dict(values[0], category="Development/Python", hash="hash")
                    ]
                },
                {
                    "packages": [
                        dict(
                            values[1],
                            version="2.1",
                            category="Development/Python3",
                            hash="other",
                        )
                    ]
                },
            ],
        ) as request:
            selected = updates.filter_groups(values, args)
        self.assertEqual(selected[0]["hash"], "hash")
        self.assertNotIn("hash", selected[1])
        self.assertEqual(request.call_args.kwargs["package_type"], "source")
        self.assertEqual(request.call_args.args[1], "/site/repository_packages")

    def test_config_schema_overrides_and_xdg_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repokeep").mkdir()
            (root / "repokeep/config.json").write_text(
                json.dumps(
                    {
                        "packages": {
                            "pkg": {
                                "repo": "old/repo",
                                "current_version": "1.0",
                                "osv_packages": "PyPI:old",
                            }
                        }
                    }
                )
            )
            args = arguments(
                "updates",
                "--repo",
                "pkg=new/repo",
                "--current-version",
                "pkg=2.0",
                "--osv-package",
                "pkg=PyPI:new",
            )
            with patch.dict("os.environ", {"XDG_CONFIG_HOME": directory}):
                rules = updates.load_rules(args, parser()[1]["updates"])
            self.assertEqual(rules["pkg"]["repo"], "new/repo")
            self.assertEqual(rules["pkg"]["current_version"], "2.0")
            self.assertEqual(
                [p["name"] for p in rules["pkg"]["osv_packages"]], ["old", "new"]
            )

    def test_explicit_missing_or_invalid_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            args = arguments("updates", "--config=" + str(path))
            with streams(), self.assertRaises(SystemExit) as exc:
                updates.load_rules(args, parser()[1]["updates"])
            self.assertEqual(exc.exception.code, 2)
            for document in (
                [],
                {"packages": []},
                {"packages": {"pkg": []}},
                {"packages": {"pkg": {"tag_regex": "v(.*)"}}},
            ):
                path.write_text(json.dumps(document))
                with streams(), self.assertRaises(SystemExit):
                    updates.load_rules(args, parser()[1]["updates"])

    def test_check_statuses_and_release_not_compared(self):
        package = {"name": "project", "version": "3.2.1", "release": "alt999"}
        for tags, status in [
            (["v3.2.1", "v3.2.4", "v4.0.1"], "UPD"),
            (["v3.2.1", "v4.0.1"], "MAJOR"),
            (["v3.2.1"], "OK"),
        ]:
            client = Client(15)
            with (
                patch.object(
                    client, "resolve_repo", return_value=("o/project", "config")
                ),
                patch.object(client, "tags", return_value=tags),
            ):
                result = updates.check_package(
                    package, {}, client, arguments("updates")
                )
            self.assertEqual(result.status, status)
        client = Client(15)
        with patch.object(
            client, "resolve_repo", return_value=(None, "outside GitHub")
        ):
            self.assertEqual(
                updates.check_package(package, {}, client, arguments("updates")).status,
                "SKIP",
            )

    def test_main_dedup_masks_json_streams_and_counts(self):
        args = arguments(
            "updates",
            "-p",
            "A*",
            "--json",
            "--updates-only",
            "--hide-skipped",
            "--no-color",
        )
        values = [
            {"name": "Alpha", "version": "1.0", "buildtime": 1},
            {"name": "Alpha", "version": "1.1", "buildtime": 2},
            {"name": "beta", "version": "1.0"},
        ]
        results = [Result("Alpha", "1.1", "alt1", status="UPD", same_major="1.2")]
        with (
            patch.object(updates, "load_rules", return_value={}),
            patch.object(updates, "package_list", return_value={"packages": values}),
            patch.object(
                updates, "package_results", return_value=(r for r in results)
            ) as worker,
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 0)
        self.assertEqual(worker.call_args.args[0], [values[1]])
        self.assertEqual(json.loads(out.getvalue())[0]["same_major"], "1.2")
        self.assertIn("UPD=1", err.getvalue())
        self.assertNotIn("Summary", out.getvalue())
        self.assertNotIn("\033", out.getvalue() + err.getvalue())

    def test_hidden_rows_still_count_and_errors_exit_two(self):
        args = arguments("updates", "--updates-only", "--hide-skipped", "--no-color")
        package = {"name": "one", "version": "1.0"}
        results = [
            Result("one", "1.0", "", status=s) for s in ("OK", "SKIP", "WARN", "ERR")
        ]
        with (
            patch.object(updates, "load_rules", return_value={}),
            patch.object(updates, "package_list", return_value={"packages": [package]}),
            patch.object(updates, "package_results", return_value=(r for r in results)),
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 2)
        self.assertNotIn("[OK]", out.getvalue())
        self.assertNotIn("[SKIP]", out.getvalue())
        for text in ("OK=1", "SKIP=1", "WARN=1", "ERR=1"):
            self.assertIn(text, err.getvalue())

    def test_osv_mode_does_not_require_git_and_keeps_warning_on_stderr(self):
        args = arguments("updates", "--vulnerable-only", "--json", "--no-color")
        result = Result(
            "one",
            "1.0",
            "alt1",
            status="VULN",
            security=True,
            vulnerabilities=[
                {"id": "CVE-2026-example", "aliases": [], "fixed_versions": []}
            ],
        )
        with (
            patch.object(updates.shutil, "which", return_value=None),
            patch.object(updates, "load_rules", return_value={}),
            patch.object(
                updates,
                "package_list",
                return_value={"packages": [{"name": "one", "version": "1.0"}]},
            ),
            patch.object(
                updates, "package_results", return_value=(r for r in [result])
            ),
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 0)
        self.assertEqual(
            json.loads(out.getvalue())[0]["vulnerabilities"][0]["id"],
            "CVE-2026-example",
        )
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("Packages with OSV matches: 1", err.getvalue())

    def test_empty_category_json_and_api_failure_streams(self):
        args = arguments("updates", "--json", "-g", "java")
        with (
            patch.object(updates, "load_rules", return_value={}),
            patch.object(
                updates,
                "package_list",
                return_value={"packages": [{"name": "one", "version": "1.0"}]},
            ),
            patch.object(updates, "filter_groups", return_value=[]),
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 0)
        self.assertEqual(json.loads(out.getvalue()), [])
        with (
            patch.object(updates, "load_rules", return_value={}),
            patch.object(updates, "package_list", side_effect=CheckError("HTTP 403")),
            streams() as (out, err),
        ):
            self.assertEqual(updates.main(args, parser()[1]["updates"]), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("HTTP 403", err.getvalue())
