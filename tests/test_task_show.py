import json
import os
import unittest
from unittest.mock import Mock, patch

from repokeep import cli, task_status, tasks
from repokeep.common import CheckError
from tests.support import Schedule, fixture, streams


class TaskShowTest(unittest.TestCase):
    def test_show_infers_owner_and_displays_all_views_with_one_detail_request(self):
        response = fixture("taskoteka-detail")
        response["subtasks"]["40"]["approved_by"] = ["cas", "nbr"]
        with (
            patch.object(tasks, "request_json", return_value=response) as request,
            patch.object(tasks, "fetch_details") as details,
            patch.dict(os.environ, {"COLUMNS": "240"}),
            streams() as (out, err),
        ):
            self.assertEqual(cli.run(["task", "show", "435564", "--no-color"]), 0)
        details.assert_not_called()
        request.assert_called_once()
        args, url = request.call_args.args
        self.assertEqual(url, tasks.API + "/tasks/435564")
        self.assertEqual((args.maintainer, args.branch), ("scala", "sisyphus"))
        self.assertEqual((args.arch, args.source, args.approvals), (True, True, True))
        text = out.getvalue()
        for value in (
            "Task 435564: scala / sisyphus",
            "[TESTED]",
            "#40",
            "#460",
            "python3-module-owlrl.git=7.6.2-alt1",
            "x86_64  i586  aarch64",
            "approved: cas, nbr",
        ):
            self.assertIn(value, text)
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("\033", text)

    def test_done_task_is_available_directly_and_json_keeps_all_details(self):
        response = dict(fixture("taskoteka-detail"), state="DONE")
        with (
            patch.object(tasks, "request_json", return_value=response) as request,
            streams() as (out, err),
        ):
            self.assertEqual(
                cli.run(["task", "show", "435564", "--json", "--color", "always"]), 0
            )
        request.assert_called_once()
        values = json.loads(out.getvalue())
        self.assertEqual(len(values), 1)
        task = values[0]
        self.assertEqual(
            (task["id"], task["state"], task["owner"]), (435564, "DONE", "scala")
        )
        self.assertEqual(task["subtasks"]["100"], response["subtasks"]["100"])
        subtask = task["subtasks"]["40"]
        self.assertEqual(subtask["build"]["x86_64"], "built")
        self.assertEqual(
            subtask["source"]["label"], "python3-module-owlrl.git=7.6.2-alt1"
        )
        self.assertIn("/435564/gears/40/git", subtask["source"]["url"])
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("\033", out.getvalue())

    def test_common_options_work_before_and_after_show(self):
        for options in (
            [
                "task",
                "-m",
                "CaS",
                "--json",
                "--no-color",
                "--timeout",
                "7",
                "show",
                "363755",
            ],
            [
                "task",
                "show",
                "363755",
                "-m",
                "CaS",
                "--json",
                "--no-color",
                "--timeout",
                "7",
            ],
            [
                "task",
                "--color",
                "always",
                "show",
                "363755",
                "-m",
                "CaS",
                "--json",
                "--no-color",
                "--timeout",
                "7",
            ],
        ):
            with self.subTest(options=options):
                root, commands = cli.parser()
                args = root.parse_args(options)
                tasks.validate_args(args, commands["show"])
                self.assertEqual(
                    (args.maintainer, args.color, args.json, args.timeout),
                    ("cas", "never", True, 7),
                )
                self.assertEqual(
                    (args.task_id, args.all, args.arch, args.source, args.approvals),
                    (363755, True, True, True, True),
                )
        root, commands = cli.parser()
        args = root.parse_args(["task", "show", "435564"])
        tasks.validate_args(args, commands["show"])
        self.assertEqual(
            (args.maintainer, args.color, args.json, args.timeout),
            (None, "auto", False, 15),
        )

    def test_owner_check_and_invalid_detail_responses_have_error_status(self):
        response = fixture("taskoteka-detail")
        for options, reply, error in (
            (["-m", "cas"], response, "owned by scala"),
            ([], dict(response, taskid=435565), "requested ID"),
            ([], [], "task object"),
            ([], {"taskid": 435564}, "missing owner/state/repo"),
        ):
            with (
                self.subTest(options=options, reply=reply),
                patch.object(tasks, "request_json", return_value=reply),
                streams() as (out, err),
            ):
                self.assertEqual(cli.run(["task", "show", "435564", *options]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(error, err.getvalue())
                self.assertNotIn("\033", err.getvalue())
        with (
            patch.object(tasks, "request_json", return_value=response),
            streams() as (out, err),
        ):
            self.assertEqual(cli.run(["task", "show", "435564", "-m", "scala"]), 0)
            self.assertEqual(err.getvalue(), "")

    def test_http_json_and_timeout_errors_stay_on_stderr(self):
        for error in (
            "HTTP 404 from git.altlinux.org",
            "invalid JSON from Taskoteka API",
            "Taskoteka request timed out",
        ):
            with (
                self.subTest(error=error),
                patch.object(tasks, "request_json", side_effect=CheckError(error)),
                streams() as (out, err),
            ):
                self.assertEqual(cli.run(["task", "show", "435564", "--json"]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn(error, err.getvalue())

    def test_failed_task_keeps_current_attempt_architectures_and_sources(self):
        response = fixture("taskoteka-363755")
        client = Mock(timeout=15)
        client.request.side_effect = [
            json.dumps(fixture("altrepo-363755")),
            fixture("girar-events-363755")["text"],
        ]
        enriched = task_status.load_status(client, tasks.normalize(response))
        schedule = Schedule([363755], {363755: enriched})
        with (
            patch.object(tasks, "request_json", return_value=response),
            schedule.patch_workers(tasks),
            patch.dict(os.environ, {"COLUMNS": "240"}),
            streams() as (out, err),
        ):
            self.assertEqual(
                cli.run(["task", "show", "363755", "--color", "always"]), 0
            )
        text = out.getvalue()
        self.assertIn("Task 363755: cas / c9f2", text)
        self.assertIn("Task build", text)
        for arch in tasks.ARCHITECTURES:
            self.assertEqual(text.count("\033[1;31m" + arch), 1)
            self.assertEqual(text.count("\033[90m" + arch), 3)
        self.assertIn("maven-compiler-plugin-3.8.0-alt1_1jpp8.src.rpm", text)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(schedule.active, set())
