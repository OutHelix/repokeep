import argparse
import multiprocessing
import os
import signal
import sys

from . import __version__, tasks, updates


def parser():
    root = argparse.ArgumentParser(
        prog="repokeep", description="ALT Linux maintainer tools."
    )
    if hasattr(root, "color"):
        root.color = False
    root.add_argument("--version", action="version", version="%(prog)s " + __version__)
    commands = root.add_subparsers(dest="command", required=True)
    update_parser = commands.add_parser(
        "updates", help="check GitHub releases or OSV advisories"
    )
    updates.configure_parser(update_parser)
    task_parser = commands.add_parser(
        "tasks", help="list Girar tasks owned by a maintainer"
    )
    tasks.configure_parser(task_parser)
    return root, {"updates": update_parser, "tasks": task_parser}


def run(argv=None):
    root, commands = parser()
    args = root.parse_args(argv)
    selected = commands[args.command]
    if args.command == "updates":
        updates.validate_args(args, selected)
        return updates.main(args, selected)
    tasks.validate_args(args, selected)
    return tasks.main(args)


def main():
    multiprocessing.freeze_support()

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        return run()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        os._exit(0)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
