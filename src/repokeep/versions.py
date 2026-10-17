import re
from dataclasses import dataclass, replace
from datetime import datetime

from .common import CheckError


@dataclass(frozen=True)
class Version:
    numbers: tuple
    stage: int = 5
    stage_numbers: tuple = ()
    text: str = ""
    snapshot: bool = False

    @property
    def calendar(self):
        if len(self.numbers) != 1 or not re.fullmatch(
            r"(?:19|20)\d{6}", str(self.numbers[0])
        ):
            return False
        try:
            datetime.strptime(str(self.numbers[0]), "%Y%m%d")
            return True
        except ValueError:
            return False

    @property
    def major(self):
        return 0 if len(self.numbers) == 1 else self.numbers[0]

    @property
    def prerelease(self):
        return self.stage < 5

    @property
    def key(self):
        numbers = list(self.numbers)
        while len(numbers) > 1 and numbers[-1] == 0:
            numbers.pop()
        return tuple(numbers), self.stage, self.stage_numbers


def parse_version(text, rpm=False):
    text = text.strip()
    original = text
    snapshot = False
    if rpm:
        text, removed = re.subn(r"[._-]git[0-9a-f]{7,40}$", "", text, flags=re.I)
        snapshot = bool(removed)

    text = re.sub(r"^([vV]?\d+)-(\d+\.\d+(?:\.\d+)*)(?=$|[._+-])", r"\1.\2", text)
    if re.fullmatch(r"[vV]?\d+(?:-\d+){2,}", text):
        text = text.replace("-", ".")
    match = re.fullmatch(r"(?:\d+:)?[vV]?(\d+(?:[._]\d+)*)(.*)", text)
    if not match:
        return None
    numbers = tuple(int(x) for x in re.split(r"[._]", match[1]))
    suffix = match[2]

    build = re.fullmatch(r"-(\d+)-g[0-9a-f]{7,40}", suffix, re.I)
    if build:
        numbers += (int(build[1]),)
        suffix = ""

    if (
        suffix.lower() == "-r"
        and len(numbers) > 3
        and re.fullmatch(r"(?:19|20)\d{6}(?:\d{4}(?:\d{2})?)?", str(numbers[-1]))
    ):
        numbers = numbers[:-1]
    suffix = re.sub(r"\+[A-Za-z0-9][A-Za-z0-9._-]*$", "", suffix)

    suffix = re.sub(r"-RI$", "", suffix, flags=re.I)
    stage, stage_numbers = 5, ()
    if suffix and not re.fullmatch(
        r"[._-]?(?:final|ga|release|jre|android|apk)|-sdl2-port|-\d+|-r|\.v\d{8,14}",
        suffix,
        re.I,
    ):
        pre = re.fullmatch(
            r"[._-]?(dev|snapshot|alpha|a|beta|b|milestone|m|rc|cr|pre|preview|ea)"
            r"((?:[._-]?\d+)*)",
            suffix,
            re.I,
        )
        post = re.fullmatch(r"[._-]?(?:post|pl|p|patch)((?:[._-]?\d+)+)", suffix, re.I)
        if pre:
            stage = {
                "dev": 0,
                "snapshot": 0,
                "alpha": 1,
                "a": 1,
                "beta": 2,
                "b": 2,
                "milestone": 3,
                "m": 3,
                "rc": 4,
                "cr": 4,
                "pre": 4,
                "preview": 4,
                "ea": 1,
            }[pre[1].lower()]
            stage_numbers = tuple(int(x) for x in re.findall(r"\d+", pre[2]))
        elif post:
            stage = 6
            stage_numbers = tuple(int(x) for x in re.findall(r"\d+", post[1]))
        else:
            return None
    return Version(numbers, stage, stage_numbers, original, snapshot)


def tag_family(prefix, tag):
    family = re.sub(r"[._/-]+", "-", prefix.lower()).strip("-")

    family = re.sub(r"^(?:(?:rel|releases?|versions?|milestones?)-)+", "", family)
    if re.fullmatch(r"(?:(?:v|r|rel|releases?|versions?|milestones?)-?)*", family):
        family = ""

    flavor = re.search(r"[._-](jre|android|apk|sdl2-port)(?:\+.*)?$", tag, re.I)
    return family + (":" + flavor[1].lower() if flavor else "")


@dataclass(frozen=True)
class Candidate:
    version: Version
    tag: str
    family: str


def parse_tag(tag, minimum_parts=2):
    if re.search(r"(?:^|/)(?:archive|archived|tmp|test|tests)/", tag, re.I):
        return None

    for pattern, fmt in (
        (r"(?:19|20)\d{2}-\d{2}-\d{2}", "%Y-%m-%d"),
        (r"\d{2}-\d{2}-\d{2}", "%y-%m-%d"),
    ):
        if re.fullmatch(r"[vV]?" + pattern, tag):
            try:
                datetime.strptime(tag.lstrip("vV"), fmt)
                return None
            except ValueError:
                pass
    for match in re.finditer(r"(?<!\d)\d+", tag):
        version = parse_version(tag[match.start() :])
        if version and (len(version.numbers) >= minimum_parts or version.calendar):
            return Candidate(version, tag, tag_family(tag[: match.start()], tag))
    return None


