import json
import unittest
from unittest.mock import patch

from repokeep import tasks
from tests.support import arguments, fixture, frozen_time, plain_text, streams


class TaskOptionsTest(unittest.TestCase):
    def test_default_time_order_and_limit_keep_latest_execution_first(self):
        values = fixture("taskoteka-list")
        for flags, expected in (
            ([], [432157, 435564]),
            (["--sort", "time"], [432157, 435564]),
            (["--sort", "id"], [435995, 435919]),
            (["--reverse"], [413835, 432607]),
        ):
            with (
                self.subTest(flags=flags),
                patch.object(tasks, "request_json", return_value=values),
                patch.object(
                    tasks, "fetch_details", side_effect=lambda rows, args: (rows, [])
                ) as details,
                patch.object(
                    tasks, "fetch_statuses", side_effect=lambda rows, args: (rows, [])
                ),
            ):
                rows, total, warnings = tasks.snapshot(
                    arguments("task", "--limit", "2", "--arch", *flags)
                )
                self.assertEqual([row["id"] for row in rows], expected)
                self.assertEqual(
                    [row["id"] for row in details.call_args.args[0]], expected
                )
                self.assertEqual(total, 9)
                self.assertEqual(warnings, [])
        self.assertEqual(arguments("task").sort, "time")

    def test_missing_invalid_dates_and_ties_are_deterministic_in_any_timezone(self):
        values = [
            {
                "id": identifier,
                "owner": "scala",
                "repo": "sisyphus",
                "state": "TESTED",
                "build_time": stamp,
                "created": "2099-01-01T00:00:00",
                "updated": 4102444800,
            }
            for identifier, stamp in (
                (100, "2026-10-18T12:00:00"),
                (2, "2026-10-19T12:00:00"),
                (3, "2026-10-19T12:00:00"),
                (6, None),
                (7, ""),
                (8, "not a date"),
                (9, "2026-02-30T12:00:00"),
            )
        ]
        values[3].pop("build_time")
        expected = [3, 2, 100, 9, 8, 7, 6]
        for tz in ("UTC", "EST5"):
            with self.subTest(tz=tz), frozen_time(tz=tz):
                self.assertEqual(
                    [
                        row["id"]
                        for row in tasks.select_tasks(values, arguments("task"))
                    ],
                    expected,
                )
                self.assertEqual(
                    [
                        row["id"]
                        for row in tasks.select_tasks(
                            values, arguments("task", "--reverse")
                        )
                    ],
                    list(reversed(expected)),
                )

    def test_explicit_date_offsets_sort_by_the_same_clock(self):
        values = [
            {
                "id": identifier,
                "owner": "scala",
                "repo": "p11",
                "state": "TESTED",
                "build_time": stamp,
            }
            for identifier, stamp in (
                (1, "2026-10-19T12:00:00+03:00"),
                (2, "2026-10-19T10:00:00Z"),
                (3, "2026-10-19T09:30:00+00:00"),
            )
        ]
        self.assertEqual(
            [row["id"] for row in tasks.select_tasks(values, arguments("task"))],
            [2, 3, 1],
        )

    def test_approvals_use_summary_and_keep_all_users_and_packages_in_json(self):
        args = arguments("task", "--approvals", "--json", "-p", "jackson-core")
        with (
            patch.object(tasks, "request_json", return_value=fixture("taskoteka-list")),
            patch.object(tasks, "fetch_details") as details,
            patch.object(
                tasks, "fetch_statuses", side_effect=lambda rows, args: (rows, [])
            ),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 0)
        details.assert_not_called()
        rows = json.loads(out.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 432607)
        self.assertEqual(len(tasks.package_rows(rows[0])), 5)
        for number in ("200", "300", "320", "340", "400"):
            self.assertEqual(rows[0]["subtasks"][number]["approved_by"], ["cas", "nbr"])
        self.assertEqual(rows[0]["subtasks"]["340"]["disapproved_by"], ["amakeenk"])
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("\033", out.getvalue())

    def test_approval_rows_wrap_without_losing_names_or_changing_source_styles(self):
        row = next(task for task in fixture("taskoteka-list") if task["id"] == 432607)
        for width, flags in (
            (1, ["--approvals"]),
            (40, ["--approvals"]),
            (80, ["--all"]),
            (240, ["--all"]),
        ):
            with self.subTest(width=width, flags=flags):
                plain = tasks.render(
                    [row], arguments("task", *flags, "--no-color"), 1, width
                )
                colored = tasks.render(
                    [row], arguments("task", *flags, "--color", "always"), 1, width
                )
                self.assertEqual(plain_text(colored), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()))
                normalized = "".join(plain.replace("│", "").split())
                self.assertEqual(normalized.count("approved:cas,nbr"), 5)
                self.assertEqual(normalized.count("disapproved:amakeenk"), 1)
                for name in tasks.packages(row):
                    self.assertIn(name, normalized)
                self.assertNotIn("https://", plain)
        colored = tasks.render(
            [row], arguments("task", "--approvals", "--color", "always"), 1, 240
        )
        self.assertIn("\033[38;5;208mapproved: cas, nbr\033[0m", colored)
        self.assertIn("\033[1;31mdisapproved: amakeenk\033[0m", colored)
        other = next(line for line in colored.splitlines() if "#200" in line)
        self.assertNotIn("amakeenk", other)
        plain = tasks.render([row], arguments("task", "--no-color"), 1, 240)
        self.assertNotIn("approved:", plain)
        self.assertNotIn("disapproved:", plain)

    def test_empty_duplicate_and_malformed_approval_lists(self):
        row = {
            "id": 1,
            "owner": "scala",
            "repo": "p11",
            "state": "TESTED",
            "subtasks": {"100": {"type": "repo", "pkgname": "example"}},
        }
        args = arguments("task", "--approvals", "--no-color")
        subtask = row["subtasks"]["100"]
        for flags in ([], ["--arch", "--source"]):
            self.assertEqual(
                tasks.render(
                    [row],
                    arguments("task", *flags, "--approvals", "--no-color"),
                    1,
                    100,
                ),
                tasks.render([row], arguments("task", *flags, "--no-color"), 1, 100),
            )
        for names, expected in (
            ([], None),
            (None, None),
            (["rider", "cas", "rider"], "approved: cas, rider"),
            ("cas", None),
            (["cas", 42], None),
            (False, None),
        ):
            with self.subTest(names=names):
                subtask["approved_by"] = names
                text = tasks.render([row], args, 1, 100)
                if expected:
                    self.assertIn(expected, text)
                else:
                    self.assertEqual(
                        text,
                        tasks.render([row], arguments("task", "--no-color"), 1, 100),
                    )
                self.assertNotIn("\033", text)
                self.assertEqual(subtask["approved_by"], names)
        row["subtasks"]["200"] = {
            "type": "repo",
            "pkgname": "approved-package",
            "approved_by": ["cas"],
        }
        text = tasks.render([row], args, 1, 100)
        empty = next(line for line in text.splitlines() if "#100" in line)
        self.assertNotIn("│", empty)
        self.assertNotIn("|", empty)
        self.assertNotIn("approved:", empty)
        self.assertIn("approved: cas", text)
