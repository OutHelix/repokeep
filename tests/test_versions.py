import re
import unittest

from repokeep.common import CheckError
from repokeep.versions import (
    compare_versions,
    parse_tag,
    parse_version,
    select_candidates,
    snapshot_base,
)


class VersionsTest(unittest.TestCase):
    def test_special_formats(self):
        cases = {
            "1:3.2.0": ((3, 2, 0), 5),
            "v2-1.13": ((2, 1, 13), 5),
            "1-2-3": ((1, 2, 3), 5),
            "3.96.4-01": ((3, 96, 4), 5),
            "2.3-9258-g85156e2": ((2, 3, 9258), 5),
            "5.11.0.202103091610-r": ((5, 11, 0), 5),
            "4.0.0-M4-RI": ((4, 0, 0), 3),
            "4.0.6-RI": ((4, 0, 6), 5),
            "1.2.3.CR2": ((1, 2, 3), 4),
            "1.2.3.Final": ((1, 2, 3), 5),
            "1.2.3-p2": ((1, 2, 3), 6),
            "1.2.3+build.45": ((1, 2, 3), 5),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = parse_version(text)
                self.assertEqual((parsed.numbers, parsed.stage), expected)
        self.assertEqual(parse_version("3.2.0").key, parse_version("3.2").key)
        for text in ("1.2-alt1", "1.2-unknown", "release-1.2", "not-a-version"):
            self.assertIsNone(parse_version(text))

    def test_java_stage_order(self):
        labels = ["dev1", "alpha1", "beta1", "M1", "RC1", "Final", "patch1"]
        keys = [parse_version("2.0-" + label).key for label in labels]
        self.assertEqual(keys, sorted(set(keys)))
        self.assertLess(parse_version("2.0-RC2").key, parse_version("2.0-RC10").key)

    def candidates(self, tags, current="3.2.1", name="project", repo="owner/project"):
        return select_candidates(tags, parse_version(current), name, repo)[0]

    def test_current_major_and_each_new_major(self):
        tags = ["v3.2.1", "v3.2.4", "v4.0.1", "v4.1.0", "v5.0.0-rc1", "v6.0.0"]
        current = parse_version("3.2.1")
        candidates = self.candidates(tags)
        same, newer = compare_versions(current, candidates)
        self.assertEqual(same.version.text, "3.2.4")
        self.assertEqual([v.version.text for v in newer], ["4.1.0", "6.0.0"])
        self.assertEqual(
            [v.version.major for v in compare_versions(current, candidates, True)[1]],
            [4, 5, 6],
        )

    def test_serial_calendar_and_dates(self):
        for current, tags, latest in [
            ("43", ["43", "44", "45", "46.0"], "45"),
            ("20260101", ["20260101", "20261001", "20270101", "30.0"], "20270101"),
        ]:
            same, newer = compare_versions(
                parse_version(current), self.candidates(tags, current)
            )
            self.assertEqual(same.version.text, latest)
            self.assertEqual(newer, [])
        self.assertFalse(parse_version("20260230").calendar)
        for tag in ("23-01-02", "2026-01-02", "archive/v2.0", "tmp/v2.0", "tests/v2.0"):
            self.assertIsNone(parse_tag(tag))

    def test_monorepo_and_root_family_transitions(self):
        tags = [
            "module-a-1.2.0",
            "module-a-1.3.0",
            "module-b-1.2.0",
            "module-b-9.0.0",
            "v8.0.0",
        ]
        chosen = self.candidates(tags, "1.2.0", "module-a", "owner/monorepo")
        self.assertEqual({c.tag for c in chosen}, {"module-a-1.2.0", "module-a-1.3.0"})
        chosen = self.candidates(
            ["maven-3.2.1", "v4.0.1", "other-9.0.0"], repo="apache/maven"
        )
        self.assertEqual({c.tag for c in chosen}, {"maven-3.2.1", "v4.0.1"})
        chosen = self.candidates(
            ["rel/project-3.2.1", "project-3.2.4-RC1", "project-3.2.4"]
        )
        self.assertEqual(len(chosen), 3)

    def test_artifact_isolation(self):
        tags = ["v31.0-jre", "v32.0-jre", "v31.0-android", "v99.0-android", "v100.0"]
        chosen = self.candidates(tags, "31.0-jre")
        self.assertEqual({c.tag for c in chosen}, {"v31.0-jre", "v32.0-jre"})
        for flavor in ("apk", "sdl2-port"):
            tags = ["v1.2-" + flavor, "v1.3-" + flavor, "v1.2", "v99.0"]
            chosen = self.candidates(tags, "1.2-" + flavor)
            self.assertEqual({c.tag for c in chosen}, set(tags[:2]))
        with self.assertRaisesRegex(CheckError, "ambiguous"):
            self.candidates(["one-1.0", "two-1.0"], "0.9", "missing", "owner/monorepo")

    def test_override_and_snapshot(self):
        regex = re.compile(r"component-v(?P<version>\d+\.\d+\.\d+)")
        tags = ["component-v1.2.3", "component-v1.2.4", "other-v99.0.0"]
        chosen, _ = select_candidates(tags, parse_version("1.2.3"), "p", "o/r", regex)
        self.assertEqual([c.tag for c in chosen], tags[:2])
        current = parse_version("1.2.3.17.gitabcdef123", rpm=True)
        base, note = snapshot_base(current, tags, regex)
        self.assertEqual(base.numbers, (1, 2, 3))
        self.assertIn("commits not compared", note)
        unchanged, note = snapshot_base(current, ["v9.0.0"])
        self.assertEqual(unchanged.numbers, current.numbers)
        self.assertIn("base tag not identified", note)

    def test_prerelease_current_tag_is_still_eligible(self):
        current = parse_version("2.0-RC1")
        chosen = self.candidates(["v2.0-RC1", "v2.0-RC2"], "2.0-RC1")
        self.assertEqual(compare_versions(current, chosen)[0].version.text, "2.0-RC1")
