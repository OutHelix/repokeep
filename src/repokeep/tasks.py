import argparse
import json
import math
import re
import shutil
import sys
import time
import urllib.parse
from collections import Counter, deque
from datetime import date, datetime, timedelta
from enum import Enum

from . import task_status
from .common import (
    CheckError,
    clean,
    color_enabled,
    maintainer_login,
    output_arguments,
    paint,
    positive,
)
from .output import wrapped_parts
from .workers import receive_worker, request_worker, start_worker, stop_workers

API = "https://git.altlinux.org/tasks/api"
ARCHITECTURES = ("x86_64", "i586", "aarch64")
STATE_COLORS = {
    "NEW": "36",
    "AWAITING": "36",
    "PENDING": "36",
    "BUILDING": "1;36",
    "COMMITTING": "1;35",
    "TESTED": "32",
    "FAILING": "1;31",
    "FAILED": "1;31",
    "DONE": "32",
    "SWEPT": "90",
    "EPERM": "1;33",
    "POSTPONED": "35",
}


class ArchStatus(Enum):
    SUCCESS = "built/cached"
    FAILED = "failed"
    SKIPPED = "skipped"
    WAITING = "waiting"
    MISSING = "no data"
    UNKNOWN = "unknown"


def task_clean(value):
    return clean(re.sub(r"[\u202a-\u202e\u2066-\u2069]", " ", str(value)))


def normalize(value):
    if not isinstance(value, dict):
        raise CheckError("expected a task object")
    task = dict(value)
    identifier = task.get("id")
    if identifier is None or identifier is False:
        identifier = task.get("taskid")
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, (int, float))
        or (isinstance(identifier, float) and not math.isfinite(identifier))
        or identifier <= 0
        or int(identifier) != identifier
    ):
        raise CheckError("invalid or missing task ID")
    task["id"] = int(identifier)
    if any(
        not isinstance(task.get(key), str) or not task[key]
        for key in ("owner", "state", "repo")
    ):
        raise CheckError("missing owner/state/repo")
    if task.get("subtasks") is not None and not isinstance(task["subtasks"], dict):
        raise CheckError("invalid subtasks")
    for key in ("created", "build_time"):
        if task.get(key) is not None and not isinstance(task[key], str):
            raise CheckError("invalid task date")
    task["state"] = task_clean(task["state"]).upper()
    return task


def package_name(subtask):
    if subtask.get("pkgname"):
        name = subtask["pkgname"]
    elif subtask.get("dir"):
        name = str(subtask["dir"]).rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    elif subtask.get("srpm"):
        name = str(subtask["srpm"]).rsplit("/", 1)[-1]
        name = re.sub(r"-[^-]+-[^-]+\.src\.rpm$", "", name)
        name = re.sub(r"\.src\.rpm$", "", name)
    else:
        name = "(unknown package)"
    return task_clean(name)


def package_rows(task, source=False):
    subtasks = task.get("subtasks") or {}

    def order(item):
        try:
            number = float(item[0])
        except ValueError:
            number = 999999999
        return number, item[0]

    rows = []
    for number, subtask in sorted(subtasks.items(), key=order):
        if not isinstance(subtask, dict) or not any(
            subtask.get(key) for key in ("type", "pkgname", "dir", "srpm")
        ):
            continue
        name = package_name(subtask)
        if source:
            info = source_info(task, number, subtask)
            subtask = dict(subtask, source=info)
            name += " [" + info["label"] + "]"
        rows.append((task_clean(number), name, subtask))
    return rows


def packages(task):
    return sorted(
        {name for _, name, _ in package_rows(task)}, key=lambda s: (s.lower(), s)
    )


def source_info(task, number, subtask):
    subtask = subtask or {}
    kind = subtask.get("type")
    if kind == "repo" and subtask.get("dir"):
        label = task_clean(str(subtask["dir"]).rstrip("/").rsplit("/", 1)[-1])
        if subtask.get("tag_name"):
            label += "=" + task_clean(subtask["tag_name"])
        filename = "git"
    elif kind == "srpm" and subtask.get("srpm"):
        label = task_clean(str(subtask["srpm"]).rsplit("/", 1)[-1])
        filename = urllib.parse.quote(label, safe="")
    else:
        label = "not available"
        if kind == "copy":
            label = (
                "copy from " + task_clean(subtask["copy_repo"])
                if subtask.get("copy_repo")
                else "copy (source branch unavailable)"
            )
        elif kind == "delete":
            label = "not applicable (delete)"
        return {"label": label, "url": None}
    number = urllib.parse.quote(str(number), safe="")
    return {
        "label": label,
        "url": f"https://git.altlinux.org/tasks/{task['id']}/gears/{number}/{filename}",
    }


