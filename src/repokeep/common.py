import argparse
import os
import re

from . import __version__


class CheckError(Exception):
    pass


def maintainer_login(value):
    value = value.strip().lower()
    if value == "all" or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("expected a specific ALT maintainer login")
    return value


def output_arguments(parser, json_help, *, defaults=True):
    group = parser.add_argument_group("Output options")
    group.add_argument(
        "--json",
        action="store_true",
        default=False if defaults else argparse.SUPPRESS,
        help=json_help,
    )
    group.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto" if defaults else argparse.SUPPRESS,
        help="execution colors (default: auto; respects NO_COLOR; help is always plain)",
    )
    group.add_argument(
        "--no-color",
        dest="color",
        action="store_const",
        const="never",
        default=argparse.SUPPRESS,
        help="disable execution colors",
    )
    group.add_argument("--version", action="version", version="%(prog)s " + __version__)


def clean(text):
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text)).split())


def positive(value):
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer")


def color_enabled(mode, stream):
    if mode != "auto":
        return mode == "always"
    return (
        stream.isatty()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
    )


def paint(text, code, enabled):
    return "\033[" + code + "m" + text + "\033[0m" if enabled else text


def link(text, url, enabled):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\" if enabled and url else text
