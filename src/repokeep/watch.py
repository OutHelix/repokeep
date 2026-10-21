import os
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager


class WatchQuit(Exception):
    pass


@contextmanager
def watch_input(enabled):
    if not enabled or sys.stdin is None or not sys.stdin.isatty():
        yield None
        return
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor, termios.TCSANOW)
        yield descriptor
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, previous)


def check_quit(descriptor, timeout=0):
    if descriptor is None or not select.select([descriptor], [], [], timeout)[0]:
        return
    keys = os.read(descriptor, 1024)
    if not keys or b"q" in keys.lower():
        raise WatchQuit


def wait(seconds, descriptor):
    if descriptor is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        check_quit(descriptor, remaining)