def with_sources(task):
    result = dict(task, subtasks=dict(task.get("subtasks") or {}))
    for number, subtask in result["subtasks"].items():
        if isinstance(subtask, dict) and any(
            subtask.get(key) for key in ("type", "pkgname", "dir", "srpm")
        ):
            result["subtasks"][number] = dict(
                subtask, source=source_info(task, number, subtask)
            )
    return result


def arch_status(subtask, architecture):
    if subtask is None:
        return ArchStatus.MISSING
    build = subtask.get("build")
    status = build.get(architecture) if isinstance(build, dict) else None
    if status in ("built", "cached"):
        return ArchStatus.SUCCESS
    if status == "failed":
        return ArchStatus.FAILED
    if status in ("excluded", "swept"):
        return ArchStatus.SKIPPED
    if status in (None, ""):
        return (
            ArchStatus.SKIPPED
            if subtask.get("type") in ("copy", "delete")
            else ArchStatus.MISSING
        )
    if status in ("pending", "waiting", "building", "awaiting", "new"):
        return ArchStatus.WAITING
    return ArchStatus.UNKNOWN


def package_matches(name, pattern):
    name, pattern = name.lower(), pattern.lower()

    if "*" in pattern or "?" in pattern:
        expression = "".join(
            ".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern
        )
        return re.fullmatch(expression, name) is not None
    return pattern in name


def select_tasks(values, args):
    if not isinstance(values, list):
        raise CheckError("API response must be a task array")
    selected = []
    for raw in values:
        task = normalize(raw)
        if task["owner"].lower() != args.maintainer:
            continue
        if args.branch != "ALL" and task["repo"].lower() not in args.branch.split(","):
            continue
        excluded_state = (
            task["state"] not in args.state if args.state else task["state"] == "DONE"
        )
        if excluded_state:
            continue
        if args.package and not any(
            package_matches(name, pattern)
            for name in packages(task)
            for pattern in args.package
        ):
            continue
        if args.since and (
            not task.get("created") or task["created"][:10] < args.since
        ):
            continue
        selected.append(task)

    def key(task):
        if args.sort == "id":
            return task["id"]
        if args.sort == "package":
            names = packages(task)
            return (names[0].lower() if names else "~"), task["id"]
        field = "repo" if args.sort == "branch" else "state"
        return task[field].lower(), task["id"]

    return sorted(selected, key=key, reverse=(args.sort == "id") != args.reverse)


def uint(value):
    if not re.fullmatch(r"[0-9]{1,9}", value):
        raise argparse.ArgumentTypeError(
            "must be a non-negative integer (at most 9 digits)"
        )
    return int(value)


