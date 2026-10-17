import json
import re
import unittest
from unittest.mock import Mock, patch

from repokeep import task_status, tasks
from repokeep.common import CheckError
from tests.support import Schedule, arguments, fixture, progress_task, streams


class TaskStatusTest(unittest.TestCase):
    def test_real_check_failures_are_independent_of_architectures(self):
        for identifier, expected in (
            (437142, "Noarch check"),
            (437493, "[aarch64] install check"),
        ):
            with self.subTest(identifier=identifier):
                progress = fixture(f"altrepo-progress-{identifier}")
                row = progress_task(progress)
                number = str(progress["subtasks"][0]["subtask_id"])
                row["subtasks"] = {number: row["subtasks"]["100"]}
                log = fixture("girar-check-logs")[str(identifier)]
                client = Mock(timeout=15)
                client.request.side_effect = [json.dumps(progress), log["text"]]
                result, warning = task_status.load_status(client, row)
                self.assertIsNone(warning)
                self.assertEqual(result["failed_check"], expected)
                self.assertEqual(result["subtasks"], row["subtasks"])
                self.assertEqual(client.request.call_args_list[1].args, (log["url"],))
                for arch in tasks.ARCHITECTURES:
                    self.assertIs(
                        tasks.arch_status(result["subtasks"][number], arch),
                        tasks.ArchStatus.SUCCESS,
                    )

    def test_elf_failure_survives_other_successes_and_ignores_log_noise(self):
        log = "\n".join(
            [
                " - Changelog mentions ELF symbols check FAILED",
                "2026-Oct-17 12:00:00 :: [x86_64] ELF symbols check FAILED",
                "2026-Oct-17 12:00:01 :: [i586] ELF symbols check OK",
                "2026-Oct-17 12:00:02 :: [x86_64] install check OK",
                "2026-Oct-17 12:00:03 :: task #437142 for sisyphus FAILED",
            ]
        )
        self.assertEqual(task_status.failed_check(log), "[x86_64] ELF symbols check")
        self.assertIsNone(
            task_status.failed_check(
                log + "\n2026-Oct-17 12:00:04 :: [x86_64] ELF symbols check OK"
            )
        )
        self.assertIsNone(
            task_status.failed_check(fixture("girar-check-logs")["435995"]["text"])
        )

    def test_stage_labels_and_unknown_stages(self):
        expected = fixture("task-stage-labels")
        self.assertEqual(task_status.STAGES, expected)
        for raw, label in (
            ("task-check-acl", "task-check-acl"),
            ("task-check-build-version", "task-check-build-version"),
            ("task-repo-check-provides", "task-repo-check-provides"),
            ("task-arepo-save", "task-arepo-save"),
            ("task-future-check", "task-future-check"),
            ("task-shelf-check", "task-shelf-check"),
            ("task-future-elf-check", "task-future-elf-check"),
            ("ELF symbols check", "ELF symbols check"),
            ("", ""),
        ):
            with self.subTest(stage=raw):
                self.assertEqual(task_status.stage_label(raw), label)

    def test_stale_attempt_iteration_and_state_are_not_shown(self):
        progress = fixture("altrepo-progress-437142")
        for changed in (
            {"try": 2},
            {"iter": 2},
            {"state": "BUILDING"},
            {"try": None},
            {"iter": None},
            {"try": True},
            {"iter": "1"},
        ):
            with self.subTest(changed=changed):
                row = dict(progress_task(progress), **changed)
                client = Mock(timeout=15)
                client.request.return_value = json.dumps(progress)
                result, warning = task_status.load_status(client, row)
                self.assertIsNone(result["stage"])
                self.assertIsNone(result["failed_check"])
                self.assertIsNone(warning)
                client.request.assert_called_once()

    def test_building_headers_use_verified_stage_names(self):
        source = fixture("altrepo-progress-437142")
        for identifier, label in fixture("task-stage-labels").items():
            with self.subTest(stage=identifier):
                progress = dict(source, task_state="BUILDING", task_stage=identifier)
                client = Mock(timeout=15)
                client.request.return_value = json.dumps(progress)
                row, warning = task_status.load_status(client, progress_task(progress))
                args = arguments("tasks", "-m", row["owner"], "--no-color")
                header = tasks.render([row], args, 1, 120).split("#100")[0]
                self.assertIn("  |  " + label, header)
                self.assertIsNone(row["failed_check"])
                self.assertIsNone(warning)
                client.request.assert_called_once()

    def test_active_completed_and_permission_states(self):
        source = fixture("altrepo-progress-437142")
        for state in tasks.STATE_COLORS:
            with self.subTest(state=state):
                stage = "task-check-install"
                progress = dict(source, task_state=state, task_stage=stage)
                client = Mock(timeout=15)
                client.request.side_effect = [json.dumps(progress), ""]
                row = dict(
                    progress_task(progress), stage="Old stage", failed_check="ACL check"
                )
                result, warning = task_status.load_status(client, row)
                self.assertEqual(
                    result["stage"],
                    "Install check" if state in task_status.STATES else None,
                )
                self.assertIsNone(result["failed_check"])
                self.assertIsNone(warning)
                self.assertEqual(
                    client.request.call_count,
                    {"BUILDING": 1, "FAILED": 2}.get(state, 0),
                )

    def test_only_building_and_failed_headers_show_check_details(self):
        source = progress_task(fixture("altrepo-progress-437142"))
        for state in tasks.STATE_COLORS:
            with self.subTest(state=state):
                row = dict(
                    source,
                    state=state,
                    stage="Install check",
                    failed_check="ACL check",
                )
                args = arguments("tasks", "--all", "--no-color")
                header = tasks.render([row], args, 1, 120).split("#100")[0]
                if state == "BUILDING":
                    self.assertIn("Install check", header)
                    self.assertNotIn("ACL check", header)
                elif state == "FAILED":
                    self.assertIn("ACL check", header)
                    self.assertNotIn("ACL check FAILED", header)
                    self.assertNotIn("Install check", header)
                else:
                    self.assertNotIn("Install check", header)
                    self.assertNotIn("ACL check", header)

    def test_other_states_do_not_start_status_workers(self):
        source = progress_task(fixture("altrepo-progress-437142"))
        rows = [
            dict(source, state=state, id=index)
            for index, state in enumerate(tasks.STATE_COLORS, 1)
            if state not in task_status.STATES
        ]
        with patch.object(
            tasks,
            "start_worker",
            side_effect=AssertionError("unexpected status request"),
        ) as start:
            result, warnings = tasks.fetch_statuses(
                rows, arguments("tasks", "-m", source["owner"])
            )
        self.assertEqual(result, rows)
        self.assertEqual(warnings, [])
        start.assert_not_called()

    def test_recovered_acl_failure_is_not_a_later_build_failure(self):
        prefix = "2026-Oct-17 12:00:"
        for terminal in ("TESTED", "EPERM", "DONE"):
            for resume in (
                "",
                prefix
                + "03 :: test-only task #437142 for sisyphus resumed by majioa:\n",
            ):
                with self.subTest(terminal=terminal, resumed=bool(resume)):
                    log = (
                        prefix
                        + "00 :: acl check FAILED\n"
                        + prefix
                        + f"01 :: task #437142 for sisyphus {terminal}\n"
                        + resume
                        + prefix
                        + "04 :: [x86_64] #100 example: build FAILED\n"
                        + prefix
                        + "05 :: task #437142 for sisyphus FAILED\n"
                    )
                    self.assertIsNone(task_status.failed_check(log))
        log = (
            prefix
            + "00 :: acl check FAILED\n"
            + prefix
            + "01 :: task #437142 for sisyphus resumed by majioa:\n"
            + prefix
            + "02 :: ELF symbols check FAILED\n"
        )
        self.assertEqual(task_status.failed_check(log), "ELF symbols check")

    def test_invalid_and_mismatching_progress_is_reported(self):
        progress = fixture("altrepo-progress-437142")
        row = progress_task(progress)
        variants = ["{invalid", "[]"] + [
            json.dumps(dict(progress, **changed))
            for changed in (
                {"task_id": True},
                {"task_id": 123},
                {"task_owner": "another"},
                {"task_repo": "p11"},
                {"task_stage": None},
                {"task_state": None},
            )
        ]
        incomplete = dict(progress)
        incomplete.pop("task_stage")
        variants.append(json.dumps(incomplete))
        for body in variants:
            with self.subTest(body=body[:100]):
                client = Mock(timeout=15)
                client.request.return_value = body
                with self.assertRaises(CheckError):
                    task_status.load_status(client, row)

    def test_log_errors_preserve_stage_and_share_the_request_deadline(self):
        progress = fixture("altrepo-progress-437142")
        for message in (
            "HTTP 403",
            "HTTP 404",
            "HTTP 429",
            "HTTP 503",
            "network error: timeout",
        ):
            with self.subTest(message=message):
                client = Mock(timeout=15)
                client.request.side_effect = [json.dumps(progress), CheckError(message)]
                with patch.object(task_status.time, "monotonic", return_value=100):
                    result, warning = task_status.load_status(
                        client, progress_task(progress)
                    )
                self.assertEqual(client.deadline, 115)
                self.assertEqual(result["stage"], "Noarch check")
                self.assertIsNone(result["failed_check"])
                self.assertIn(message, warning)

    def test_headers_wrap_and_json_keeps_failures_without_ansi(self):
        row = dict(
            progress_task(fixture("altrepo-progress-437142")),
            test_only=True,
            stage="Dependencies check",
            failed_check="ELF symbols check",
        )
        for flags, width in (
            (["--all"], 24),
            (["--arch"], 72),
            (["--source"], 120),
            ([], 120),
        ):
            with self.subTest(flags=flags, width=width):
                args = arguments("tasks", "-m", row["owner"], *flags, "--no-color")
                text = tasks.render([row], args, 1, width)
                self.assertNotIn("\033", text)
                self.assertTrue(all(len(line) <= width for line in text.splitlines()))
                header = text.split("#100")[0]
                self.assertIn("ELFsymbolscheck", re.sub(r"\s", "", header))
                self.assertEqual(header.count("FAILED"), 1)
                self.assertIn("TEST", header)
                self.assertNotIn("Dependencies check", text)
        args = arguments("tasks", "--all", "--json", "--color", "always")
        with (
            patch.object(tasks, "snapshot", return_value=([row], 1, [])),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 0)
        value = json.loads(out.getvalue())[0]
        self.assertEqual(value["failed_check"], "ELF symbols check")
        self.assertIn("source", value["subtasks"]["100"])
        self.assertNotIn("\033", out.getvalue())
        self.assertEqual(err.getvalue(), "")
        text = tasks.render(
            [row], arguments("tasks", "--arch", "--color", "always"), 1, 120
        )
        self.assertIn("\033[1;31mELF symbols check\033[0m", text)
        self.assertIn("\033[32mx86_64", text)

    def test_neighbour_check_does_not_repeat_failed_state(self):
        row = dict(
            progress_task(fixture("altrepo-progress-437142")),
            stage="Neighbour version check",
            failed_check="Neighbour version check",
        )
        for width in (40, 120):
            with self.subTest(width=width):
                args = arguments("tasks", "--all", "--no-color")
                header = tasks.render([row], args, 1, width).split("#100")[0]
                self.assertEqual(header.count("FAILED"), 1)
                self.assertIn("Neighbourversioncheck", re.sub(r"\s", "", header))

    def test_snapshot_enriches_only_the_selected_tasks_by_default(self):
        row = progress_task(fixture("altrepo-progress-437142"))
        other = dict(row, id=row["id"] - 1)
        enriched = dict(row, stage="Noarch check", failed_check="Noarch check")
        args = arguments("tasks", "-m", row["owner"], "--limit", "1")
        with (
            patch.object(tasks, "request_json", return_value=[row, other]),
            patch.object(
                tasks, "fetch_statuses", return_value=([enriched], [])
            ) as fetch,
            patch.object(tasks, "fetch_details") as details,
        ):
            self.assertEqual(tasks.snapshot(args), ([enriched], 2, []))
        fetch.assert_called_once_with([row], args)
        details.assert_not_called()

    def test_partial_status_failures_preserve_rows_and_warn(self):
        row = progress_task(fixture("altrepo-progress-437142"))
        enriched = dict(row, stage="Noarch check", failed_check=None)
        worker = {"connection": Mock(), "process": Mock(), "started": 0}
        worker["connection"].poll.return_value = True
        args = arguments("tasks", "-m", row["owner"], "--json", "--no-color")
        for response in (
            (enriched, "check log unavailable: HTTP 404"),
            CheckError("HTTP 503 from rdb.altlinux.org"),
        ):
            with self.subTest(response=response):
                with (
                    patch.object(tasks, "start_worker", return_value=worker),
                    patch.object(tasks, "receive_worker", side_effect=[response]),
                    patch.object(tasks, "stop_workers"),
                ):
                    rows, warnings = tasks.fetch_statuses([row], args)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["subtasks"], row["subtasks"])
                self.assertEqual(len(warnings), 1)
                with (
                    patch.object(tasks, "snapshot", return_value=(rows, 1, warnings)),
                    streams() as (out, err),
                ):
                    self.assertEqual(tasks.main(args), 1)
                self.assertEqual(json.loads(out.getvalue()), rows)
                self.assertIn("warning:", err.getvalue())

    def test_status_completion_order_and_concurrency_limit(self):
        base = progress_task(fixture("altrepo-progress-437142"))
        rows = [dict(base, id=identifier) for identifier in range(1, 6)]
        schedule = Schedule(
            [4, 3, 2, 1, 5],
            {
                row["id"]: (dict(row, stage="Noarch check", failed_check=None), None)
                for row in rows
            },
        )
        with schedule.patch_workers(tasks):
            output, warnings = tasks.fetch_statuses(
                rows, arguments("tasks", "-m", base["owner"])
            )
        self.assertEqual([row["id"] for row in output], [1, 2, 3, 4, 5])
        self.assertEqual(warnings, [])
        self.assertEqual(schedule.peak, 4)
        self.assertEqual(schedule.active, set())
        self.assertEqual(len(schedule.closed), 5)

    def test_status_timeout_keeps_task_and_stops_worker(self):
        row = progress_task(fixture("altrepo-progress-437142"))
        worker = {"connection": Mock(), "process": Mock(), "started": 0}
        worker["connection"].poll.return_value = False
        with (
            patch.object(tasks, "start_worker", return_value=worker),
            patch.object(tasks.time, "monotonic", return_value=16),
            patch.object(tasks, "stop_workers") as stop,
        ):
            output, warnings = tasks.fetch_statuses(
                [row], arguments("tasks", "-m", row["owner"])
            )
        self.assertEqual(output, [row])
        self.assertIn("Task status request timed out", warnings[0])
        self.assertEqual(stop.call_args_list[0].args, ([worker],))
