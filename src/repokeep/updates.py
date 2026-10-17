import argparse
import fnmatch
import json
import os
import re
import shutil
import sys
import time
from collections import Counter, deque
from dataclasses import asdict
from pathlib import Path

from .common import (
    CheckError,
    clean,
    color_enabled,
    maintainer_login,
    output_arguments,
    paint,
    positive,
)
from .models import Result, vulnerability_result
from .osv import osv_package
from .output import (
    STATUS_COLORS,
    print_heading,
    render,
    security_summary,
    security_warning,
    wrapped,
    wrapped_parts,
)
from .upstream import github_repo
from .versions import (
    compare_versions,
    parse_version,
    repository_matches_name,
    select_candidates,
    snapshot_base,
)
from .workers import receive_worker, request_worker, start_worker, stop_workers

ACL_MODES = (
    "none",
    "by_nick",
    "by_nick_leader",
    "by_nick_or_group",
    "by_nick_leader_and_group",
)


def check_package(package, rule, client, args):
    result = Result(package["name"], package["version"], package.get("release", ""))
    try:
        current = parse_version(rule.get("current_version", result.current), rpm=True)
        if current is None:
            raise CheckError("unrecognized RPM version; configure current_version")
        result.repo, result.repo_source = client.resolve_repo(
            package, rule, args.branch
        )
        if not result.repo:
            result.status = "SKIP"
            result.note = (
                result.repo_source
                or client.repo_note
                or "no GitHub upstream found; use --repo"
            )
            return result
        tags = client.tags(result.repo)
        current, snapshot_note = snapshot_base(
            current, tags, rule.get("compiled_regex")
        )
        candidates, result.note = select_candidates(
            tags,
            current,
            result.name,
            result.repo,
            rule.get("compiled_regex"),
            client.spec_aliases,
        )
        if (
            result.note
            and not client.repo_confident
            and not repository_matches_name(
                result.repo, result.name, client.spec_aliases
            )
        ):
            result.status = "WARN"
            result.note = (
                "current tag missing and repository name differs: "
                + result.repo
                + "; verify upstream with --repo"
            )
            return result
        same, newer = compare_versions(current, candidates, args.include_prereleases)
        result.newer_majors = [{"version": c.version.text, "tag": c.tag} for c in newer]
        if same:
            result.same_major, result.same_major_tag = same.version.text, same.tag
        if same and same.version.key > current.key:
            result.status = "UPD"
        elif newer:
            result.status = "MAJOR"
        elif not same:
            result.status = "WARN"
            result.note = "no eligible tags in current major; use --include-prereleases or --tag-regex"
        elif same.version.key < current.key:
            result.status, result.note = (
                "WARN",
                "RPM version newer than matching upstream tags",
            )
        elif result.note or snapshot_note or client.repo_note:
            result.status = "WARN"
        else:
            result.status = "OK"
        result.note = "; ".join(
            filter(None, (result.note, snapshot_note, client.repo_note))
        )
    except CheckError as exc:
        result.note = str(exc)
        result.status = "ERR"
    return result


