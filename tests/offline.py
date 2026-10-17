import os
import shutil
import socket
import sys
from pathlib import Path

INSTALLED = False
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def audit(event, args):
    if (
        event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr")
        and args[0] not in LOOPBACK
    ):
        raise AssertionError("Offline tests blocked DNS lookup: " + str(args[0]))
    if event in ("socket.connect", "socket.sendto"):
        sock, address = args[0], args[-1]
        if (
            sock.family in (socket.AF_INET, socket.AF_INET6)
            and address[0] not in LOOPBACK
        ):
            raise AssertionError(
                "Offline tests blocked network connection: " + str(address)
            )
    if event == "subprocess.Popen":
        executable, _, _, environment = args
        if Path(os.fsdecode(executable)).name == "git":
            environment = os.environ if environment is None else environment
            resolved = shutil.which(
                os.fsdecode(executable), path=environment.get("PATH")
            )
            allowed = environment.get("REPOKEEP_TEST_GIT")
            if not allowed or resolved != allowed:
                raise AssertionError(
                    "Offline tests blocked real git; mock it or set REPOKEEP_TEST_GIT to a test stub"
                )


def install():
    global INSTALLED
    if INSTALLED:
        return
    INSTALLED = True
    root = Path(__file__).resolve().parent.parent
    paths = [str(root / "tests" / "bootstrap"), str(root)]
    paths.extend(os.environ.get("PYTHONPATH", "").split(os.pathsep))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys(path for path in paths if path)
    )
    sys.addaudithook(audit)
