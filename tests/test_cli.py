import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from repokeep import cli
from repokeep.common import color_enabled
from tests.support import arguments, streams


class CliTest(unittest.TestCase):
    def command(self, *options):
        return subprocess.run(
            [sys.executable, "-m", "repokeep", *options],
            text=True,
            capture_output=True,
            timeout=10,
        )

    def test_help_and_version_all_entry_points(self):
        for prefix in ([], ["updates"], ["tasks"]):
            for option in ("--help", "--version"):
                result = self.command(*prefix, option)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout)
                self.assertEqual(result.stderr, "")
                self.assertNotIn("\033", result.stdout)
                if option == "--version":
                    self.assertIn("0.1.0", result.stdout)
        for command in ("updates", "tasks"):
            result = self.command(command, "--color", "always", "--help")
            self.assertNotIn("\033", result.stdout)

    def test_invalid_arguments_and_no_implicit_maintainer(self):
        cases = [
            [],
            ["updates"],
            ["tasks"],
            ["updates", "-m", ""],
            ["updates", "-m", "other", "-j", "3"],
            ["updates", "-m", "other", "--vuln-details"],
            ["tasks", "-m", "ALL"],
            ["tasks", "-m", "other", "--watch", "0"],
            ["tasks", "-m", "other", "--watch", "4"],
            ["tasks", "-m", "other", "--watch", "--json"],
            ["tasks", "-m", "other", "--branch", "ALL,p11"],
            ["tasks", "-m", "other", "--state", "FAILED,,DONE"],
            ["tasks", "-m", "other", "--state", "unknown"],
            ["tasks", "-m", "other", "--state", ""],
            ["tasks", "-m", "other", "--package", ""],
            ["tasks", "-m", "other", "--limit", "-1"],
            ["tasks", "-m", "other", "--days", "0"],
            ["tasks", "-m", "other", "--details"],
            ["tasks", "-m", "other", "--with-source"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), streams() as (out, err):
                with self.assertRaises(SystemExit) as exc:
                    cli.run(argv)
                self.assertEqual(exc.exception.code, 2)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("error:", err.getvalue())
            self.assertNotIn("\033", err.getvalue())

    def test_defaults_and_ctrl_c(self):
        root, _ = cli.parser()
        args = root.parse_args(["updates", "-m", "someone"])
        self.assertEqual(
            (
                args.maintainer,
                args.branch,
                args.jobs,
                args.timeout,
                args.package_timeout,
            ),
            ("someone", "sisyphus", 1, 15, 15),
        )
        with (
            patch.object(cli, "run", side_effect=KeyboardInterrupt),
            streams() as (out, err),
        ):
            self.assertEqual(cli.main(), 130)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("Interrupted", err.getvalue())

    def test_no_color_environment_and_non_tty(self):
        class Tty:
            def isatty(self):
                return True

        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm"}):
            self.assertFalse(color_enabled("auto", Tty()))
            self.assertFalse(color_enabled("never", Tty()))
            self.assertTrue(color_enabled("always", Tty()))

    def test_common_options_have_matching_defaults_and_normalization(self):
        for command in ("updates", "tasks"):
            with self.subTest(command=command):
                args = arguments(command, "-m", " ScAlA ")
                self.assertEqual(args.maintainer, "scala")
                self.assertEqual(
                    (args.color, args.json, args.timeout), ("auto", False, 15)
                )
                for flags, color in (
                    (["--color", "always", "--no-color"], "never"),
                    (["--no-color", "--color", "always"], "always"),
                ):
                    args = arguments(
                        command, *flags, "--json", "--timeout", "7", "-b", " P11 "
                    )
                    self.assertEqual(
                        (args.branch, args.color, args.json, args.timeout),
                        ("p11", color, True, 7),
                    )
                help_text = cli.parser()[1][command].format_help()
                self.assertIn("Output options:", help_text)
                self.assertIn("respects NO_COLOR", help_text)
                self.assertIn("--package PATTERN", help_text)
                for flags in (
                    ["-m", "ALL"],
                    ["-m", "wrong@login"],
                    ["-m", "bad login"],
                    ["-b", ""],
                    ["-p", ""],
                    ["--timeout", "0"],
                ):
                    with (
                        self.subTest(command=command, flags=flags),
                        streams() as (out, err),
                    ):
                        with self.assertRaises(SystemExit) as exc:
                            arguments(command, *flags)
                        self.assertEqual(exc.exception.code, 2)
                        self.assertEqual(out.getvalue(), "")
                        self.assertIn("error:", err.getvalue())

    def test_command_specific_options_keep_their_meaning(self):
        update = arguments(
            "updates", "-j", "2", "--sort", "--repo", "example=owner/repo"
        )
        task = arguments("tasks", "--sort", "package", "--repo", "p11", "--all")
        self.assertEqual(update.jobs, 2)
        self.assertIs(update.sort, True)
        self.assertEqual(update.repo, [("example", "owner/repo")])
        self.assertEqual(
            (task.sort, task.branch, task.arch, task.source),
            ("package", "p11", True, True),
        )