def configure_parser(parser):
    parser.description = (
        "List Girar tasks owned by a specific maintainer. Defaults: all branches, "
        "all states except DONE, newest first. Package filters select tasks; all "
        "their packages remain visible. Output adapts to terminal width. "
        "Stages appear for BUILDING and failed checks for FAILED tasks. "
        "TEST means no automatic commit."
    )
    parser.epilog = (
        "Only tasks still available in the API are listed; this is not a complete archive. "
        "Exit codes: 0 success (including no matches), 1 API/detail error, 2 invalid options. "
        "Architecture colors: green built/cached, red failed, gray skipped/waiting/no data/unknown."
    )
    if hasattr(parser, "color"):
        parser.color = False
    parser.add_argument(
        "-m",
        "--maintainer",
        type=maintainer_login,
        required=True,
        help="task owner, not subtask author or package ACL member",
    )
    parser.add_argument(
        "-b",
        "-r",
        "--branch",
        "--repo",
        default="ALL",
        help="branch, comma-separated branches or ALL (default: ALL)",
    )
    parser.add_argument(
        "-s",
        "--state",
        action="append",
        default=[],
        help="comma-separated states; repeatable; explicitly select DONE here",
    )
    parser.add_argument(
        "-p",
        "--package",
        action="append",
        default=[],
        metavar="PATTERN",
        help="case-insensitive substring or quoted '*'/'?' glob; repeatable (OR)",
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=uint,
        default=0,
        help="first N tasks after selection/sorting; 0 means all",
    )
    parser.add_argument(
        "--days",
        type=positive,
        help="creation date in last N local calendar days, including today",
    )
    parser.add_argument(
        "--sort",
        choices=("id", "package", "state", "branch"),
        default="id",
        help="id descending; other fields alphabetical (default: id)",
    )
    parser.add_argument(
        "--reverse", action="store_true", help="reverse the chosen sort order"
    )
    parser.add_argument(
        "--arch",
        action="store_true",
        help="fetch details; color x86_64, i586 and aarch64 per subtask",
    )
    parser.add_argument(
        "--source",
        action="store_true",
        help="show package names followed by source names and Git tags; add source objects to JSON",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="show both architectures and sources (--arch --source)",
    )
    parser.add_argument(
        "-w",
        "--watch",
        type=uint,
        nargs="?",
        const=30,
        default=None,
        help="refresh every N seconds (default: 30; minimum: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=positive,
        default=15,
        help="total request timeout in seconds (default: 15)",
    )
    output_arguments(
        parser, "output a JSON array (details with --arch, --source or --all)"
    )


def validate_args(args, parser):
    args.arch = args.arch or args.all
    args.source = args.source or args.all
    branch = re.sub(r"\s+", "", args.branch).lower()
    if not re.fullmatch(r"[a-z0-9_.-]+(?:,[a-z0-9_.-]+)*", branch):
        parser.error("--branch: expected a branch or comma-separated list")
    if "all" in branch.split(","):
        if branch != "all":
            parser.error("ALL cannot be combined with other branches")
        branch = "ALL"
    args.branch = branch
    states = re.sub(r"\s+", "", ",".join(args.state)).upper()
    if any(not value.strip() for value in args.state):
        parser.error("--state must not be empty")
    args.state = states.split(",") if states else []
    if any(state not in STATE_COLORS for state in args.state):
        parser.error("--state: choose from " + ", ".join(STATE_COLORS))
    if any(not pattern for pattern in args.package):
        parser.error("--package must not be empty")

    if args.watch is not None and args.watch < 5:
        parser.error("--watch interval must be at least 5 seconds")
    args.watch = args.watch or 0
    if args.watch and args.json:
        parser.error("--watch and --json cannot be combined")
    args.since = ""
    if args.days:
        try:
            args.since = (date.today() - timedelta(days=args.days - 1)).isoformat()
        except (OverflowError, ValueError):
            parser.error("--days value is too large")


def request_json(args, url):
    return request_worker("json-url", url, args, "Taskoteka request timed out")


def fetch_task_data(rows, args, status=False):
    pending = deque(
        task for task in rows if not status or task["state"] in task_status.STATES
    )
    active, full, warnings = [], {}, []
    label = "status" if status else "details"
    try:
        while active or pending:
            while pending and len(active) < 4:
                task = pending.popleft()
                worker = start_worker(
                    "task-status" if status else "json-url",
                    (task, task_status.API + str(task["id"]), task_status.TASKS)
                    if status
                    else API + "/tasks/" + str(task["id"]),
                    args,
                )
                worker["task"] = task
                active.append(worker)
            for worker in active[:]:
                task = worker["task"]
                try:
                    if worker["connection"].poll():
                        value = receive_worker(worker)
                        if status:
                            value, warning = value
                            if warning:
                                warnings.append(
                                    f"warning: #{task['id']}: {clean(warning)}."
                                )
                        detail = normalize(value)
                        if (
                            detail["id"] != task["id"]
                            or detail["owner"].lower() != args.maintainer
                            or detail["repo"] != task["repo"]
                        ):
                            raise CheckError("task details do not match summary")
                        full[task["id"]] = dict(task, **detail)
                    elif time.monotonic() - worker["started"] >= args.timeout:
                        raise CheckError(
                            "Task status request timed out"
                            if status
                            else "Taskoteka detail request timed out"
                        )
                    elif not worker["process"].is_alive():
                        raise CheckError("API worker exited without a result")
                    else:
                        continue
                except CheckError as exc:
                    warnings.append(
                        f"warning: #{task['id']}: task {label} unavailable; "
                        f"showing summary data ({clean(exc)})."
                    )
                stop_workers([worker])
                active.remove(worker)
            if active:
                time.sleep(0.05)
    finally:
        stop_workers(active)
    return [full.get(task["id"], task) for task in rows], warnings


