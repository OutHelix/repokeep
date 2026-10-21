import io
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import urllib.parse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from repokeep import watch
from tests.support import FIXTURES, arguments, fixture, live


@contextmanager
def terminal_process(url=None):
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "watch_cli.py"
        script.write_text(
            dedent(
                f"""\
                import json
                import sys
                from pathlib import Path
                from repokeep import cli, tasks

                if __name__ == "__main__":
                    url = {url!r}
                    if url:
                        tasks.API = url
                    else:
                        row = tasks.normalize(json.loads(Path(
                            {str(FIXTURES / "taskoteka-approval-comments.json")!r}
                        ).read_text()))
                        tasks.snapshot = lambda args: ([row], 1, [])
                    sys.exit(cli.main())
                """
            )
        )
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(script),
                "task",
                "-m",
                "scala",
                "--all",
                "-w",
                "3",
                "--no-color",
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            yield process, master, slave, original
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            os.close(master)
            os.close(slave)


def wait_output(process, marker):
    data = b""
    deadline = time.monotonic() + 10
    while marker not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise AssertionError(
                "watch did not produce the expected output: " + repr(data)
            )
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise AssertionError("watch exited early: " + repr(process.stderr.read()))
        data += chunk
    return data


@contextmanager
def blocked_api(phase):
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if (phase == "list" and path == "/tasks") or (
                phase == "detail" and path == "/tasks/435564"
            ):
                started.set()
                release.wait(30)
            value = (
                [next(row for row in fixture("taskoteka-list") if row["id"] == 435564)]
                if path == "/tasks"
                else fixture("taskoteka-detail")
            )
            data = json.dumps(value).encode()
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), started
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class WatchTest(unittest.TestCase):
    def test_watch_defaults_and_intervals_start_at_three_seconds(self):
        for flags, expected in (
            ([], 0),
            (["-w"], 30),
            (["-w", "3"], 3),
            (["-w", "4"], 4),
        ):
            with self.subTest(flags=flags):
                self.assertEqual(arguments("task", *flags).watch, expected)

    def test_noninteractive_input_is_not_consumed(self):
        source = io.StringIO("q\n")
        with (
            patch.object(watch.sys, "stdin", source),
            patch.object(watch.time, "sleep") as sleep,
        ):
            with watch.watch_input(True) as descriptor:
                self.assertIsNone(descriptor)
                watch.wait(3, descriptor)
            sleep.assert_called_once_with(3)
            self.assertEqual(source.read(), "q\n")

    def test_terminal_is_restored_after_exception(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        try:
            with (
                os.fdopen(os.dup(slave)) as source,
                patch.object(watch.sys, "stdin", source),
            ):
                with self.assertRaisesRegex(RuntimeError, "failure"):
                    with watch.watch_input(True):
                        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ICANON)
                        raise RuntimeError("failure")
            self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)

    def test_q_and_signals_restore_terminal_and_return_correct_exit_codes(self):
        for control, expected in (
            (b"q", 0),
            (b"Q", 0),
            (signal.SIGINT, 130),
            (signal.SIGTERM, 143),
        ):
            with (
                self.subTest(control=control),
                terminal_process() as (process, master, slave, original),
            ):
                text = wait_output(process, b"Every 3s")
                self.assertIn(b"q or Ctrl+C to stop", text)
                self.assertFalse(
                    termios.tcgetattr(slave)[3] & (termios.ECHO | termios.ICANON)
                )
                if isinstance(control, bytes):
                    os.write(master, control)
                else:
                    process.send_signal(control)
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, expected, error)
                self.assertEqual(termios.tcgetattr(slave), original)
                if expected == 130:
                    self.assertIn(b"Interrupted", error)
                else:
                    self.assertEqual(error, b"")

    def test_q_during_api_requests_terminates_workers(self):
        for phase in ("list", "detail"):
            with (
                self.subTest(phase=phase),
                blocked_api(phase) as (url, started),
                terminal_process(url) as (process, master, slave, original),
            ):
                self.assertTrue(started.wait(timeout=10), "API request did not start")
                children = (
                    Path(f"/proc/{process.pid}/task/{process.pid}/children")
                    .read_text()
                    .split()
                )
                self.assertTrue(children)
                os.write(master, b"q")
                _, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(error, b"")
                self.assertEqual(termios.tcgetattr(slave), original)
                deadline = time.monotonic() + 5
                while (
                    any(live(int(pid)) for pid in children)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.01)
                self.assertFalse(any(live(int(pid)) for pid in children))
