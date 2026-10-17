import copy
import json
import unittest
from unittest.mock import Mock, patch

from repokeep import task_status, tasks
from repokeep.common import CheckError
from tests.support import arguments, fixture, progress_task, streams


class CurrentBuildsTest(unittest.TestCase):
    def test_saved_failed_attempt_only_marks_first_subtask_red(self):
        raw = fixture("taskoteka-363755")
        row = tasks.normalize(raw)
        self.assertTrue(
            all(
                row["subtasks"][number]["build"]["x86_64"] == "failed"
                for number in ("4", "20", "40", "100")
            )
        )
        client = Mock(timeout=15)
        client.request.side_effect = [
            json.dumps(fixture("altrepo-363755")),
            fixture("girar-events-363755")["text"],
        ]
        result, warning = task_status.load_status(client, row)
        self.assertIsNone(warning)
        self.assertEqual(result["stage"], "Task build")
        self.assertIsNone(result["failed_check"])
        self.assertEqual(tasks.normalize(raw), row)
        self.assertEqual(
            [n for n, _, _ in tasks.package_rows(result)], ["4", "20", "40", "100"]
        )
        for number, _, subtask in tasks.package_rows(result):
            for arch in tasks.ARCHITECTURES:
                with self.subTest(number=number, arch=arch):
                    self.assertIs(
                        tasks.arch_status(subtask, arch),
                        tasks.ArchStatus.FAILED
                        if number == "4"
                        else tasks.ArchStatus.MISSING,
                    )
        args = arguments("tasks", "-m", "cas", "--all", "--color", "always")
        text = tasks.render([result], args, 1, 220)
        for arch in tasks.ARCHITECTURES:
            self.assertEqual(text.count("\033[1;31m" + arch), 1)
            self.assertEqual(text.count("\033[90m" + arch), 3)
        self.assertEqual(text.count("\033[1;31m  [FAILED]"), 1)
        self.assertEqual(result["subtasks"]["10"], row["subtasks"]["10"])

    def test_saved_mixed_success_failure_and_unstarted_subtasks(self):
        row = tasks.normalize(fixture("taskoteka-363764"))
        self.assertEqual(row["subtasks"]["140"]["build"]["x86_64"], "failed")
        result = task_status.current_subtasks(row, fixture("altrepo-363764"))
        for number, expected in (
            ("100", tasks.ArchStatus.SUCCESS),
            ("120", tasks.ArchStatus.FAILED),
            ("140", tasks.ArchStatus.MISSING),
            ("200", tasks.ArchStatus.MISSING),
        ):
            for arch in tasks.ARCHITECTURES:
                with self.subTest(number=number, arch=arch):
                    self.assertIs(tasks.arch_status(result[number], arch), expected)

    def test_json_and_plain_output_use_current_build_results(self):
        row = tasks.normalize(fixture("taskoteka-363755"))
        row["subtasks"] = task_status.current_subtasks(row, fixture("altrepo-363755"))
        args = arguments("tasks", "-m", "cas", "--all", "--json", "--color", "always")
        with (
            patch.object(tasks, "snapshot", return_value=([row], 1, [])),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 0)
        result = json.loads(out.getvalue())[0]
        self.assertEqual(result["subtasks"]["4"]["build"]["x86_64"], "failed")
        self.assertEqual(result["subtasks"]["20"]["build"], {})
        self.assertIn("source", result["subtasks"]["100"])
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("\033", out.getvalue())
        text = tasks.render([row], arguments("tasks", "--all", "--no-color"), 1, 160)
        self.assertNotIn("\033", text)
        self.assertEqual(text.count("x86_64"), 4)

    def test_current_architecture_states_do_not_promote_unknown_results(self):
        progress = fixture("altrepo-progress-437493")
        row = progress_task(progress)
        row["subtasks"]["100"]["build"] = {"x86_64": "cached", "i586": "failed"}
        for status, expected, stored in (
            ("processed", tasks.ArchStatus.SUCCESS, "cached"),
            ("failed", tasks.ArchStatus.FAILED, "failed"),
            ("started", tasks.ArchStatus.WAITING, "building"),
            ("excluded", tasks.ArchStatus.SKIPPED, "excluded"),
            ("swept", tasks.ArchStatus.SKIPPED, "swept"),
            ("pending", tasks.ArchStatus.WAITING, "pending"),
            ("future-result", tasks.ArchStatus.UNKNOWN, "future-result"),
        ):
            with self.subTest(status=status):
                progress["subtasks"][0]["archs"] = [
                    {"arch": "x86_64", "stage_status": status}
                ]
                subtask = task_status.current_subtasks(row, progress)["100"]
                self.assertIs(tasks.arch_status(subtask, "x86_64"), expected)
                self.assertIs(
                    tasks.arch_status(subtask, "i586"), tasks.ArchStatus.MISSING
                )
                self.assertEqual(subtask["build"]["x86_64"], stored)
        progress["subtasks"][0]["archs"] = [
            {"arch": "i586", "stage_status": "processed"}
        ]
        self.assertEqual(
            task_status.current_subtasks(row, progress)["100"]["build"],
            {"i586": "built"},
        )

    def test_invalid_progress_architectures_do_not_change_source_data(self):
        row = progress_task(fixture("altrepo-progress-437493"))
        original = copy.deepcopy(row)
        for entries in (
            None,
            {},
            [None],
            [{"subtask_id": True, "archs": []}],
            [{"subtask_id": -1, "archs": []}],
            [{"subtask_id": 100}],
            [{"subtask_id": 100, "archs": None}],
            [{"subtask_id": 100, "archs": [None]}],
            [{"subtask_id": 100, "archs": [{"arch": "x86_64"}]}],
            [{"subtask_id": 100, "archs": [{"arch": "x86_64", "stage_status": None}]}],
            [{"subtask_id": 100, "archs": []}] * 2,
            [
                {
                    "subtask_id": 100,
                    "archs": [{"arch": "x86_64", "stage_status": "processed"}] * 2,
                }
            ],
        ):
            with self.subTest(entries=entries):
                with self.assertRaises(CheckError):
                    task_status.current_subtasks(row, {"subtasks": entries})
                self.assertEqual(row, original)

    def test_mismatched_attempt_never_replaces_build_results(self):
        row = tasks.normalize(fixture("taskoteka-363755"))
        progress = fixture("altrepo-363755")
        for key in ("task_try", "task_iter"):
            with self.subTest(key=key):
                response = dict(progress, **{key: progress[key] + 1})
                client = Mock(timeout=15)
                client.request.return_value = json.dumps(response)
                result, warning = task_status.load_status(client, row)
                self.assertEqual(result["subtasks"], row["subtasks"])
                self.assertIsNone(result["stage"])
                self.assertIsNone(warning)
                client.request.assert_called_once()
