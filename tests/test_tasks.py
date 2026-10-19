import io
import json
import os
import re
import unittest
from datetime import timedelta
from unittest.mock import patch

from repokeep import cli, tasks
from repokeep.common import CheckError
from tests.support import (
    FIXED_DATE,
    arguments,
    fixture,
    frozen_time,
    plain_text,
    streams,
)


def task(identifier=1, **values):
    result = {
        "id": identifier,
        "owner": "scala",
        "repo": "sisyphus",
        "state": "FAILED",
        "created": FIXED_DATE.isoformat() + "T00:00:00",
        "subtasks": {"100": {"type": "repo", "pkgname": "example", "userid": "other"}},
    }
    result.update(values)
    return result


class TasksTest(unittest.TestCase):
    def setUp(self):
        clock = frozen_time()
        clock.__enter__()
        self.addCleanup(clock.__exit__, None, None, None)

    def test_git_source_matches_real_task_and_clone_url(self):
        row = tasks.normalize(fixture("taskoteka-source-repo"))
        subtask = row["subtasks"]["100"]
        self.assertEqual(subtask["epoch"], "2")
        self.assertEqual(
            tasks.source_info(row, "100", subtask),
            {
                "label": "python3-module-pivy.git=0.6.11-alt2",
                "url": fixture("taskoteka-source-git")["clone_url"],
            },
        )
        self.assertEqual(
            tasks.source_info(row, "100", dict(subtask, tag_name=None))["label"],
            "python3-module-pivy.git",
        )

    def test_srpm_copy_delete_and_unavailable_sources(self):
        row = tasks.normalize(fixture("taskoteka-source-srpm"))
        source = tasks.source_info(row, "100", row["subtasks"]["100"])
        filename = "perl-Tie-Filehandle-Preempt-Stdin-0.03-alt1.1.src.rpm"
        self.assertEqual(source["label"], filename)
        self.assertEqual(
            source["url"], "https://git.altlinux.org/tasks/437328/gears/100/" + filename
        )
        row = tasks.normalize(fixture("taskoteka-source-delete"))
        self.assertEqual(
            tasks.source_info(row, "100", row["subtasks"]["100"]),
            {"label": "not applicable (delete)", "url": None},
        )
        for subtask, label in [
            ({"type": "copy", "copy_repo": "p11"}, "copy from p11"),
            ({"type": "copy"}, "copy (source branch unavailable)"),
            ({"type": "repo", "pkgname": "example"}, "not available"),
            ({"type": "srpm"}, "not available"),
            (None, "not available"),
        ]:
            self.assertEqual(
                tasks.source_info(row, "100", subtask), {"label": label, "url": None}
            )

    def test_source_urls_escape_srpm_filenames_and_subtask_numbers(self):
        source = tasks.source_info(
            task(), "100/200", {"type": "srpm", "srpm": "/incoming/name+1#2.src.rpm"}
        )
        self.assertEqual(source["label"], "name+1#2.src.rpm")
        self.assertEqual(
            source["url"],
            "https://git.altlinux.org/tasks/1/gears/100%2F200/name%2B1%232.src.rpm",
        )

    def test_source_flag_fetches_details_once_with_or_without_arch(self):
        for options in (
            [],
            ["--source"],
            ["--arch"],
            ["--arch", "--source"],
            ["--all"],
        ):
            with self.subTest(options=options):
                args = arguments("task", *options)
                with (
                    patch.object(tasks, "request_json", return_value=[task()]),
                    patch.object(
                        tasks,
                        "fetch_statuses",
                        side_effect=lambda rows, args: (rows, []),
                    ),
                    patch.object(
                        tasks, "fetch_details", return_value=([task()], [])
                    ) as fetch,
                ):
                    rows, total, warnings = tasks.snapshot(args)
                self.assertEqual((rows, total, warnings), ([task()], 1, []))
                self.assertEqual(fetch.call_count, int(bool(options)))

    def test_all_matches_explicit_flags_without_changing_selection(self):
        row = task(
            subtasks={
                "100": {
                    "type": "repo",
                    "pkgname": "example",
                    "dir": "/gears/e/example.git",
                    "tag_name": "1.2-alt1",
                    "build": {"x86_64": "built", "i586": "cached"},
                    "approved_by": ["cas", "nbr"],
                }
            }
        )
        for output_options in (["--no-color"], ["--json", "--color", "always"]):
            outputs = []
            for flags in (
                ["--arch", "--source", "--approvals"],
                ["--all"],
                ["--source", "--all", "--arch"],
            ):
                with (
                    self.subTest(output=output_options, flags=flags),
                    patch.object(
                        tasks, "request_json", return_value=[row, task(2, state="DONE")]
                    ),
                    patch.object(
                        tasks,
                        "fetch_statuses",
                        side_effect=lambda rows, args: (rows, []),
                    ),
                    patch.object(
                        tasks, "fetch_details", return_value=([row], [])
                    ) as fetch,
                    streams() as (out, err),
                ):
                    self.assertEqual(
                        cli.run(["task", "-m", "scala", *flags, *output_options]), 0
                    )
                    fetch.assert_called_once()
                    selected, args = fetch.call_args.args
                    self.assertEqual(selected, [row])
                    self.assertTrue(args.arch)
                    self.assertTrue(args.source)
                    self.assertTrue(args.approvals)
                    self.assertEqual(err.getvalue(), "")
                    self.assertNotIn("\033", out.getvalue())
                    outputs.append(out.getvalue())
            self.assertEqual(outputs, [outputs[0]] * len(outputs))
            if "--json" in output_options:
                subtask = json.loads(outputs[0])[0]["subtasks"]["100"]
                self.assertEqual(subtask["build"]["x86_64"], "built")
                self.assertEqual(subtask["pkgname"], "example")
                self.assertEqual(subtask["approved_by"], ["cas", "nbr"])
                self.assertEqual(subtask["source"]["label"], "example.git=1.2-alt1")
            else:
                self.assertIn("example", outputs[0])
                self.assertIn("[example.git=1.2-alt1]", outputs[0])
                self.assertIn("x86_64  i586  aarch64", outputs[0])
                self.assertIn("approved: cas, nbr", outputs[0])

    def test_source_keeps_package_names_and_original_filtering(self):
        row = task(
            subtasks={
                "100": {
                    "type": "repo",
                    "pkgname": "built-name",
                    "dir": "/gears/g/gear-name.git",
                    "tag_name": "1.2-alt3",
                },
                "200": {"type": "copy", "pkgname": "copied", "copy_repo": "p11"},
                "300": {"type": "delete", "pkgname": "removed"},
                "400": {"userid": "scala"},
                "500": {"type": "repo", "pkgname": "pending"},
            }
        )
        original = json.dumps(row, sort_keys=True)
        args = arguments("task", "--source", "-p", "built-*")
        self.assertEqual(tasks.select_tasks([row], args), [row])
        self.assertEqual(
            [name for _, name, _ in tasks.package_rows(row, source=True)],
            [
                "built-name [gear-name.git=1.2-alt3]",
                "copied [copy from p11]",
                "removed [not applicable (delete)]",
                "pending [not available]",
            ],
        )
        self.assertEqual(
            tasks.select_tasks([row], arguments("task", "--source", "-p", "gear-*")),
            [],
        )
        self.assertEqual(json.dumps(row, sort_keys=True), original)

    def test_source_rows_wrap_without_losing_names_or_links(self):
        rows = [
            tasks.normalize(fixture("taskoteka-source-" + kind))
            for kind in ("repo", "srpm", "delete")
        ] + [task(subtasks={})]
        url = fixture("taskoteka-source-git")["clone_url"]
        source_label = "python3-module-pivy.git=0.6.11-alt2"
        for width, options in (
            (1, ["--all"]),
            (8, ["--source"]),
            (40, ["--all"]),
            (72, ["--all"]),
            (160, ["--source"]),
            (160, ["--all"]),
        ):
            with self.subTest(width=width, options=options):
                plain = tasks.render(
                    rows, arguments("task", *options, "--no-color"), 4, width
                )
                colored = tasks.render(
                    rows, arguments("task", *options, "--color", "always"), 4, width
                )
                self.assertEqual(plain_text(colored), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()))
                self.assertNotIn("Source:", plain)
                self.assertNotIn("https://", plain)
                normalized = "".join(plain.replace("│", "").split())
                expected_architectures = 4 if "--all" in options else 0
                self.assertEqual(normalized.count("x86_64"), expected_architectures)
                names = normalized.replace("x86_64i586aarch64", "")
                for value in (
                    "python3-module-pivy",
                    source_label,
                    "perl-Tie-Filehandle-Preempt-Stdin-0.03-alt1.1.src.rpm",
                    "woadaptor[notapplicable(delete)]",
                    "(nopackages)",
                ):
                    self.assertIn(value, names)
                fragments = re.findall(
                    re.escape("\033]8;;" + url + "\033\\")
                    + "(.*?)"
                    + re.escape("\033]8;;\033\\"),
                    colored,
                )
                self.assertEqual("".join("".join(fragments).split()), source_label)
                before_link = plain_text(colored.split("\033]8;;", 1)[0])
                self.assertIn("python3-module-pivy", "".join(before_link.split()))
                if width == 160:
                    package_line = next(
                        line
                        for line in plain.splitlines()
                        if "python3-module-pivy" in line
                    )
                    self.assertIn(
                        "python3-module-pivy [" + source_label + "]", package_line
                    )
                    self.assertEqual(
                        "x86_64" in package_line, bool(expected_architectures)
                    )
        output = tasks.render(rows, arguments("task", "--no-color"), 4, 160)
        self.assertNotIn("Source:", output)
        self.assertNotIn("https://git.altlinux.org/tasks/", output)

    def test_package_names_are_not_bold_and_only_sources_are_linked(self):
        row = tasks.normalize(fixture("taskoteka-source-repo"))
        source_label = "python3-module-pivy.git=0.6.11-alt2"
        url = fixture("taskoteka-source-git")["clone_url"]
        for options, width in (
            ([], 40),
            (["--arch"], 120),
            (["--source"], 40),
            (["--all"], 120),
        ):
            with self.subTest(options=options, width=width):
                output = tasks.render(
                    [row], arguments("task", *options, "--color", "always"), 1, width
                )
                body = output.split("#100", 1)[1].split("\n\n", 1)[0]
                self.assertNotIn("\033[1m", body)
                linked = re.findall(
                    re.escape("\033]8;;" + url + "\033\\")
                    + "(.*?)"
                    + re.escape("\033]8;;\033\\"),
                    body,
                )
                show_source = "--source" in options or "--all" in options
                self.assertEqual(
                    "".join("".join(linked).split()),
                    source_label if show_source else "",
                )
                self.assertNotIn("[", "".join(linked))
                self.assertNotIn("]", "".join(linked))

    def test_sources_share_package_rows_with_architectures(self):
        row = tasks.normalize(fixture("taskoteka-detail"))
        for width in (72, 160):
            with self.subTest(width=width):
                output = tasks.render(
                    [row],
                    arguments("task", "--arch", "--source", "--color", "always"),
                    1,
                    width,
                )
                plain = re.sub(r"\033(?:\[[0-9;]*m|\]8;;[^\033]*\033\\)", "", output)
                lines = [
                    line for line in plain.splitlines() if re.search(r"#\d+", line)
                ]
                self.assertEqual(len(lines), 6)
                self.assertEqual(len({line.index("x86_64") for line in lines}), 1)
                for _, name, subtask in tasks.package_rows(row, True):
                    opening = "\033]8;;" + subtask["source"]["url"] + "\033\\"
                    fragments = re.findall(
                        re.escape(opening) + "(.*?)" + re.escape("\033]8;;\033\\"),
                        output,
                    )
                    self.assertEqual("".join(fragments), subtask["source"]["label"])
                self.assertNotIn("Source:", plain)
                self.assertNotIn("https://", plain)

    def test_wrapped_source_hyperlinks_close_before_architectures(self):
        row = tasks.normalize(fixture("taskoteka-detail"))
        output = tasks.render(
            [row], arguments("task", "--arch", "--source", "--color", "always"), 1, 72
        )
        for line in output.splitlines():
            targets = re.findall(r"\033\]8;;([^\033]*)\033\\", line)
            self.assertEqual(sum(bool(url) for url in targets), targets.count(""))
            if "x86_64" in line:
                self.assertLess(line.rfind("\033]8;;\033\\"), line.index("x86_64"))
        deleted = tasks.normalize(fixture("taskoteka-source-delete"))
        plain = tasks.render(
            [deleted], arguments("task", "--source", "--no-color"), 1, 72
        )
        self.assertIn("woadaptor", plain)
        self.assertIn("woadaptor [not applicable (delete)]", plain)

    def test_source_json_preserves_raw_subtasks_and_deleted_placeholders(self):
        row = tasks.normalize(fixture("taskoteka-detail"))
        original = json.dumps(row, sort_keys=True)
        args = arguments("task", "--source", "--json", "--color", "always")
        with (
            patch.object(tasks, "snapshot", return_value=([row], 1, [])),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 0)
        value = json.loads(out.getvalue())[0]
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("\033", out.getvalue())
        self.assertEqual(value["subtasks"]["100"], row["subtasks"]["100"])
        for number, _, subtask in tasks.package_rows(row):
            actual = dict(value["subtasks"][number])
            self.assertEqual(
                actual.pop("source"), tasks.source_info(row, number, subtask)
            )
            self.assertEqual(actual, subtask)
        self.assertEqual(json.dumps(row, sort_keys=True), original)

    def test_layout_follows_terminal_width_on_each_render(self):
        rows = [task(435995, test_only=True)]
        args = arguments("task", "--arch", "--no-color")
        with patch.object(
            tasks.shutil,
            "get_terminal_size",
            side_effect=[os.terminal_size((40, 24)), os.terminal_size((160, 24))],
        ):
            compact = tasks.render(rows, args, 1)
            wide = tasks.render(rows, args, 1)
        self.assertIn("[1/1]  435995  [FAILED]  sisyphus  TEST", compact)
        self.assertIn("\n  └─ #100  example\n           x86_64", compact)
        self.assertNotIn("SUBTASK", compact)
        self.assertNotIn("SUBTASK", wide)
        self.assertIn("  └─ #100  example       │ x86_64  i586  aarch64", wide)

    def test_same_layout_with_and_without_source_or_arch(self):
        row = tasks.normalize(fixture("taskoteka-detail"))
        for width in (72, 160):
            for arch in (False, True):
                for source in (False, True):
                    with self.subTest(width=width, arch=arch, source=source):
                        options = (["--arch"] if arch else []) + (
                            ["--source"] if source else []
                        )
                        output = tasks.render(
                            [row], arguments("task", *options, "--no-color"), 1, width
                        )
                        lines = [
                            line
                            for line in output.splitlines()
                            if re.search(r"#\d+", line)
                        ]
                        self.assertEqual(len(lines), 6)
                        self.assertTrue(lines[0].startswith("  ├─  #40  "))
                        self.assertTrue(lines[-1].startswith("  └─ #460  "))
                        self.assertNotIn("SUBTASK", output)
                        self.assertNotIn("Source:", output)
                        self.assertEqual(output.count("x86_64"), 6 if arch else 0)
                        if arch:
                            self.assertEqual(
                                len({line.index("x86_64") for line in lines}), 1
                            )

    def test_all_output_fits_narrow_and_wide_terminals(self):
        rows = [
            tasks.normalize(fixture("taskoteka-detail")),
            task(1, subtasks={}, test_only=True, state="POSTPONED"),
            task(2, state="AWAITING", repo="a-very-long-branch-name"),
        ]
        for width in (1, 8, 40, 80, 160):
            for arch in (False, True):
                with self.subTest(width=width, arch=arch):
                    options = ["--watch", "5"] + (["--arch"] if arch else [])
                    plain = tasks.render(
                        rows, arguments("task", *options, "--no-color"), 123, width
                    )
                    colored = tasks.render(
                        rows,
                        arguments("task", *options, "--color", "always"),
                        123,
                        width,
                    )
                    self.assertEqual(re.sub(r"\033\[[0-9;]*m", "", colored), plain)
                    self.assertTrue(
                        all(len(line) <= width for line in plain.splitlines())
                    )

    def test_compact_layout_keeps_long_names_subtasks_and_architecture_colors(self):
        name = "python3-module-" + "very-long-package-name-" * 8 + "end"
        rows = [
            task(
                subtasks={
                    "100": {
                        "type": "repo",
                        "pkgname": name,
                        "build": {"x86_64": "cached", "i586": "failed"},
                    },
                    "200": {"type": "copy", "pkgname": "copied"},
                    "300": {"type": "delete", "pkgname": "deleted"},
                    "400": {},
                }
            )
        ]
        output = tasks.render(
            rows, arguments("task", "--arch", "--color", "always"), 1, 40
        )
        plain = re.sub(r"\033\[[0-9;]*m", "", output)
        package_lines = plain.split("#100", 1)[1].split("x86_64", 1)[0]
        self.assertEqual("".join(package_lines.replace("│", "").split()), name)
        self.assertIn("#200  copied", plain)
        self.assertIn("#300  deleted", plain)
        self.assertNotIn("#400", plain)
        for code, architecture in [
            ("32", "x86_64"),
            ("1;31", "i586"),
            ("90", "aarch64"),
        ]:
            self.assertIn("\033[" + code + "m" + architecture, output)
        self.assertEqual(plain.count("x86_64"), 3)

    def test_compact_columns_align_names_numbers_and_architectures(self):
        rows = [
            task(1, subtasks={"100": {"pkgname": "python3-module-example"}}),
            tasks.normalize(fixture("taskoteka-detail")),
        ]
        output = tasks.render(rows, arguments("task", "--arch", "--no-color"), 2, 72)
        package_lines = [line for line in output.splitlines() if "python3-" in line]
        self.assertEqual(len(package_lines), 7)
        self.assertEqual(len({line.index("python3-") for line in package_lines}), 1)
        self.assertEqual(len({line.index("x86_64") for line in package_lines}), 1)
        self.assertIn("  ├─  #40", output)
        self.assertIn("  └─ #460", output)

    def test_stacked_subtasks_have_separate_groups(self):
        row = tasks.normalize(fixture("taskoteka-detail"))
        output = tasks.render([row], arguments("task", "--arch", "--no-color"), 1, 40)
        self.assertIn("  ├─  #40  python3-module-owlrl\n  │        x86_64", output)
        self.assertIn("\n  │\n  ├─ #120", output)
        self.assertIn("  └─ #460  python3-module-spdx-tools\n           x86_64", output)

    def test_long_package_does_not_stack_other_tasks(self):
        name = "python3-module-" + "x" * 120
        rows = [
            task(1, subtasks={"100": {"pkgname": name}}),
            task(2),
        ]
        output = tasks.render(rows, arguments("task", "--arch", "--no-color"), 2, 72)
        package_line = next(line for line in output.splitlines() if "example" in line)
        self.assertIn("x86_64  i586  aarch64", package_line)
        first_task = output.split("[2/2]", 1)[0]
        package_lines = first_task.split("#100", 1)[1].strip().splitlines()
        fragments = [line.split("│", 1)[0].strip() for line in package_lines]
        self.assertEqual("".join(fragments), name)
        self.assertIn("x86_64", package_lines[0])

    def test_compact_layout_supports_ascii_output_encoding(self):
        args = arguments("task", "--arch", "--no-color")
        with io.TextIOWrapper(io.BytesIO(), encoding="ascii") as stream:
            with patch.object(tasks.sys, "stdout", stream):
                output = tasks.render(
                    [tasks.normalize(fixture("taskoteka-detail"))], args, 1, 72
                )
                stream.write(output)
                stream.flush()
        self.assertIn("  |-  #40", output)
        self.assertIn("  `- #460", output)

    def test_compact_layout_uses_available_width_for_long_names(self):
        name = "python3-module-" + "x" * 80
        output = tasks.render(
            [task(subtasks={"100": {"pkgname": name}})],
            arguments("task", "--arch", "--no-color"),
            1,
            110,
        )
        self.assertNotIn("SUBTASK", output)
        package_lines = output.split("#100", 1)[1].split("\n\n", 1)[0].splitlines()
        fragments = [line.split("│", 1)[0].strip() for line in package_lines]
        self.assertEqual("".join(fragments), name)
        row = next(line for line in output.splitlines() if "#100" in line)
        self.assertEqual(len(row), 110)
        self.assertIn("x86_64", row)

    def test_narrow_empty_output_and_no_color(self):
        args = arguments("task", "--arch")
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch.object(tasks.sys.stdout, "isatty", return_value=True),
        ):
            output = tasks.render([task()], args, 1, 30)
        self.assertNotIn("\033", output)
        empty = tasks.render([], args, 0, 20)
        self.assertIn("No tasks match the\nselected filters.", empty)
        self.assertIn("Summary: 0 tasks", empty)
        self.assertTrue(all(len(line) <= 20 for line in empty.splitlines()))

    def test_json_does_not_depend_on_terminal_width(self):
        rows = [tasks.normalize(fixture("taskoteka-detail"))]
        args = arguments("task", "--json", "--arch")
        with (
            patch.object(tasks, "snapshot", return_value=(rows, 1, [])),
            patch.object(tasks.shutil, "get_terminal_size", side_effect=AssertionError),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 0)
        self.assertEqual(json.loads(out.getvalue()), rows)
        self.assertEqual(err.getvalue(), "")

    def test_saved_responses_and_deleted_placeholders(self):
        args = arguments("task")
        rows = tasks.select_tasks(fixture("taskoteka-list"), args)
        self.assertEqual(
            [row["id"] for row in rows],
            [432157, 435564, 435355, 435352, 435335, 435995, 435919, 432607, 413835],
        )
        self.assertTrue(all(t["state"] != "DONE" for t in rows))
        detail = tasks.normalize(fixture("taskoteka-detail"))
        self.assertEqual(detail["id"], 435564)
        self.assertEqual(
            [r[0] for r in tasks.package_rows(detail)],
            ["40", "120", "240", "340", "410", "460"],
        )
        self.assertEqual(
            tasks.arch_status(detail["subtasks"]["40"], "x86_64"),
            tasks.ArchStatus.SUCCESS,
        )
        self.assertEqual(
            tasks.normalize(dict(detail, id=None, taskid=435564))["id"], 435564
        )

    def test_owner_is_not_subtask_author(self):
        values = [
            task(1),
            task(
                2,
                owner="someone",
                subtasks={"100": {"userid": "scala", "pkgname": "example"}},
            ),
        ]
        self.assertEqual(
            [t["id"] for t in tasks.select_tasks(values, arguments("task"))], [1]
        )

    def test_task_filter_retains_all_packages(self):
        value = task(
            subtasks={
                "200": {"type": "copy", "pkgname": "unrelated-long-package-name"},
                "100": {"type": "repo", "pkgname": "Python3-Module-Example"},
            }
        )
        args = arguments("task", "-p", "python3-*")
        rows = tasks.select_tasks([value], args)
        output = tasks.render(rows, args, 1)
        self.assertIn("unrelated-long-package-name", output)
        self.assertIn("#100", output)
        self.assertIn("#200", output)
        self.assertTrue(tasks.package_matches("Python-Example", "EXAMPLE"))
        self.assertFalse(tasks.package_matches("abc", "[ab]*"))
        self.assertTrue(tasks.package_matches("[ab]c", "[ab]*"))

    def test_source_names_build_copy_delete_and_empty(self):
        rows = tasks.package_rows(
            task(
                subtasks={
                    "300": {"type": "delete", "pkgname": "removed"},
                    "100": {
                        "type": "repo",
                        "dir": "/people/nick/packages/really-long-name.git/",
                    },
                    "200": {
                        "type": "srpm",
                        "srpm": "/incoming/long-package-1.2-alt3.src.rpm",
                    },
                    "400": {"type": "copy", "pkgname": "copied"},
                    "500": {"userid": "scala"},
                    "600": {},
                }
            )
        )
        self.assertEqual(
            [r[:2] for r in rows],
            [
                ("100", "really-long-name"),
                ("200", "long-package"),
                ("300", "removed"),
                ("400", "copied"),
            ],
        )
        args = arguments("task", "--arch", "--no-color")
        output = tasks.render([task(subtasks={})], args, 1)
        self.assertIn("(no packages)", output)
        self.assertIn("x86_64", output)
        self.assertNotIn("\033", output)

    def test_architecture_colors(self):
        value = task(
            subtasks={
                "100": {
                    "pkgname": "example",
                    "build": {
                        "x86_64": "built",
                        "i586": "failed",
                        "aarch64": "excluded",
                    },
                }
            }
        )
        output = tasks.render(
            [value], arguments("task", "--arch", "--color", "always"), 1
        )
        for code, name in [("32", "x86_64"), ("1;31", "i586"), ("9;90", "aarch64")]:
            self.assertIn("\033[" + code + "m" + name, output)
        self.assertNotIn(" OK", output)
        self.assertNotIn(" FAIL", output)

    def test_defaults_filters_dates_and_sorting(self):
        values = [
            task(1, repo="p11"),
            task(2, state="DONE"),
            task(3, created=None),
            task(4, created=(FIXED_DATE - timedelta(days=7)).isoformat()),
        ]
        self.assertEqual(
            [t["id"] for t in tasks.select_tasks(values, arguments("task"))], [4, 3, 1]
        )
        self.assertEqual(
            [
                t["id"]
                for t in tasks.select_tasks(values, arguments("task", "-s", "DONE"))
            ],
            [2],
        )
        self.assertEqual(
            [
                t["id"]
                for t in tasks.select_tasks(
                    values, arguments("task", "-b", "p11", "--days", "1")
                )
            ],
            [1],
        )
        values = [task(4, repo="sisyphus"), task(1, repo="p11")]
        self.assertEqual(
            [
                t["id"]
                for t in tasks.select_tasks(
                    values, arguments("task", "--sort", "branch")
                )
            ],
            [1, 4],
        )
        self.assertEqual(
            [
                t["id"]
                for t in tasks.select_tasks(values, arguments("task", "--reverse"))
            ],
            [1, 4],
        )

    def test_request_contract_limit_and_detail_reselection(self):
        values = [task(1), task(2), task(3)]
        args = arguments("task", "-n", "2", "--arch")
        with (
            patch.object(tasks, "request_json", return_value=values) as request,
            patch.object(
                tasks, "fetch_statuses", side_effect=lambda rows, args: (rows, [])
            ),
            patch.object(
                tasks,
                "fetch_details",
                return_value=([task(3, state="DONE"), task(2)], []),
            ) as detail,
        ):
            rows, total, warnings = tasks.snapshot(args)
        self.assertEqual(
            request.call_args[0][1], tasks.API + "/tasks?user=scala&repo=ALL&state=ALL"
        )
        self.assertEqual([t["id"] for t in detail.call_args[0][0]], [3, 2])
        self.assertEqual([t["id"] for t in rows], [2])
        self.assertEqual(total, 3)
        self.assertTrue(warnings)

    def test_main_json_errors_and_empty_matches(self):
        args = arguments("task", "--json", "--no-color")
        for rows, warnings, code in [
            ([task()], [], 0),
            ([task()], ["warning: details failed"], 1),
            ([], [], 0),
        ]:
            with (
                patch.object(
                    tasks, "snapshot", return_value=(rows, len(rows), warnings)
                ),
                streams() as (out, err),
            ):
                result = tasks.main(args)
            self.assertEqual(result, code)
            self.assertEqual(json.loads(out.getvalue()), rows)
            self.assertNotIn("\033", out.getvalue() + err.getvalue())
        with (
            patch.object(tasks, "snapshot", side_effect=CheckError("HTTP 503")),
            streams() as (out, err),
        ):
            self.assertEqual(tasks.main(args), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("HTTP 503", err.getvalue())

    def test_watch_keeps_previous_snapshot_on_error(self):
        args = arguments("task", "--watch", "5", "--no-color")
        with (
            patch.object(
                tasks, "snapshot", side_effect=[([], 0, []), CheckError("offline")]
            ),
            patch.object(tasks.time, "sleep", side_effect=[None, KeyboardInterrupt]),
            streams() as (out, err),
        ):
            with self.assertRaises(KeyboardInterrupt):
                tasks.main(args)
        self.assertEqual(out.getvalue().count("Listing"), 1)
        self.assertIn("previous successful snapshot is unchanged", err.getvalue())
        self.assertNotIn("\033", out.getvalue())

    def test_malformed_response(self):
        for value in (
            [],
            {},
            task(id=True),
            task(id=-1),
            task(id=float("inf")),
            task(subtasks=[]),
            task(created=7),
        ):
            with self.subTest(value=value), self.assertRaises(CheckError):
                tasks.normalize(value)
