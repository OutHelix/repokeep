import json
import unittest
from unittest.mock import patch

from repokeep import cli, tasks
from tests.support import arguments, fixture, plain_text, streams


class TaskArchitecturesTest(unittest.TestCase):
    def test_saved_exclusion_is_struck_through_without_changing_other_columns(self):
        row = tasks.normalize(fixture("taskoteka-excluded"))
        self.assertEqual(row["subtasks"]["100"]["build"]["i586"], "excluded")
        self.assertIs(
            tasks.arch_status(row["subtasks"]["100"], "i586"),
            tasks.ArchStatus.EXCLUDED,
        )
        for width in (1, 40, 120, 240):
            with self.subTest(width=width):
                colored = tasks.render(
                    [row], arguments("task", "--all", "--color", "always"), 1, width
                )
                plain = tasks.render(
                    [row], arguments("task", "--all", "--no-color"), 1, width
                )
                self.assertEqual(plain_text(colored), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()))
                if width >= 120:
                    self.assertIn("\033[9;90mi586\033[0m", colored)
                    self.assertIn("\033[32mx86_64\033[0m", colored)
                    self.assertIn("\033[32maarch64\033[0m", colored)
                    self.assertEqual(colored.count("\033[9;90m"), 1)
                    self.assertIn("\033[38;5;208mapproved: cas\033[0m", colored)

    def test_only_explicit_exclusion_gets_strikethrough(self):
        for kind, status, code in (
            ("repo", "excluded", "9;90"),
            ("srpm", "excluded", "9;90"),
            ("repo", "built", "32"),
            ("repo", "cached", "32"),
            ("repo", "failed", "1;31"),
            ("repo", "swept", "90"),
            ("repo", "waiting", "90"),
            ("repo", "building", "90"),
            ("repo", "pending", "90"),
            ("repo", None, "90"),
            ("repo", "future-state", "90"),
            ("copy", None, "90"),
            ("delete", None, "90"),
        ):
            with self.subTest(kind=kind, status=status):
                row = tasks.normalize(fixture("taskoteka-excluded"))
                subtask = row["subtasks"]["100"]
                subtask.update(type=kind, build={"i586": status})
                text = tasks.render(
                    [row], arguments("task", "--arch", "--color", "always"), 1, 240
                )
                self.assertIn("\033[" + code + "mi586\033[0m", text)
                self.assertEqual(text.count("\033[9;90m"), int(status == "excluded"))

    def test_show_json_preserves_exclusion_and_disabled_colors_stay_plain(self):
        for flags in (
            ["--json", "--color", "always"],
            ["--color", "never"],
            ["--color", "always", "--no-color"],
        ):
            with (
                self.subTest(flags=flags),
                patch.object(
                    tasks, "request_json", return_value=fixture("taskoteka-excluded")
                ) as request,
                streams() as (out, err),
            ):
                self.assertEqual(cli.run(["task", "show", "437537", *flags]), 0)
                request.assert_called_once()
                self.assertEqual(err.getvalue(), "")
                self.assertNotIn("\033", out.getvalue())
                if "--json" in flags:
                    subtask = json.loads(out.getvalue())[0]["subtasks"]["100"]
                    self.assertEqual(subtask["build"]["i586"], "excluded")
                    self.assertEqual(subtask["build"]["x86_64"], "built")
                else:
                    self.assertIn("i586", out.getvalue())