def assignment(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected PACKAGE=VALUE")
    name, setting = value.split("=", 1)
    if not name or not setting:
        raise argparse.ArgumentTypeError("expected nonempty PACKAGE=VALUE")
    return name, setting


def configure_parser(parser):
    parser.description = (
        "Compare ALT source package versions with GitHub tags. "
        "The newest version of the current major is shown first; each higher "
        "major is shown separately. Integer and YYYYMMDD releases use one series. "
        "Parallel checks finish independently; --sort prints alphabetically."
    )
    parser.epilog = (
        "OSV checks upstream versions; ALT backports are not checked. "
        "Exit codes: 0 success, 2 invalid options or failed/incomplete checks. "
        "Headings, summary and diagnostics go to stderr; results go to stdout."
    )

    if hasattr(parser, "color"):
        parser.color = False
    parser.add_argument(
        "packages", nargs="*", help="package names or quoted shell globs"
    )
    parser.add_argument(
        "-m",
        "--maintainer",
        type=maintainer_login,
        required=True,
        help="package maintainer; --acl selects the ownership mode",
    )
    parser.add_argument(
        "-b", "--branch", default="sisyphus", help="ALT branch (default: sisyphus)"
    )
    parser.add_argument(
        "-g",
        "--group",
        action="append",
        default=[],
        metavar="GROUP",
        help="RPM category or short name; python includes Python/Python3; repeatable (OR)",
    )
    parser.add_argument(
        "--vulnerable-only",
        action="store_true",
        help="only OSV upstream vulnerability matches; compact package/ID list; no token; ALT backports are not checked",
    )
    parser.add_argument(
        "--vuln-details",
        action="store_true",
        help="with --vulnerable-only, show descriptions, severity and fixed upstream releases",
    )
    parser.add_argument(
        "--osv-package",
        action="append",
        type=assignment,
        default=[],
        metavar="PACKAGE=ECOSYSTEM:NAME",
        help="override OSV identity; Maven names are groupId:artifactId; repeatable",
    )
    parser.add_argument(
        "--acl",
        choices=ACL_MODES,
        default="none",
        help="ownership filter (default: packager address)",
    )
    parser.add_argument(
        "-p",
        "--package",
        action="append",
        default=[],
        metavar="PATTERN",
        help="case-sensitive name or quoted shell glob; repeatable (OR)",
    )
    parser.add_argument(
        "-u",
        "--updates-only",
        action="store_true",
        help="hide OK results; keep updates and problems",
    )
    parser.add_argument(
        "--hide-skipped",
        action="store_true",
        help="hide SKIP rows; keep their count in the summary",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="print packages alphabetically, ignoring case; results may wait for earlier packages",
    )
    parser.add_argument(
        "--include-prereleases",
        action="store_true",
        help="also consider alpha/beta/rc/dev tags",
    )
    parser.add_argument(
        "--repo",
        action="append",
        type=assignment,
        default=[],
        metavar="PACKAGE=OWNER/REPO",
        help="override upstream GitHub repository; repeatable",
    )
    parser.add_argument(
        "--tag-regex",
        action="append",
        type=assignment,
        default=[],
        metavar="PACKAGE=REGEX",
        help="full tag pattern with (?P<version>...) group; repeatable",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="JSON config (default: $XDG_CONFIG_HOME/repokeep/config.json or ~/.config/repokeep/config.json; missing default is OK)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=positive,
        default=1,
        help="parallel package checks (default: 1, maximum: 2); use --sort to keep alphabetical output",
    )
    parser.add_argument(
        "--timeout",
        type=positive,
        default=15,
        help="HTTP/Git request timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--package-timeout",
        type=positive,
        default=15,
        help="total time limit per package, seconds (default: 15)",
    )
    parser.add_argument(
        "--show-repo",
        action="store_true",
        help="show the upstream GitHub repository",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="hide checked/total counters in package rows",
    )
    parser.add_argument(
        "--current-version",
        action="append",
        type=assignment,
        default=[],
        metavar="PACKAGE=VERSION",
        help="override upstream Version (not ALT Release); repeatable",
    )
    output_arguments(
        parser, "output a JSON array of results; diagnostics and summary go to stderr"
    )


def validate_args(args, parser):
    args.branch = args.branch.strip().lower()
    if args.branch == "all" or not re.fullmatch(r"[a-z0-9_.-]+", args.branch):
        parser.error("--branch: expected one ALT branch")
    if any(not pattern for pattern in args.packages + args.package):
        parser.error("--package must not be empty")
    if args.jobs > 2:
        parser.error("--jobs must be at most 2")
    if args.vuln_details and not args.vulnerable_only:
        parser.error("--vuln-details requires --vulnerable-only")
    aliases = {
        "java": ["Development/Java"],
        "python": ["Development/Python", "Development/Python3"],
        "python3": ["Development/Python3"],
    }
    groups = [value.strip(" /") for value in args.group]
    if any(not value for value in groups):
        parser.error("--group must not be empty")
    args.group = list(
        dict.fromkeys(
            group
            for value in groups
            for group in aliases.get(
                value.lower(),
                [value if "/" in value else "Development/" + value.capitalize()],
            )
        )
    )


def load_rules(args, parser):
    explicit_config = args.config is not None
    if args.config is None:
        root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        args.config = root / "repokeep" / "config.json"
    rules = {}
    if args.config.exists():
        try:
            document = json.loads(args.config.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("config must be an object")
            rules = document["packages"]
            if not isinstance(rules, dict):
                raise ValueError("packages must be an object")
            if any(not isinstance(rule, dict) for rule in rules.values()):
                raise ValueError("each package rule must be an object")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error("invalid config: " + clean(str(exc)))
    elif explicit_config:
        parser.error("config file does not exist: " + str(args.config))
    for setting in ("current_version", "repo", "tag_regex"):
        for name, value in getattr(args, setting):
            rules.setdefault(name, {})[setting] = value
    for name, identity in args.osv_package:
        rule = rules.setdefault(name, {})
        identities = rule.setdefault("osv_packages", [])
        if isinstance(identities, str):
            identities = rule["osv_packages"] = [identities]
        if not isinstance(identities, list):
            parser.error("osv_packages must be a list for " + name)
        identities.append(identity)
    for name, rule in rules.items():
        if "osv_packages" in rule:
            identities = rule["osv_packages"]
            if isinstance(identities, str):
                identities = [identities]
            if (
                not isinstance(identities, list)
                or not identities
                or any(not isinstance(i, str) for i in identities)
            ):
                parser.error(
                    "osv_packages must contain ECOSYSTEM:NAME strings for " + name
                )
            try:
                rule["osv_packages"] = [osv_package(i) for i in identities]
            except ValueError as exc:
                parser.error("invalid OSV identity for " + name + ": " + str(exc))
        if "repo" in rule:
            repo = github_repo(str(rule["repo"]), allow_short=True)
            if not repo:
                parser.error("invalid GitHub repository for " + name)
            rule["repo"] = repo
        if "current_version" in rule and not parse_version(
            str(rule["current_version"])
        ):
            parser.error("invalid current_version for " + name)
        if "current_version" in rule:
            rule["current_version"] = str(rule["current_version"])
        if "tag_regex" in rule:
            try:
                regex = re.compile(rule["tag_regex"])
            except (re.error, TypeError) as exc:
                parser.error("invalid tag_regex for " + name + ": " + clean(str(exc)))
            if "version" not in regex.groupindex:
                parser.error("tag_regex for " + name + " needs (?P<version>...)")
            rule["compiled_regex"] = regex
    return rules


def request_data(args, endpoint, **parameters):
    return request_worker(
        "request",
        (endpoint, parameters),
        args,
        "ALTRepo request timed out: " + endpoint,
    )


def package_list(args):
    return request_data(
        args,
        "/site/maintainer_packages",
        branch=args.branch,
        maintainer_nickname=args.maintainer,
        by_acl=args.acl,
    )


def filter_groups(packages, args):
    if not args.group:
        return packages
    selected = {}
    for group in args.group:
        try:
            response = request_data(
                args,
                "/site/repository_packages",
                branch=args.branch,
                package_type="source",
                group=group,
            )
        except CheckError as exc:
            hint = (
                "; category is absent or invalid in " + args.branch
                if str(exc).startswith("HTTP 400 ")
                else ""
            )
            raise CheckError("RPM group " + group + ": " + str(exc) + hint) from None
        entries = response.get("packages")
        if not isinstance(entries, list) or any(
            not isinstance(p, dict) or not isinstance(p.get("name"), str)
            for p in entries
        ):
            raise CheckError("invalid RPM group response")
        selected.update({entry["name"]: entry for entry in entries})
    filtered = []
    for package in packages:
        metadata = selected.get(package["name"])
        if metadata:
            package = dict(package, category=metadata.get("category", ""))
            if (metadata.get("version"), metadata.get("release")) == (
                package["version"],
                package.get("release"),
            ):
                package["hash"] = metadata.get("hash")
            filtered.append(package)
    return filtered


def package_results(packages, rules, args):
    active = []
    pending = deque(packages)
    ready = {}
    next_result = 0
    finished = 0
    last_activity = time.monotonic()
    try:
        while active or pending:
            while pending and len(active) < args.jobs:
                package = pending.popleft()
                worker = start_worker(
                    "package", (package, rules.get(package["name"], {})), args
                )
                worker["package"] = package
                active.append(worker)
            for worker in active[:]:
                package = worker["package"]
                error = None
                result = None
                if worker["connection"].poll():
                    try:
                        result = receive_worker(worker, progress=True)
                    except CheckError as exc:
                        error = str(exc)
                elif time.monotonic() - worker["started"] >= args.package_timeout:
                    error = f"package check timed out after {args.package_timeout}s"
                elif not worker["process"].is_alive():
                    error = "worker exited without a result"
                else:
                    continue
                if result is None and not error:
                    if time.monotonic() - worker["started"] >= args.package_timeout:
                        error = f"package check timed out after {args.package_timeout}s"
                    else:
                        continue
                stop_workers([worker])
                active.remove(worker)
                if error:
                    result = worker.get("partial")
                    if result is None:
                        result = (
                            vulnerability_result(package)
                            if args.vulnerable_only
                            else Result(
                                package["name"],
                                package["version"],
                                package.get("release", ""),
                            )
                        )
                    result.status, result.note = "ERR", error
                finished += 1
                last_activity = time.monotonic()
                if args.sort:
                    ready[package["name"]] = result
                    while (
                        next_result < len(packages)
                        and packages[next_result]["name"] in ready
                    ):
                        result = ready.pop(packages[next_result]["name"])
                        next_result += 1
                        yield result
                else:
                    yield result
            if (
                active
                and not args.no_progress
                and time.monotonic() - last_activity >= 10
            ):
                waiting = ", ".join(
                    clean(w["package"]["name"])
                    + f" ({int(time.monotonic() - w['started'])}s)"
                    for w in active
                )
                print(
                    paint(
                        f"Waiting [{finished}/{len(packages)}]: " + waiting,
                        "90",
                        color_enabled(args.color, sys.stderr),
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                last_activity = time.monotonic()
            if active:
                time.sleep(0.05)
    finally:
        stop_workers(active)


def main(args, parser):
    if not args.vulnerable_only and not shutil.which("git"):
        parser.error("git is required")
    rules = load_rules(args, parser)
    security_warning(args)
    try:
        response = package_list(args)
        packages = response["packages"]
        if not isinstance(packages, list) or any(
            not isinstance(p, dict)
            or not isinstance(p.get("name"), str)
            or not isinstance(p.get("version"), str)
            for p in packages
        ):
            raise CheckError("invalid package list from ALTRepo API")
        unique = {}
        for package in packages:
            old = unique.get(package["name"])
            if old is None or package.get("buildtime", 0) > old.get("buildtime", 0):
                unique[package["name"]] = package
        packages = sorted(unique.values(), key=lambda p: p["name"].lower())
        patterns = args.packages + args.package
        for pattern in patterns:
            if not any(fnmatch.fnmatchcase(p["name"], pattern) for p in packages):
                parser.error(
                    "no package matches " + pattern + " for " + args.maintainer
                )
        if patterns:
            packages = [
                p
                for p in packages
                if any(fnmatch.fnmatchcase(p["name"], pat) for pat in patterns)
            ]
        if not packages:
            raise CheckError("ALTRepo returned an empty package list")
        packages = filter_groups(packages, args)
        if not packages:
            if args.json:
                print("[]")
            print(
                "No maintainer packages match the selected RPM groups.", file=sys.stderr
            )
            return 0
    except (CheckError, KeyError, TypeError) as exc:
        print("error: " + clean(str(exc)), file=sys.stderr)
        return 2
    count = Counter()
    completed = 0
    matched = 0
    name_width, current_width = print_heading(packages, args)
    json_rows = []
    results = package_results(packages, rules, args)
    try:
        for result in results:
            completed += 1
            count[result.status] += 1
            matched += bool(result.vulnerabilities)
            if args.hide_skipped and result.status == "SKIP":
                continue
            if args.vulnerable_only and not result.vulnerabilities:
                if result.status in ("SKIP", "WARN", "ERR"):
                    print(
                        wrapped(
                            f"[{result.status}] "
                            + clean(result.name)
                            + ": "
                            + clean(result.note),
                            continuation="  ",
                            code=STATUS_COLORS[result.status],
                            color=color_enabled(args.color, sys.stderr),
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                continue
            if args.updates_only and result.status == "OK":
                continue
            if args.json:
                json_rows.append(asdict(result))
                continue
            print(
                render(
                    result,
                    args.show_repo,
                    name_width,
                    current_width,
                    done=completed if not args.no_progress else None,
                    total=len(packages),
                    color=color_enabled(args.color, sys.stdout),
                    details=args.vuln_details,
                ),
                end="\n\n" if args.vulnerable_only else "\n",
                flush=True,
            )
    finally:
        results.close()
    if args.json:
        print(json.dumps(json_rows, ensure_ascii=True, indent=2))
    if (
        args.vulnerable_only
        and count["OK"]
        and not matched
        and not any(count[s] for s in ("WARN", "ERR"))
    ):
        print(
            "No OSV vulnerability matches found in the checked upstream versions.",
            file=sys.stderr,
        )
    if args.vulnerable_only:
        print(
            "\n"
            + security_summary(
                count, matched, completed, color_enabled(args.color, sys.stderr)
            ),
            file=sys.stderr,
            flush=True,
        )
    else:
        parts = [("Summary: ", None)]
        for status in ("UPD", "MAJOR", "OK", "SKIP", "WARN", "ERR"):
            if len(parts) > 1:
                parts.append(("  |  ", "90"))
            parts.append((f"{status}={count[status]}", STATUS_COLORS[status]))
        print(
            "\n"
            + wrapped_parts(
                parts,
                width=shutil.get_terminal_size().columns,
                continuation="  ",
                color=color_enabled(args.color, sys.stderr),
            ),
            file=sys.stderr,
            flush=True,
        )
    if count["ERR"] or count["WARN"] or (args.vulnerable_only and count["SKIP"]):
        return 2
    return 0
