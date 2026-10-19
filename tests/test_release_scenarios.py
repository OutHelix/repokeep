import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from repokeep import updates, upstream
from repokeep.cli import parser
from repokeep.models import Result
from repokeep.upstream import Client
from tests.support import arguments, fixture, streams


class ReleaseScenariosTest(unittest.TestCase):
    def setUp(self):
        git = patch.object(updates.shutil, "which", return_value="/test/git")
        git.start()
        self.addCleanup(git.stop)

    def test_fixed_version_scenarios_through_package_checks(self):
        for case in fixture("version-scenarios"):
            with self.subTest(
                name=case["name"], current=case["current"], regex=case.get("regex")
            ):
                client = Client(15)
                rule = {"repo": case["repo"]}
                if "regex" in case:
                    rule["compiled_regex"] = re.compile(case["regex"])
                args = arguments(
                    "update",
                    *(["--include-prereleases"] if case.get("prereleases") else []),
                )
                with patch.object(client, "tags", return_value=case["tags"]):
                    result = updates.check_package(
                        {
                            "name": case["name"],
                            "version": case["current"],
                            "release": "alt999",
                        },
                        rule,
                        client,
                        args,
                    )
                self.assertEqual(
                    (result.status, result.same_major), (case["status"], case["same"])
                )
                self.assertEqual(
                    [item["version"] for item in result.newer_majors], case["majors"]
                )
                if "note" in case:
                    self.assertIn(case["note"], result.note)

    def test_real_tag_snapshots_do_not_mix_components(self):
        for repo, name, current, latest, majors in (
            ("vsch/flexmark-java", "flexmark-java", "0.64.0", "0.64.6", []),
            ("apache/maven", "maven", "3.8.8", "3.10.0", []),
            ("google/guava", "guava", "31.0", "31.1", ["32.1.3", "33.7.2"]),
        ):
            with self.subTest(repo=repo):
                client = Client(15)
                with patch.object(
                    client,
                    "tags",
                    return_value=fixture("github-" + repo.replace("/", "-")),
                ):
                    result = updates.check_package(
                        {"name": name, "version": current},
                        {"repo": repo},
                        client,
                        arguments("update"),
                    )
                self.assertEqual(result.status, "UPD")
                self.assertEqual(result.same_major, latest)
                self.assertEqual([v["version"] for v in result.newer_majors], majors)
        client = Client(15)
        with patch.object(client, "tags", return_value=fixture("github-google-guava")):
            result = updates.check_package(
                {"name": "guava", "version": "31.0-jre"},
                {"repo": "google/guava"},
                client,
                arguments("update"),
            )
        self.assertEqual(result.status, "ERR")
        self.assertIn("no recognizable version tags", result.note)

    def test_git_launch_and_tag_parser_use_fixed_refs(self):
        tags = fixture("github-google-guava")
        output = "\n".join("a" * 40 + "\trefs/tags/" + tag for tag in tags)
        output += "\n" + "b" * 40 + "\trefs/tags/v31.0^{}\nnoise\n"
        process = Mock(stdout=io.StringIO(), stderr=io.StringIO(), returncode=0)
        process.communicate.return_value = (output, "")
        with patch.object(upstream.subprocess, "Popen", return_value=process) as launch:
            self.assertEqual(Client(15).tags("google/guava"), tags)
        command = launch.call_args.args[0]
        self.assertEqual(
            command[-4:],
            ["ls-remote", "--tags", "--refs", "https://github.com/google/guava.git"],
        )
        self.assertEqual(launch.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_cli_overrides_win_over_json_config_without_metadata_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "packages": {
                            "pkg": {
                                "repo": "old/repo",
                                "tag_regex": "old-(?P<version>.*)",
                                "current_version": "9.0",
                            }
                        }
                    }
                )
            )
            args = arguments(
                "update",
                "--config",
                str(config),
                "--repo",
                "pkg=owner/monorepo",
                "--tag-regex",
                r"pkg=component-v(?P<version>\d+\.\d+)",
                "--current-version",
                "pkg=1.2",
            )
            rules = updates.load_rules(args, parser()[1]["update"])
            client = Client(15)
            with (
                patch.object(
                    client,
                    "json",
                    side_effect=AssertionError("override must bypass spec lookup"),
                ),
                patch.object(
                    client,
                    "tags",
                    return_value=["component-v1.2", "component-v1.3", "other-v99.0"],
                ),
            ):
                result = updates.check_package(
                    {
                        "name": "pkg",
                        "version": "unusual-RPM-Version",
                        "release": "alt100",
                    },
                    rules["pkg"],
                    client,
                    args,
                )
            self.assertEqual(
                (result.repo, result.status, result.same_major),
                ("owner/monorepo", "UPD", "1.3"),
            )
            self.assertEqual(result.current, "unusual-RPM-Version")

    def test_real_category_response_intersects_owned_names(self):
        args = arguments("update", "-g", "java")
        packages = fixture("altrepo-packages")["packages"]
        with patch.object(
            updates, "request_data", return_value=fixture("altrepo-java")
        ):
            selected = updates.filter_groups(packages, args)
        names = {p["name"] for p in selected}
        self.assertEqual(len(names), 169)
        self.assertTrue({"flexmark-java", "guava", "maven", "jackson-core"} <= names)
        self.assertNotIn("python3-module-pycep-parser", names)

    def test_saved_maintainers_and_large_viy_list_through_main(self):
        for owner, count in (("cas", 699), ("grenka", 743), ("viy", 2976)):
            args = arguments("update", "-m", owner, "--json", "--no-color")

            def results(packages, rules, options):
                for package in packages:
                    yield Result(
                        package["name"],
                        package["version"],
                        package["release"],
                        status="OK",
                    )

            with (
                self.subTest(owner=owner),
                patch.object(updates, "load_rules", return_value={}),
                patch.object(
                    updates, "package_list", return_value=fixture("altrepo-" + owner)
                ),
                patch.object(updates, "package_results", side_effect=results),
                streams() as (out, err),
            ):
                self.assertEqual(updates.main(args, parser()[1]["update"]), 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(len(rows), count)
            names = [p["name"] for p in rows]
            self.assertEqual(names, sorted(names, key=str.lower))
            self.assertEqual(len(set(names)), count)
            self.assertIn("OK=" + str(count), err.getvalue())

    def test_unmatched_masks_and_bad_package_shapes_are_errors(self):
        for response, options in (
            ({}, []),
            ({"packages": None}, []),
            ({"packages": [{}]}, []),
            ({"packages": []}, []),
            ({"packages": [{"name": "pkg", "version": "1.0"}]}, ["-p", "missing*"]),
        ):
            args = arguments("update", "--json", "--no-color", *options)
            with (
                self.subTest(response=response, options=options),
                patch.object(updates, "load_rules", return_value={}),
                patch.object(updates, "package_list", return_value=response),
                streams() as (out, err),
            ):
                if options:
                    with self.assertRaises(SystemExit) as exc:
                        updates.main(args, parser()[1]["update"])
                    self.assertEqual(exc.exception.code, 2)
                else:
                    self.assertEqual(updates.main(args, parser()[1]["update"]), 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("error:", err.getvalue())
