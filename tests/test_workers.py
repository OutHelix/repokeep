import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from unittest.mock import Mock, patch

from repokeep import task_status, tasks, updates, workers
from repokeep.common import CheckError
from tests.support import FIXTURES, arguments, fixture


@contextlib.contextmanager
def server():
    requests = []
    detail = fixture("taskoteka-detail")
    summary = next(t for t in fixture("taskoteka-list") if t["id"] == detail["taskid"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            path = urllib.parse.urlparse(self.path).path
            if path == "/slow":
                time.sleep(2)
                data = b"{}"
            elif path == "/tasks":
                data = json.dumps([summary]).encode()
            elif path == "/tasks/435564":
                data = json.dumps(detail).encode()
            elif path == "/progress/435564":
                data = json.dumps(
                    {
                        "task_id": detail["taskid"],
                        "task_owner": detail["owner"],
                        "task_repo": detail["repo"],
                        "task_state": detail["state"],
                        "task_try": detail["try"],
                        "task_iter": detail["iter"],
                        "task_stage": "",
                    }
                ).encode()
            elif path == "/progress/437142":
                data = json.dumps(fixture("altrepo-progress-437142")).encode()
            elif path == "/437142/logs/events.1.1.log":
                data = fixture("girar-check-logs")["437142"]["text"].encode()
            elif path == "/invalid":
                data = b"{broken-json"
            elif path.startswith("/tasks/"):
                self.send_error(404)
                return
            else:
                data = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_port), requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def live(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1].split()[0]
        return state not in ("Z", "X")
    except FileNotFoundError:
        return False


class WorkersTest(unittest.TestCase):
    def assert_process_gone(self, pid):
        deadline = time.monotonic() + 10
        while live(pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(live(pid), "worker descendant survived cleanup")

    def test_api_request_keeps_empty_results(self):
        for value in (None, [], {"packages": []}):
            worker = {"connection": Mock()}
            worker["connection"].poll.return_value = True
            worker["connection"].recv.return_value = (True, value)
            with (
                self.subTest(value=value),
                patch.object(workers, "start_worker", return_value=worker),
                patch.object(workers, "stop_workers") as stop,
            ):
                self.assertEqual(
                    workers.request_worker(
                        "request", None, arguments("updates"), "timeout"
                    ),
                    value,
                )
            stop.assert_called_once_with([worker])

    def test_api_request_cleans_up_on_interrupt_and_error(self):
        for error in (KeyboardInterrupt(), CheckError("API unavailable")):
            worker = {"connection": Mock()}
            worker["connection"].poll.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(workers, "start_worker", return_value=worker),
                patch.object(workers, "stop_workers") as stop,
                self.assertRaises(type(error)),
            ):
                workers.request_worker("request", None, arguments("updates"), "timeout")
            stop.assert_called_once_with([worker])

    def test_api_request_timeout_keeps_source_message(self):
        worker = {"connection": Mock(), "started": 0}
        worker["connection"].poll.return_value = False
        with (
            patch.object(workers, "start_worker", return_value=worker),
            patch.object(workers, "stop_workers") as stop,
            patch.object(workers.time, "monotonic", return_value=15),
            self.assertRaisesRegex(CheckError, "ALTRepo request timed out: /packages"),
        ):
            workers.request_worker(
                "request",
                None,
                arguments("updates"),
                "ALTRepo request timed out: /packages",
            )
        stop.assert_called_once_with([worker])

    def test_task_snapshot_through_real_processes_and_local_http(self):
        with (
            server() as (url, requests),
            patch.object(tasks, "API", url),
            patch.object(task_status, "API", url + "/progress/"),
        ):
            rows, total, warnings = tasks.snapshot(
                arguments("tasks", "--arch", "--timeout", "5")
            )
        self.assertEqual(total, 1)
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["subtasks"]["40"]["build"]["x86_64"], "built")
        self.assertEqual(
            requests,
            [
                "/tasks?user=scala&repo=ALL&state=ALL",
                "/tasks/435564",
            ],
        )

    def test_source_details_through_real_processes_and_local_http(self):
        with (
            server() as (url, requests),
            patch.object(tasks, "API", url),
            patch.object(task_status, "API", url + "/progress/"),
        ):
            rows, total, warnings = tasks.snapshot(
                arguments("tasks", "--source", "--timeout", "5")
            )
        self.assertEqual(total, 1)
        self.assertEqual(warnings, [])
        subtask = rows[0]["subtasks"]["40"]
        self.assertEqual(
            tasks.source_info(rows[0], "40", subtask)["label"],
            "python3-module-owlrl.git=7.6.2-alt1",
        )
        self.assertEqual(
            requests,
            [
                "/tasks?user=scala&repo=ALL&state=ALL",
                "/tasks/435564",
            ],
        )

    def test_failed_check_through_real_process_and_local_http(self):
        row = {
            "id": 437142,
            "owner": "majioa",
            "repo": "sisyphus",
            "state": "FAILED",
            "try": 1,
            "iter": 1,
            "subtasks": {},
        }
        with (
            server() as (url, requests),
            patch.object(task_status, "API", url + "/progress/"),
            patch.object(task_status, "TASKS", url + "/"),
        ):
            rows, warnings = tasks.fetch_statuses(
                [row], arguments("tasks", "-m", "majioa", "--timeout", "5")
            )
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["failed_check"], "Noarch check")
        self.assertEqual(requests, ["/progress/437142", "/437142/logs/events.1.1.log"])

    def test_detail_failure_keeps_names_and_rejects_mismatches(self):
        rows = fixture("taskoteka-list")[:1]
        args = arguments("tasks", "--arch", "--timeout", "3")
        with server() as (url, _), patch.object(tasks, "API", url):
            output, warnings = tasks.fetch_details(rows, args)
        self.assertEqual(output, rows)
        self.assertIn("task details unavailable", warnings[0])
        other = dict(fixture("taskoteka-list")[0], id=435564)
        with server() as (url, _), patch.object(tasks, "API", url):
            output, warnings = tasks.fetch_details([dict(other, repo="p11")], args)
        self.assertEqual(output[0]["repo"], "p11")
        self.assertIn("do not match summary", warnings[0])

    def test_request_deadline_invalid_json_and_no_active_children(self):
        import multiprocessing

        initial = {p.pid for p in multiprocessing.active_children()}
        with server() as (url, _):
            with self.assertRaisesRegex(CheckError, "timed out"):
                tasks.request_json(arguments("tasks", "--timeout", "1"), url + "/slow")
            with self.assertRaisesRegex(CheckError, "invalid JSON"):
                tasks.request_json(arguments("tasks"), url + "/invalid")
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, initial)

    def fake_git(self, directory, hanging=False):
        path = Path(directory) / "git"
        if hanging:
            body = dedent(
                r"""
                import os
                import subprocess
                import time

                process = subprocess.Popen(
                    [
                        os.environ["REPOKEEP_TEST_PYTHON"],
                        "-c",
                        "import time\n\ntime.sleep(60)\n",
                    ]
                )
                with open(os.environ["REPOKEEP_TEST_PID"], "w") as marker:
                    marker.write(str(process.pid))
                time.sleep(60)
                """
            ).lstrip()
        else:
            body = dedent(
                r"""
                import json
                import os
                import sys
                import time
                from pathlib import Path

                if "/Alpha.git" in sys.argv[-1]:
                    release = Path(os.environ["REPOKEEP_TEST_RELEASE"])
                    deadline = time.monotonic() + 30
                    while not release.exists():
                        if time.monotonic() >= deadline:
                            raise RuntimeError("parallel test did not release Alpha")
                        time.sleep(0.01)
                with open(os.environ["REPOKEEP_TEST_TAGS"]) as stream:
                    tags = json.load(stream)
                for tag in tags:
                    print("0123456789abcdef\trefs/tags/" + tag)
                """
            ).lstrip()

        launcher = (
            "#!/bin/sh\n'''exec' "
            + shlex.quote(sys.executable)
            + " \"$0\" \"$@\"\n' '''\n"
        )
        path.write_text(launcher + body)
        path.chmod(0o755)

    def test_sorted_and_completion_order_with_real_parallel_workers(self):
        packages = [
            {"name": name, "version": "1.0.0"} for name in ("Alpha", "beta", "Zeta")
        ]
        rules = {p["name"]: {"repo": "owner/" + p["name"]} for p in packages}
        with tempfile.TemporaryDirectory() as directory:
            self.fake_git(directory)
            release = Path(directory) / "release"
            receive = updates.receive_worker

            def receive_and_release(worker, progress=False):
                result = receive(worker, progress=progress)
                if result is not None and result.name != "Alpha":
                    release.touch()
                return result

            with (
                patch.dict(
                    os.environ,
                    {
                        "PATH": directory + os.pathsep + os.environ.get("PATH", ""),
                        "REPOKEEP_TEST_GIT": str(Path(directory) / "git"),
                        "REPOKEEP_TEST_RELEASE": str(release),
                        "REPOKEEP_TEST_TAGS": str(FIXTURES / "github-parallel.json"),
                    },
                ),
                patch.object(
                    updates, "receive_worker", side_effect=receive_and_release
                ),
            ):
                for sorted_output in (False, True):
                    release.unlink(missing_ok=True)
                    args = arguments(
                        "updates",
                        "-j",
                        "3",
                        "--no-progress",
                        "--package-timeout",
                        "5",
                        *(["--sort"] if sorted_output else []),
                    )
                    results = list(updates.package_results(packages, rules, args))
                    self.assertTrue(all(r.status == "UPD" for r in results), results)
                    if sorted_output:
                        self.assertEqual(
                            [r.name for r in results], ["Alpha", "beta", "Zeta"]
                        )
                    else:
                        self.assertNotEqual(results[0].name, "Alpha")
                    self.assertEqual(results[0].newer_majors[0]["version"], "2.0.1")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
    def test_package_timeout_kills_git_and_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fake_git(directory, hanging=True)
            marker = Path(directory) / "helper.pid"
            env = {
                "PATH": directory + os.pathsep + os.environ.get("PATH", ""),
                "REPOKEEP_TEST_PYTHON": sys.executable,
                "REPOKEEP_TEST_PID": str(marker),
                "REPOKEEP_TEST_GIT": str(Path(directory) / "git"),
            }
            args = arguments(
                "updates", "--timeout", "10", "--package-timeout", "2", "--no-progress"
            )
            with patch.dict(os.environ, env):
                result = list(
                    updates.package_results(
                        [{"name": "p", "version": "1.0"}], {"p": {"repo": "o/r"}}, args
                    )
                )[0]
            self.assertEqual(result.status, "ERR")
            self.assertIn("timed out", result.note)
            self.assertTrue(marker.exists())
            self.assert_process_gone(int(marker.read_text()))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
    def test_git_request_timeout_also_kills_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fake_git(directory, hanging=True)
            marker = Path(directory) / "helper.pid"
            with patch.dict(
                os.environ,
                {
                    "PATH": directory + os.pathsep + os.environ.get("PATH", ""),
                    "REPOKEEP_TEST_PYTHON": sys.executable,
                    "REPOKEEP_TEST_PID": str(marker),
                    "REPOKEEP_TEST_GIT": str(Path(directory) / "git"),
                },
            ):
                args = arguments(
                    "updates",
                    "--timeout",
                    "1",
                    "--package-timeout",
                    "5",
                    "--no-progress",
                )
                result = list(
                    updates.package_results(
                        [{"name": "p", "version": "1.0"}], {"p": {"repo": "o/r"}}, args
                    )
                )[0]
            self.assertEqual(result.status, "ERR")
            self.assertIn("GitHub tags request timed out", result.note)
            self.assert_process_gone(int(marker.read_text()))

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
    def check_interrupt_cleanup(self, sent_signal, expected_code):
        with tempfile.TemporaryDirectory() as directory:
            self.fake_git(directory, hanging=True)
            marker = Path(directory) / "helper.pid"
            script = Path(directory) / "interrupt.py"
            script.write_text(
                dedent(
                    """
                    import sys

                    from repokeep import cli, updates

                    if __name__ == "__main__":

                        def run():
                            root, _ = cli.parser()
                            args = root.parse_args(
                                [
                                    "updates",
                                    "-m",
                                    "other",
                                    "--no-progress",
                                    "--package-timeout",
                                    "30",
                                ]
                            )
                            packages = [{"name": "p", "version": "1.0"}]
                            rules = {"p": {"repo": "o/r"}}
                            for result in updates.package_results(packages, rules, args):
                                pass

                        cli.run = run
                        sys.exit(cli.main())
                    """
                ).lstrip()
            )
            env = dict(
                os.environ,
                PATH=directory + os.pathsep + os.environ.get("PATH", ""),
                REPOKEEP_TEST_PYTHON=sys.executable,
                REPOKEEP_TEST_PID=str(marker),
                REPOKEEP_TEST_GIT=str(Path(directory) / "git"),
            )
            process = subprocess.Popen(
                [sys.executable, str(script)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 6
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists())
                process.send_signal(sent_signal)
                out, err = process.communicate(timeout=4)
                self.assertEqual(process.returncode, expected_code, err)
                if sent_signal == signal.SIGINT:
                    self.assertIn("Interrupted", err)
                self.assert_process_gone(int(marker.read_text()))
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=2)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
    def test_ctrl_c_cleans_worker_group(self):
        self.check_interrupt_cleanup(signal.SIGINT, 130)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
    def test_sigterm_cleans_worker_group(self):
        self.check_interrupt_cleanup(signal.SIGTERM, 143)
