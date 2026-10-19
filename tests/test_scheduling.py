import json
import unittest
from unittest.mock import Mock, patch

from repokeep import tasks, updates
from repokeep.common import CheckError
from repokeep.models import Result, vulnerability_result
from tests.support import Schedule, arguments, streams


class SchedulingTest(unittest.TestCase):
    def test_all_completion_orders_preserve_case_insensitive_sorted_output(self):
        names = ["alpha", "Beta", "zeta"]
        values = [{"name": name, "version": "1.0"} for name in names]
        orders = [
            ["alpha", "Beta", "zeta"],
            ["alpha", "zeta", "Beta"],
            ["Beta", "alpha", "zeta"],
            ["Beta", "zeta", "alpha"],
        ]
        for order in orders:
            for sorted_output in (False, True):
                with self.subTest(order=order, sorted=sorted_output):
                    schedule = Schedule(
                        order,
                        {
                            name: Result(name, "1.0", "alt1", status="OK")
                            for name in names
                        },
                    )
                    args = arguments(
                        "update",
                        "-j",
                        "2",
                        "--no-progress",
                        *(["--sort"] if sorted_output else []),
                    )
                    with schedule.patch_workers(updates):
                        results = list(updates.package_results(values, {}, args))
                    self.assertEqual(
                        [r.name for r in results],
                        names if sorted_output else list(order),
                    )
                    self.assertEqual(schedule.peak, 2)
                    self.assertEqual(set(schedule.closed), set(names))
                    self.assertEqual(schedule.active, set())

    def test_worker_limit_is_respected_and_pending_items_are_not_lost(self):
        names = ["alpha", "Beta", "charlie", "Zeta"]
        order = ["Beta", "alpha", "Zeta", "charlie"]
        schedule = Schedule(
            order, {name: Result(name, "1.0", "", status="OK") for name in names}
        )
        with schedule.patch_workers(updates):
            results = list(
                updates.package_results(
                    [{"name": name, "version": "1.0"} for name in names],
                    {},
                    arguments("update", "-j", "2", "--sort", "--no-progress"),
                )
            )
        self.assertEqual([r.name for r in results], names)
        self.assertEqual(schedule.peak, 2)
        self.assertEqual(schedule.started, names)
        self.assertEqual(len(schedule.closed), 4)

    def test_partial_task_details_keep_good_results_and_warn_on_stderr(self):
        values = [
            {
                "id": identifier,
                "owner": "lav",
                "repo": "p11",
                "state": "FAILED",
                "subtasks": {
                    "100": {"pkgname": "pkg" + str(identifier), "type": "repo"}
                },
            }
            for identifier in (1, 2)
        ]
        detail = dict(
            values[0],
            subtasks={
                "100": dict(values[0]["subtasks"]["100"], build={"x86_64": "cached"})
            },
        )
        schedule = Schedule(
            [2, 1], {1: detail, 2: CheckError("HTTP 429 from Taskoteka")}
        )
        args = arguments("task", "-m", "lav", "--arch", "--json", "--no-color")
        with (
            patch.object(tasks, "request_json", return_value=values),
            patch.object(
                tasks, "fetch_statuses", side_effect=lambda rows, args: (rows, [])
            ),
            schedule.patch_workers(tasks),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 1)
        rows = json.loads(out.getvalue())
        self.assertEqual([r["id"] for r in rows], [2, 1])
        self.assertIs(
            tasks.arch_status(rows[0]["subtasks"]["100"], "x86_64"),
            tasks.ArchStatus.MISSING,
        )
        self.assertIs(
            tasks.arch_status(rows[1]["subtasks"]["100"], "x86_64"),
            tasks.ArchStatus.SUCCESS,
        )
        self.assertIn("#2", err.getvalue())
        self.assertIn("HTTP 429", err.getvalue())
        self.assertEqual(schedule.active, set())

    def test_package_timeout_keeps_partial_osv_matches(self):
        package = {"name": "pkg", "version": "1.2", "release": "alt1"}
        partial = vulnerability_result(package)
        partial.vulnerabilities = [
            {"id": "CVE-fixture", "aliases": ["CVE-fixture"], "fixed_versions": []}
        ]
        worker = {"started": 0, "connection": Mock(poll=lambda: True)}
        worker["connection"].recv.return_value = ("progress", partial)
        with (
            patch.object(updates, "start_worker", return_value=worker),
            patch.object(updates, "stop_workers") as stop,
            patch.object(updates.time, "monotonic", return_value=16),
        ):
            result = list(
                updates.package_results(
                    [package],
                    {},
                    arguments("update", "--vulnerable-only", "--no-progress"),
                )
            )[0]
        self.assertEqual(result.status, "ERR")
        self.assertEqual(result.vulnerabilities[0]["id"], "CVE-fixture")
        self.assertIn("timed out", result.note)
        stop.assert_any_call([worker])

    def test_task_detail_timeout_does_not_invent_build_success(self):
        value = {
            "id": 1,
            "owner": "cas",
            "repo": "p11",
            "state": "NEW",
            "subtasks": {"100": {"type": "repo", "pkgname": "pkg"}},
        }
        worker = {"started": 0, "connection": Mock(poll=lambda: False)}
        with (
            patch.object(tasks, "start_worker", return_value=worker),
            patch.object(tasks, "stop_workers") as stop,
            patch.object(tasks.time, "monotonic", return_value=16),
        ):
            rows, warnings = tasks.fetch_details(
                [value], arguments("task", "-m", "cas", "--arch")
            )
        self.assertEqual(rows, [value])
        self.assertIn("timed out", warnings[0])
        self.assertIs(
            tasks.arch_status(rows[0]["subtasks"]["100"], "x86_64"),
            tasks.ArchStatus.MISSING,
        )
        stop.assert_any_call([worker])