def fetch_details(rows, args):
    return fetch_task_data(rows, args)


def fetch_statuses(rows, args):
    return fetch_task_data(rows, args, status=True)


def snapshot(args):
    query = urllib.parse.urlencode(
        {"user": args.maintainer, "repo": args.branch, "state": "ALL"}
    )
    selected = select_tasks(request_json(args, API + "/tasks?" + query), args)
    total = len(selected)
    rows = selected[: args.limit] if args.limit else selected
    warnings = []
    if (args.arch or args.source) and rows:
        rows, warnings = fetch_details(rows, args)
        before = len(rows)
        rows = select_tasks(rows, args)
        if before != len(rows):
            warnings.append(
                "warning: task states changed during the request; rerun for a fresh list."
            )
    if rows:
        rows, status_warnings = fetch_statuses(rows, args)
        warnings.extend(status_warnings)
    return rows, total, warnings


def render_architectures(subtask, color, width=None, indent=""):
    colors = {ArchStatus.SUCCESS: "32", ArchStatus.FAILED: "1;31"}
    parts = []
    for architecture in ARCHITECTURES:
        if parts:
            parts.append(("  ", None))
        parts.append(
            (
                architecture,
                colors.get(arch_status(subtask, architecture), "90"),
            )
        )
    if width is not None:
        return wrapped_parts(parts, width, indent, color=color)
    return "".join(paint(text, code, color) if code else text for text, code in parts)