def family_score(family, name, repo, spec_aliases=()):
    if not family:
        return 10
    family_base = family.split(":")[0]
    aliases = {name.lower(), repo.split("/")[1].lower()}
    aliases.update(str(x).lower() for x in spec_aliases)
    aliases.add(re.sub(r"^python3-module-", "", name.lower()))
    aliases.add(re.sub(r"^apache-", "", name.lower()))
    for alias in list(aliases):
        aliases.add(re.sub(r"-java$", "", alias))
    for alias in aliases:
        alias = re.sub(r"[._/-]+", "-", alias).strip("-")
        if family_base == alias:
            return 30
        if family_base in (alias + "-parent", alias + "-project"):
            return 20
    return 0


def repository_matches_name(repo, name, spec_aliases=()):
    def normalize(value):
        return re.sub(r"[._/-]+", "-", value.lower()).strip("-")

    aliases = {name, re.sub(r"^(?:python3-module-|apache-)", "", name)}
    aliases.update(spec_aliases)
    aliases.update(re.sub(r"-(?:java|project|parent)$", "", a) for a in list(aliases))
    return normalize(repo.split("/")[1]) in {normalize(a) for a in aliases}


def select_candidates(tags, current, name, repo, tag_regex=None, spec_aliases=()):
    candidates = []
    if tag_regex:
        for tag in tags:
            match = tag_regex.fullmatch(tag)
            if match:
                version = parse_version(match.group("version"))
                if version:
                    candidates.append(Candidate(version, tag, "configured"))
        if not candidates:
            raise CheckError("no version tags match tag_regex")
        return candidates, ""

    for tag in tags:
        candidate = parse_tag(tag, min(2, len(current.numbers)))
        if candidate and candidate.version.calendar == current.calendar:
            candidates.append(candidate)
    flavor = re.search(
        r"[._-](jre|android|apk|sdl2-port)(?:\+.*)?$", current.text, re.I
    )
    if flavor:
        candidates = [
            c for c in candidates if c.family.endswith(":" + flavor[1].lower())
        ]
    if not candidates:
        raise CheckError("no recognizable version tags; use --tag-regex")

    exact = {x.family for x in candidates if x.version.key == current.key}
    note = ""
    families = exact
    if not families:
        families = {x.family for x in candidates}
        note = "current tag not found; family inferred"
    scores = {f: family_score(f, name, repo, spec_aliases) for f in families}
    best_score = max(scores.values())
    winners = [f for f, score in scores.items() if score == best_score]
    if len(winners) != 1 or (not exact and best_score == 0 and len(families) > 1):
        choices = ", ".join(sorted(f or "plain/v" for f in families))
        raise CheckError("ambiguous tag families: " + choices + "; use --tag-regex")
    family = winners[0]
    accepted = {family}

    repo_name = repo.split("/")[1].lower()
    roots = {
        repo_name,
        repo_name + "-parent",
        repo_name + "-project",
        "parent",
        "project",
    }
    if family == "" or family in roots:
        accepted.update({""} | roots)
    return [x for x in candidates if x.family in accepted], note


def compare_versions(current, candidates, include_prereleases=False):
    eligible = [
        c
        for c in candidates
        if c.version.calendar == current.calendar
        and (len(current.numbers) != 1 or len(c.version.numbers) == 1)
        and (
            include_prereleases
            or not c.version.prerelease
            or c.version.key == current.key
        )
    ]
    same = [c for c in eligible if c.version.major == current.major]
    same_latest = max(same, key=lambda c: (c.version.key, c.tag), default=None)
    newer = {}
    for candidate in eligible:
        major = candidate.version.major
        if major > current.major:
            old = newer.get(major)
            if old is None or (candidate.version.key, candidate.tag) > (
                old.version.key,
                old.tag,
            ):
                newer[major] = candidate
    return same_latest, [newer[k] for k in sorted(newer)]


def snapshot_base(current, tags, tag_regex=None):
    if not current.snapshot:
        return current, ""
    base = (
        replace(current, numbers=current.numbers[:-1])
        if len(current.numbers) > 1
        else current
    )
    for tag in tags:
        if tag_regex:
            match = tag_regex.fullmatch(tag)
            candidate = parse_version(match.group("version")) if match else None
        else:
            parsed = parse_tag(tag, min(2, len(base.numbers)))
            candidate = parsed.version if parsed else None
        if candidate and candidate.key == base.key:
            return (
                base,
                "Git snapshot after " + candidate.text + "; commits not compared",
            )
    return current, "Git snapshot; base tag not identified; commits not compared"
