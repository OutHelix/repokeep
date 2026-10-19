import contextlib
import io
import json
import os
import re
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from repokeep import tasks, updates
from repokeep.cli import parser
from repokeep.common import CheckError

FIXTURES = Path(__file__).parent / "fixtures"
FIXED_DATE = date(2026, 10, 17)


def plain_text(value):
    return re.sub(r"\033(?:\[[0-9;]*m|\]8;;[^\033]*\033\\)", "", value)


@contextlib.contextmanager
def frozen_time(instant=datetime(2026, 10, 17, 12, tzinfo=timezone.utc), tz="UTC"):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return datetime.fromtimestamp(instant.timestamp()).date()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(instant.timestamp(), tz)

    try:
        with patch.dict(os.environ, {"TZ": tz}):
            time.tzset()
            with (
                patch.object(tasks, "date", FrozenDate),
                patch.object(tasks, "datetime", FrozenDatetime),
            ):
                yield
    finally:
        time.tzset()


def fixture(name):
    return json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))


def arguments(command, *options):
    root, parsers = parser()
    args = root.parse_args([command, "-m", "scala", *options])
    (updates if command == "update" else tasks).validate_args(args, parsers[command])
    return args


@contextlib.contextmanager
def streams():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def progress_task(progress):
    return {
        "id": progress["task_id"],
        "owner": progress["task_owner"],
        "repo": progress["task_repo"],
        "state": progress["task_state"],
        "try": progress["task_try"],
        "iter": progress["task_iter"],
        "test_only": bool(progress["task_testonly"]),
        "subtasks": {
            "100": {
                "type": "repo",
                "pkgname": "example",
                "dir": "/people/maintainer/packages/example.git",
                "tag_name": "1.0-alt1",
                "build": {arch: "built" for arch in tasks.ARCHITECTURES},
            }
        },
    }


class Schedule:
    def __init__(self, order, results):
        self.order = deque(order)
        self.results = results
        self.active = set()
        self.started = []
        self.closed = []
        self.peak = 0
        self.polls = 0

    @contextlib.contextmanager
    def patch_workers(self, module):
        with (
            patch.object(module, "start_worker", side_effect=self.start),
            patch.object(module, "receive_worker", side_effect=self.receive),
            patch.object(module, "stop_workers", side_effect=self.stop),
            patch.object(module.time, "monotonic", return_value=0),
            patch.object(module.time, "sleep"),
        ):
            yield

    def start(self, action, payload, args):
        if action == "package":
            key = payload[0]["name"]
        elif action == "task-status":
            key = payload[0]["id"]
        else:
            key = int(payload.rsplit("/", 1)[1])
        self.active.add(key)
        self.started.append(key)
        self.peak = max(self.peak, len(self.active))

        def poll():
            self.polls += 1
            if self.polls > 100:
                raise AssertionError(
                    "scheduler did not consume controlled completion order"
                )
            return bool(self.order) and self.order[0] == key

        return {
            "key": key,
            "started": 0,
            "connection": Mock(poll=poll),
            "process": Mock(is_alive=lambda: True),
        }

    def receive(self, worker, progress=False):
        key = self.order.popleft()
        if key != worker["key"]:
            raise AssertionError("unexpected completion")
        result = self.results[key]
        if isinstance(result, CheckError):
            raise result
        return result

    def stop(self, workers):
        for worker in workers:
            key = worker["key"]
            self.active.remove(key)
            self.closed.append(key)
