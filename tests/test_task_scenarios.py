import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from repokeep import tasks
from repokeep.common import CheckError
from tests.support import arguments, fixture, frozen_time, streams


class TaskScenariosTest(unittest.TestCase):
    def test_saved_lists_cover_owners_branches_and_states(self):
        for owner, count, states in (
            ("cas", 75, {"EPERM", "FAILED", "DONE", "TESTED"}),
            ("lav", 127, {"TESTED", "POSTPONED", "NEW", "FAILED", "EPERM"}),
            ("rider", 37, {"EPERM", "FAILED", "TESTED", "NEW"}),
        ):
            with self.subTest(owner=owner):
                values = fixture("taskoteka-" + owner)
                self.assertEqual(len(values), count)
                self.assertEqual({t["state"] for t in values}, states)
                rows = tasks.select_tasks(values, arguments("task", "-m", owner))
                self.assertTrue(
                    all(t["owner"] == owner and t["state"] != "DONE" for t in rows)
                )
                self.assertEqual(
                    [t.get("build_time") or "" for t in rows],
                    sorted((t.get("build_time") or "" for t in rows), reverse=True),
                )
                selected = tasks.select_tasks(
                    values,
                    arguments(
                        "task", "-m", owner, "-b", "p11,sisyphus", "-s", "FAILED,EPERM"
                    ),
                )
                self.assertTrue(
                    all(
                        t["repo"] in ("p11", "sisyphus")
                        and t["state"] in ("FAILED", "EPERM")
                        for t in selected
                    )
                )
                self.assertTrue(selected)

    def test_real_details_numeric_order_and_deleted_placeholders(self):
        for owner, count, first, last in (
            ("cas", 26, ("100", "qt5-base"), ("6600", "hplip")),
            (
                "lav",
                31,
                ("40", "rpm-build-mediawiki"),
                ("4100", "mediawiki-extensions-WebStore"),
            ),
            ("rider", 53, ("100", "php"), ("6500", "unit")),
        ):
            with self.subTest(owner=owner):
                row = tasks.normalize(fixture("taskoteka-" + owner + "-detail"))
                entries = tasks.package_rows(row)
                self.assertEqual(len(entries), count)
                self.assertEqual(entries[0][:2], first)
                self.assertEqual(entries[-1][:2], last)
                self.assertEqual(
                    [int(n) for n, _, _ in entries],
                    sorted(int(n) for n, _, _ in entries),
                )
        row = tasks.normalize(fixture("taskoteka-lav-detail"))
        self.assertEqual(row["subtasks"]["4100"]["type"], "delete")

    def test_failed_task_does_not_override_successful_architectures(self):
        row = tasks.normalize(fixture("taskoteka-rider-detail"))
        self.assertEqual(row["state"], "FAILED")
        for arch in tasks.ARCHITECTURES:
            self.assertIs(
                tasks.arch_status(row["subtasks"]["100"], arch),
                tasks.ArchStatus.SUCCESS,
            )
        output = tasks.render(
            [row],
            arguments("task", "-m", "rider", "--arch", "--color", "always"),
            1,
            120,
        )
        self.assertIn("\033[1;31m  [FAILED]", output)
        first = output.split("#100", 1)[1].splitlines()[0]
        for arch in tasks.ARCHITECTURES:
            self.assertIn("\033[32m" + arch, first)

    def test_architecture_matrix_never_promotes_unknown_results(self):
        self.assertIs(tasks.arch_status(None, "x86_64"), tasks.ArchStatus.MISSING)
        expected = {
            "built": tasks.ArchStatus.SUCCESS,
            "cached": tasks.ArchStatus.SUCCESS,
            "failed": tasks.ArchStatus.FAILED,
            "excluded": tasks.ArchStatus.EXCLUDED,
            "swept": tasks.ArchStatus.SKIPPED,
            "pending": tasks.ArchStatus.WAITING,
            "waiting": tasks.ArchStatus.WAITING,
            "awaiting": tasks.ArchStatus.WAITING,
            "new": tasks.ArchStatus.WAITING,
            "building": tasks.ArchStatus.WAITING,
            "": tasks.ArchStatus.MISSING,
            None: tasks.ArchStatus.MISSING,
            "future-state": tasks.ArchStatus.UNKNOWN,
        }
        for kind in ("repo", "srpm", "copy", "delete"):
            for state, outcome in expected.items():
                with self.subTest(kind=kind, state=state):
                    if state in (None, "") and kind in ("copy", "delete"):
                        outcome = tasks.ArchStatus.SKIPPED
                    subtask = {"type": kind, "build": {"x86_64": state}}
                    self.assertIs(tasks.arch_status(subtask, "x86_64"), outcome)
        for build in (None, {}, [], "built"):
            self.assertIs(
                tasks.arch_status({"type": "repo", "build": build}, "x86_64"),
                tasks.ArchStatus.MISSING,
            )

    def test_missing_name_fallbacks_and_owner_filter_keep_all_packages(self):
        value = {
            "id": 10,
            "owner": "cas",
            "repo": "p11",
            "state": "NEW",
            "test_only": True,
            "subtasks": {
                "1000": {"type": "repo", "userid": "rider"},
                "20": {
                    "type": "srpm",
                    "srpm": "/incoming/a-long-name-1.2-alt3.src.rpm",
                    "userid": "lav",
                },
                "100": {
                    "type": "repo",
                    "dir": "/gears/r/repository.git/",
                    "userid": "lav",
                },
                "40": {"type": "copy", "pkgname": "copied", "copy_repo": "sisyphus"},
                "50": {"userid": "cas"},
                "60": {},
            },
        }
        self.assertEqual(
            [entry[:2] for entry in tasks.package_rows(value)],
            [
                ("20", "a-long-name"),
                ("40", "copied"),
                ("100", "repository"),
                ("1000", "(unknown package)"),
            ],
        )
        args = arguments("task", "-m", "cas", "-p", "a-long-*")
        rows = tasks.select_tasks([value], args)
        self.assertEqual(len(rows), 1)
        output = tasks.render(rows, args, 1, 120)
        for text in ("#20", "#40", "#100", "#1000", "copied", "repository", "TEST"):
            self.assertIn(text, output)
        self.assertEqual(
            tasks.select_tasks([value], arguments("task", "-m", "lav")), []
        )

    def test_calendar_boundaries_in_fixed_timezones(self):
        instant = datetime(2027, 1, 1, 0, 30, tzinfo=timezone.utc)
        values = [
            {"id": n, "owner": "cas", "repo": "p11", "state": "NEW", "created": created}
            for n, created in (
                (1, "2026-12-30T23:59:59"),
                (2, "2026-12-31T00:00:00"),
                (3, "2027-01-01T00:00:00"),
                (4, None),
            )
        ]
        for tz, since, identifiers in (
            ("UTC", "2026-12-31", [3, 2]),
            ("EST5", "2026-12-30", [3, 2, 1]),
        ):
            with self.subTest(tz=tz), frozen_time(instant, tz):
                args = arguments("task", "-m", "cas", "--days", "2")
                self.assertEqual(args.since, since)
                self.assertEqual(
                    [t["id"] for t in tasks.select_tasks(values, args)], identifiers
                )

    def test_every_state_filter_and_all_sort_modes(self):
        rows = [
            {
                "id": index,
                "owner": "lav",
                "repo": "p11" if index % 2 else "sisyphus",
                "state": state,
                "subtasks": {"100": {"pkgname": f"p{index:02}"}},
            }
            for index, state in enumerate(tasks.STATE_COLORS, 1)
        ]
        for row in rows:
            self.assertEqual(
                tasks.select_tasks(
                    rows, arguments("task", "-m", "lav", "-s", row["state"])
                ),
                [row],
            )
        for field in ("time", "id", "package", "state", "branch"):
            args = arguments("task", "-m", "lav", "--sort", field)
            forward = tasks.select_tasks(rows, args)
            reverse = tasks.select_tasks(
                rows, arguments("task", "-m", "lav", "--sort", field, "--reverse")
            )
            self.assertEqual(reverse, list(reversed(forward)))

    def test_missing_fields_and_partial_snapshots_have_error_exit_status(self):
        good = tasks.normalize(fixture("taskoteka-rider-detail"))
        for field in ("owner", "repo", "state"):
            malformed = dict(good)
            malformed.pop(field)
            with self.assertRaisesRegex(CheckError, "missing owner/state/repo"):
                tasks.normalize(malformed)
        args = arguments("task", "-m", "rider", "--arch", "--json", "--no-color")
        with (
            patch.object(
                tasks,
                "snapshot",
                return_value=([good], 2, ["warning: second task HTTP 503"]),
            ),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 1)
        self.assertEqual(json.loads(out.getvalue()), [good])
        self.assertIn("second task HTTP 503", err.getvalue())
        self.assertNotIn("\033", out.getvalue() + err.getvalue())
