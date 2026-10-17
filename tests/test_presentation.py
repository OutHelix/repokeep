import json
import os
import unittest
from unittest.mock import patch

from repokeep import updates
from repokeep.cli import parser
from repokeep.models import Result
from repokeep.output import print_heading, render, wrapped, wrapped_parts
from tests.support import arguments, plain_text, streams


class PresentationTest(unittest.TestCase):
    def test_update_rows_fit_terminal_without_losing_versions_or_notes(self):
        cases = [("UPD", width) for width in (1, 8, 40, 80, 160)]
        cases += [(status, 80) for status in ("MAJOR", "OK", "SKIP", "WARN", "ERR")]
        for status, width in cases:
            with self.subTest(status=status, width=width):
                result = Result(
                    "python3-module-long-package-name",
                    "3.2.1",
                    "alt1",
                    status=status,
                    same_major="3.2.4",
                    repo="owner/long-package-name",
                    newer_majors=[{"version": "4.0.1"}],
                    note="latest stable release in the current major",
                )
                options = dict(
                    show_repo=True,
                    name_width=36,
                    current_width=8,
                    done=2,
                    total=12,
                    width=width,
                )
                plain = render(result, **options)
                colored = render(result, color=True, **options)
                self.assertEqual(plain_text(colored), plain)
                self.assertNotIn("\033", plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()))
                normalized = "".join(plain.split())
                for value in (
                    "[2/12]",
                    "[" + status + "]",
                    result.name,
                    result.current,
                    result.same_major,
                    "4.0.1",
                    result.note,
                    "https://github.com/" + result.repo,
                ):
                    self.assertIn("".join(value.split()), normalized)
                self.assertNotIn("\033[1;36m" + result.name, colored)
                self.assertFalse(
                    any(line.strip() == "|" for line in plain.splitlines())
                )

    def test_security_modes_keep_names_plain(self):
        result = Result(
            "python3-module-example",
            "1.0",
            "alt1",
            status="ERR",
            security=True,
            note="request failed; partial data retained",
            vulnerabilities=[
                {"id": "CVE-example", "aliases": [], "fixed_versions": []}
            ],
        )
        for details, width in ((False, 40), (True, 40), (True, 1), (False, 120)):
            with self.subTest(details=details, width=width):
                options = dict(done=1, total=1, width=width, details=details)
                plain = render(result, **options)
                colored = render(result, color=True, **options)
                self.assertEqual(plain_text(colored), plain)
                self.assertTrue(all(len(line) <= width for line in plain.splitlines()))
                self.assertIn("\033[1;31m[", colored)
                self.assertNotIn("\033[1;36m", colored)

    def test_update_heading_adapts_to_width_and_json_mode(self):
        packages = [{"name": "a-long-package-name", "version": "1.0"}]
        for width in (20, 40, 160):
            for json_mode in (False, True):
                with self.subTest(width=width, json=json_mode):
                    args = arguments(
                        "updates", "--no-color", *(["--json"] if json_mode else [])
                    )
                    with (
                        patch(
                            "shutil.get_terminal_size",
                            return_value=os.terminal_size((width, 24)),
                        ),
                        streams() as (out, err),
                    ):
                        print_heading(packages, args)
                    self.assertEqual(out.getvalue(), "")
                    self.assertTrue(
                        all(len(line) <= width for line in err.getvalue().splitlines())
                    )
                    self.assertEqual(
                        "STATUS" in err.getvalue(), width == 160 and not json_mode
                    )

    def test_wrapping_indents_never_exceed_available_width(self):
        for width in (1, 2, 8, 40):
            for wrap in (
                lambda: wrapped(
                    "an-example-value", width, indent="    ", continuation="        "
                ),
                lambda: wrapped_parts(
                    [("an-example-value", "36")],
                    width,
                    indent="    ",
                    continuation="        ",
                    color=True,
                ),
            ):
                with self.subTest(width=width):
                    plain = plain_text(wrap())
                    self.assertTrue(
                        all(len(line) <= width for line in plain.splitlines())
                    )
                    self.assertEqual("".join(plain.split()), "an-example-value")

    def test_updates_headings_summary_and_results_keep_their_streams(self):
        package = {"name": "python3-module-long-package-name", "version": "1.0"}
        cases = (
            (False, False, "never"),
            (False, True, "always"),
            (True, False, "always"),
            (True, True, "never"),
        )
        for security, json_mode, color in cases:
            with self.subTest(security=security, json=json_mode, color=color):
                args = arguments(
                    "updates",
                    "--color",
                    color,
                    *(["--vulnerable-only"] if security else []),
                    *(["--json"] if json_mode else []),
                )
                result = Result(
                    package["name"],
                    "1.0",
                    "alt1",
                    status="VULN" if security else "UPD",
                    same_major="1.1",
                    security=security,
                    vulnerabilities=[
                        {"id": "CVE-example", "aliases": [], "fixed_versions": []}
                    ]
                    if security
                    else [],
                )
                with (
                    patch.object(updates.shutil, "which", return_value="/test/git"),
                    patch.object(updates, "load_rules", return_value={}),
                    patch.object(
                        updates, "package_list", return_value={"packages": [package]}
                    ),
                    patch.object(
                        updates, "package_results", return_value=(r for r in [result])
                    ),
                    patch(
                        "shutil.get_terminal_size",
                        return_value=os.terminal_size((40, 24)),
                    ),
                    streams() as (out, err),
                ):
                    self.assertEqual(updates.main(args, parser()[1]["updates"]), 0)
                self.assertNotIn("Checking", out.getvalue())
                self.assertNotIn("Summary:", out.getvalue())
                self.assertIn("Summary:", err.getvalue())
                self.assertTrue(
                    all(
                        len(line) <= 40
                        for line in plain_text(err.getvalue()).splitlines()
                    )
                )
                if json_mode:
                    self.assertEqual(json.loads(out.getvalue())[0]["name"], result.name)
                    self.assertNotIn("\033", out.getvalue())
                else:
                    self.assertTrue(
                        all(
                            len(line) <= 40
                            for line in plain_text(out.getvalue()).splitlines()
                        )
                    )
                if color == "never":
                    self.assertNotIn("\033", out.getvalue() + err.getvalue())
