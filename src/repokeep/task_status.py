import json
import re
import time

from .common import CheckError

API = "https://rdb.altlinux.org/api/task/progress/task_info/"
TASKS = "https://git.altlinux.org/tasks/"
STATES = ("BUILDING", "FAILED")
STAGES = {
    "task-build": "Task build",
    "task-check-build": "Build check",
    "task-check-girar": "Gears inheritance check",
    "task-check-noarch": "Noarch check",
    "task-check-lastchange": "SRPM inheritance check",
    "task-check-neighbour-version": "Neighbour version check",
    "task-check-install": "Install check",
    "task-repo-plan": "Duplicate provides check",
    "task-repo-elfsym": "ELF symbols check",
    "task-gen-next-repo": "Created next repo",
    "task-gen-hash": "Created hash files",
    "task-repo-unmets": "Dependencies check",
    "task-save-repo": "Preparing the repository",
}


def stage_label(stage):
    return STAGES.get(stage, stage)


def failed_check(log):
    failures = {}
    for line in log.splitlines():
        if re.fullmatch(
            r"\d{4}-[A-Za-z]{3}-\d{2} \d{2}:\d{2}:\d{2} :: "
            r"(?:test-only )?task #\d+ for \S+ "
            r"(?:(?:started|resumed) by .+|TESTED|EPERM|DONE)",
            line,
        ):
            failures.clear()
            continue
        match = re.fullmatch(
            r"\d{4}-[A-Za-z]{3}-\d{2} \d{2}:\d{2}:\d{2} :: "
            r"(.+?\bcheck) (FAILED|OK|IGNORED|COND-OK)\b.*",
            line,
        )
        if not match:
            continue
        name, status = match.groups()
        failures.pop(name, None)
        if status == "FAILED":
            failures[name] = None
    if not failures:
        return None
    name = next(reversed(failures))
    return name[0].upper() + name[1:]


def current_subtasks(task, progress):
    entries = progress.get("subtasks")
    if not isinstance(entries, list):
        raise CheckError("invalid ALTRepo progress subtasks")
    builds = {}
    original = task.get("subtasks") or {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or type(entry.get("subtask_id")) is not int
            or entry["subtask_id"] < 1
        ):
            raise CheckError("invalid ALTRepo progress subtask")
        number = str(entry["subtask_id"])
        if number in builds or not isinstance(entry.get("archs"), list):
            raise CheckError("invalid ALTRepo progress architectures")
        build = {}
        previous = original.get(number) or {}
        previous = previous.get("build") if isinstance(previous, dict) else None
        previous = previous if isinstance(previous, dict) else {}
        for item in entry["archs"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("arch"), str)
                or not item["arch"]
                or item["arch"] in build
                or not isinstance(item.get("stage_status"), str)
            ):
                raise CheckError("invalid ALTRepo progress architecture")
            status = item["stage_status"]
            if status == "processed":
                status = previous.get(item["arch"])
                if status not in ("built", "cached"):
                    status = "built"
            elif status == "started":
                status = "building"
            build[item["arch"]] = status
        builds[number] = build
    return {
        number: dict(subtask, build=builds.get(str(number), {}))
        if isinstance(subtask, dict)
        and any(subtask.get(key) for key in ("type", "pkgname", "dir", "srpm"))
        else subtask
        for number, subtask in original.items()
    }


def load_status(client, task, progress_url=None, logs_base=TASKS):
    result = dict(task, stage=None, failed_check=None)
    if task["state"] not in STATES:
        return result, None
    client.deadline = time.monotonic() + client.timeout
    try:
        progress = json.loads(client.request(progress_url or API + str(task["id"])))
    except ValueError:
        raise CheckError("invalid JSON from ALTRepo task progress") from None
    if not isinstance(progress, dict):
        raise CheckError("invalid ALTRepo task progress object")
    if (
        type(progress.get("task_id")) is not int
        or progress["task_id"] != task["id"]
        or progress.get("task_owner") != task["owner"]
        or progress.get("task_repo") != task["repo"]
    ):
        raise CheckError("task progress does not match Taskoteka data")
    if not isinstance(progress.get("task_stage"), str) or not isinstance(
        progress.get("task_state"), str
    ):
        raise CheckError("invalid task progress stage/state")
    if progress["task_state"] != task["state"] or any(
        type(task.get(key)) is not int
        or type(progress.get("task_" + key)) is not int
        or task[key] != progress["task_" + key]
        for key in ("try", "iter")
    ):
        return result, None
    result["subtasks"] = current_subtasks(task, progress)
    result["stage"] = stage_label(progress["task_stage"]) or None
    attempt, iteration = progress.get("task_try"), progress.get("task_iter")
    if task["state"] != "FAILED" or any(
        type(value) is not int or value < 1 for value in (attempt, iteration)
    ):
        return result, None
    url = logs_base + f"{task['id']}/logs/events.{attempt}.{iteration}.log"
    try:
        result["failed_check"] = failed_check(client.request(url))
    except CheckError as exc:
        return result, "check log unavailable: " + str(exc)
    return result, None