def render_rows(rows, package_lists, show_arch, color, width):
    lines = []
    tree = ("├─", "└─", "│")
    try:
        "".join(tree).encode(sys.stdout.encoding or "utf-8")
    except UnicodeEncodeError:
        tree = ("|-", "`-", "|")
    joint, end, vertical = tree
    digits = len(str(len(rows)))
    all_rows = [row for entries in package_lists for row in entries]
    number_width = max([1] + [len(number) + 1 for number, _, _ in all_rows])
    prefix_width = number_width + 7
    architecture_width = len(render_architectures(None, False))
    package_width = min(
        max([13] + [len(name) for _, name, _ in all_rows]),
        max(0, width - prefix_width - 3 - architecture_width),
    )
    for index, (task, entries) in enumerate(zip(rows, package_lists), 1):
        if lines:
            lines.append("")
        header = [
            (f"[{index:>{digits}}/{len(rows)}]  ", "90"),
            (str(task["id"]), "1"),
            ("  [" + task["state"] + "]", STATE_COLORS.get(task["state"], "0")),
            ("  " + task_clean(task["repo"]), "36"),
        ]
        if task.get("test_only") is True:
            header.append(("  TEST", "90"))
        failed = task["state"] == "FAILED" and bool(task.get("failed_check"))
        stage = (
            (task.get("failed_check") if failed else task.get("stage"))
            if task["state"] in task_status.STATES
            else None
        )
        if stage:
            header.extend(
                [
                    ("  |  ", "90"),
                    (
                        task_clean(stage),
                        "1;31" if failed else STATE_COLORS.get(task["state"], "36"),
                    ),
                ]
            )
        lines.append(wrapped_parts(header, width, color=color))
        entries = entries or [("", "(no packages)", None)]
        inline = show_arch and (
            all(len(name) <= package_width for _, name, _ in entries)
            or package_width >= 20
        )
        for subindex, (number, name, subtask) in enumerate(entries):
            last = subindex == len(entries) - 1
            connector = "  " + (end if last else joint) + " "
            label = ("#" + number if number else "-").rjust(number_width)
            parts = [(connector, "90"), (label, "36"), ("  ", None)]
            gutter = "   " if last else "  " + vertical
            continuation = gutter.ljust(prefix_width)[: min(prefix_width, width // 2)]
            source = (subtask or {}).get("source") or {}
            url = source.get("url")
            if source:
                name_parts = [
                    (package_name(subtask), None),
                    (" [", "90"),
                    (source["label"], "36", url),
                    ("]", "90"),
                ]
            else:
                name_parts = [(name, None)]
            if inline:
                plain_cells = wrapped_parts([(name, None)], package_width).splitlines()
                cells = wrapped_parts(
                    name_parts, package_width, color=color
                ).splitlines()
                for line_index, (plain, cell) in enumerate(zip(plain_cells, cells)):
                    prefix = (
                        "".join(
                            paint(text, code, color) if code else text
                            for text, code in parts
                        )
                        if line_index == 0
                        else paint(gutter.ljust(prefix_width), "90", color)
                    )
                    line = prefix + cell
                    if line_index == 0:
                        line += " " * (package_width - len(plain))
                        line += paint(" " + vertical + " ", "90", color)
                        line += render_architectures(subtask, color)
                    lines.append(line)
            else:
                lines.append(
                    wrapped_parts(
                        parts + name_parts,
                        width,
                        continuation=continuation,
                        color=color,
                    )
                )
                if show_arch:
                    lines.append(
                        render_architectures(subtask, color, width, continuation)
                    )
            if not last and show_arch and not inline:
                lines.append(paint(gutter[:width].rstrip(), "90", color))
    return lines


def render(rows, args, total, width=None):
    width = max(1, width if width is not None else shutil.get_terminal_size().columns)
    color = color_enabled(args.color, sys.stdout)
    branch = "all branches" if args.branch == "ALL" else args.branch
    listing = f"Listing {len(rows)} tasks: {args.maintainer} / {branch}" + (
        f" ({total} matched)" if total > len(rows) else ""
    )
    lines = [wrapped_parts([(listing, None)], width)]
    if args.watch:
        lines.append(
            wrapped_parts(
                [
                    (
                        f"Updated: {datetime.now():%Y-%m-%d %H:%M:%S}  |  Every {args.watch}s  |  Ctrl+C to stop",
                        None,
                    )
                ],
                width,
            )
        )
    lines.append("")
    if not rows:
        lines.append(
            wrapped_parts([("No tasks match the selected filters.", None)], width)
        )
    else:
        lines.extend(
            render_rows(
                rows,
                [package_rows(t, args.source) for t in rows],
                args.arch,
                color,
                width,
            )
        )
    counts = Counter(t["state"] for t in rows)
    summary = [("Summary: ", None)]
    for state in sorted(counts):
        if len(summary) > 1:
            summary.append(("  |  ", None))
        summary.append((f"{state}={counts[state]}", STATE_COLORS.get(state, "0")))
    if not counts:
        summary.append(("0 tasks", None))
    lines.extend(["", wrapped_parts(summary, width, color=color)])
    if any(t.get("test_only") is True for t in rows):
        lines.append(
            wrapped_parts(
                [("TEST: test-only task; no automatic commit.", "90")],
                width,
                color=color,
            )
        )
    return "\n".join(lines)


def main(args):
    while True:
        try:
            rows, total, warnings = snapshot(args)
        except (CheckError, OSError) as exc:
            print(
                "error: could not retrieve or process tasks: " + clean(exc),
                file=sys.stderr,
                flush=True,
            )
            if not args.watch:
                return 1
            print(
                f"Retrying in {args.watch}s; the previous successful snapshot is unchanged.",
                file=sys.stderr,
                flush=True,
            )
        else:
            if args.json and args.source:
                rows = [with_sources(task) for task in rows]
            if (
                args.watch
                and sys.stdout.isatty()
                and color_enabled(args.color, sys.stdout)
            ):
                print("\033[2J\033[H", end="")
            print(
                json.dumps(rows, ensure_ascii=True, indent=2)
                if args.json
                else render(rows, args, total),
                flush=True,
            )
            for warning in warnings:
                print(warning, file=sys.stderr, flush=True)
            if not args.watch:
                return 1 if warnings else 0
        time.sleep(args.watch)
