import copy
import json
import unittest
from unittest.mock import patch

from repokeep import cli, tasks
from tests.support import arguments, fixture, streams


class ApprovalsTest(unittest.TestCase):
    def test_comment_only_approval_is_normalized_without_changing_api_data(self):
        response = fixture("taskoteka-approval-comments")
        original = copy.deepcopy(response)
        self.assertNotIn("approved_by", response["subtasks"]["100"])
        row = tasks.normalize(response)
        self.assertEqual(row["subtasks"]["100"]["approved_by"], ["antohami"])
        self.assertEqual(response, original)
        text = tasks.render(
            [row], arguments("task", "--all", "--color", "always"), 1, 240
        )
        self.assertIn("\033[38;5;208mapproved: antohami\033[0m", text)

    def test_approvals_alone_fetch_missing_detail_and_keep_json_consistent(self):
        response = fixture("taskoteka-approval-comments")
        summary = fixture("taskoteka-approvals-list")
        for flags in (["--approvals"], ["--all"], ["--approvals", "--json"]):
            with (
                self.subTest(flags=flags),
                patch.object(tasks, "request_json", return_value=summary),
                patch.object(
                    tasks,
                    "fetch_details",
                    return_value=([tasks.normalize(response)], []),
                ) as details,
                patch.object(
                    tasks, "fetch_statuses", side_effect=lambda rows, args: (rows, [])
                ),
                streams() as (out, err),
            ):
                self.assertEqual(
                    cli.run(
                        [
                            "task",
                            "-m",
                            "scala",
                            "-p",
                            "jackson-module-jsonSchema",
                            "--no-color",
                            *flags,
                        ]
                    ),
                    0,
                )
                details.assert_called_once()
                self.assertEqual(
                    [row["id"] for row in details.call_args.args[0]], [438004]
                )
                if "--json" in flags:
                    subtask = json.loads(out.getvalue())[0]["subtasks"]["100"]
                    self.assertEqual(subtask["approved_by"], ["antohami"])
                    self.assertEqual(
                        subtask["approval_comments"]["antohami"]["text"], "Review"
                    )
                else:
                    self.assertIn("approved: antohami", out.getvalue())
                self.assertEqual(err.getvalue(), "")
                self.assertNotIn("\033", out.getvalue())

    def test_explicit_lists_take_precedence_over_comment_fallback(self):
        for field, comments in (
            ("approved_by", "approval_comments"),
            ("disapproved_by", "disapproval_comments"),
        ):
            for users, expected in (([], []), (["cas"], ["cas"]), (None, None)):
                with self.subTest(field=field, users=users):
                    value = {field: users, comments: {"old-user": {"text": "old"}}}
                    self.assertEqual(tasks.subtask_approvals(value)[field], expected)
            for value in (
                None,
                [],
                "antohami",
                {"antohami": None},
                {"antohami": "Review"},
            ):
                with self.subTest(field=field, comments=value):
                    self.assertNotIn(field, tasks.subtask_approvals({comments: value}))
            result = tasks.subtask_approvals(
                {comments: {"cas": {}, "nbr": {"text": "Review"}}}
            )
            self.assertEqual(result[field], ["cas", "nbr"])

    def test_show_recovers_approval_in_json_and_plain_output(self):
        for flags in (["--json"], ["--no-color"]):
            with (
                self.subTest(flags=flags),
                patch.object(
                    tasks,
                    "request_json",
                    return_value=fixture("taskoteka-approval-comments"),
                ),
                streams() as (out, err),
            ):
                self.assertEqual(cli.run(["task", "show", "438004", *flags]), 0)
                if "--json" in flags:
                    self.assertEqual(
                        json.loads(out.getvalue())[0]["subtasks"]["100"]["approved_by"],
                        ["antohami"],
                    )
                else:
                    self.assertIn("approved: antohami", out.getvalue())
                self.assertEqual(err.getvalue(), "")
