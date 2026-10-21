import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from repokeep import __version__, cli
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
        for prefix in ([], ["update"], ["task"], ["task", "show"]):
            for option in ("--help", "--version"):
                result = self.command(*prefix, option)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout)
                self.assertEqual(result.stderr, "")
                self.assertNotIn("\033", result.stdout)
                if option == "--version":
                    self.assertEqual(
                        result.stdout,
                        " ".join(["repokeep", *prefix, __version__]) + "\n",
                    )
        for command in ("update", "task"):
            result = self.command(command, "--color", "always", "--help")
            self.assertNotIn("\033", result.stdout)

    def test_invalid_arguments_and_no_implicit_maintainer(self):
        cases = [
            [],
            ["update"],
            ["task"],
            ["updates", "-m", "other"],
            ["tasks", "-m", "other"],
            ["task", "show"],
            ["task", "show", "0"],
            ["task", "show", "-1"],
            ["task", "show", "invalid"],
            ["task", "show", "1", "2"],
            ["task", "show", "1", "--timeout", "0"],
            ["task", "show", "1", "-m", "ALL"],
            ["task", "--limit", "1", "show", "1"],
            ["update", "-m", ""],
            ["update", "-m", "other", "-j", "3"],
            ["update", "-m", "other", "--vuln-details"],
            ["task", "-m", "ALL"],
            ["task", "-m", "other", "--watch", "0"],
            ["task", "-m", "other", "--watch", "2"],
            ["task", "-m", "other", "--watch", "--json"],
            ["task", "-m", "other", "--branch", "ALL,p11"],
            ["task", "-m", "other", "--state", "FAILED,,DONE"],
            ["task", "-m", "other", "--state", "unknown"],
            ["task", "-m", "other", "--state", ""],
            ["task", "-m", "other", "--package", ""],
            ["task", "-m", "other", "--limit", "-1"],
            ["task", "-m", "other", "--days", "0"],
            ["task", "-m", "other", "--details"],
            ["task", "-m", "other", "--with-source"],
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
        args = root.parse_args(["update", "-m", "someone"])
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
        for command in ("update", "task"):
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
            "update", "-j", "2", "--sort", "--repo", "example=owner/repo"
        )
        task = arguments("task", "--sort", "package", "--repo", "p11", "--all")
        self.assertEqual(update.jobs, 2)
        self.assertIs(update.sort, True)
        self.assertEqual(update.repo, [("example", "owner/repo")])
        self.assertEqual(
            (task.sort, task.branch, task.arch, task.source, task.approvals),
            ("package", "p11", True, True, True),
        )
