import json
import multiprocessing
import os
import signal
import time

from .common import CheckError, clean
from .upstream import Client
from .watch import check_quit


def worker_entry(connection, action, payload, args):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(os, "setsid"):
        os.setsid()
    try:
        client = Client(args.timeout)
        if action == "request":
            endpoint, parameters = payload
            value = client.json(endpoint, **parameters)
        elif action == "json-url":
            try:
                value = json.loads(client.request(payload))
            except ValueError:
                raise CheckError("invalid JSON from Taskoteka API") from None
        elif action == "task-status":
            from .task_status import load_status

            value = load_status(client, *payload)
        else:
            from .osv import check_vulnerable_package
            from .updates import check_package

            package, rule = payload
            if args.vulnerable_only:
                value = check_vulnerable_package(
                    package,
                    rule,
                    client,
                    args,
                    progress=lambda result: connection.send(("progress", result)),
                )
            else:
                value = check_package(package, rule, client, args)
        connection.send((True, value))
    except Exception as exc:
        connection.send((False, clean(str(exc))))
    finally:
        connection.close()


def start_worker(action, payload, args):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=worker_entry, args=(sender, action, payload, args), daemon=True
    )
    try:
        process.start()
    except BaseException:
        receiver.close()
        sender.close()
        if process.pid:
            process.kill()
            process.join(timeout=1)
        raise
    sender.close()
    return {"process": process, "connection": receiver, "started": time.monotonic()}


def stop_worker(worker):
    process = worker["process"]
    if hasattr(os, "killpg"):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
    elif process.is_alive():
        process.kill()
    process.join(timeout=1)
    worker["connection"].close()
    if not process.is_alive():
        process.close()


def stop_workers(workers):
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for worker in workers:
            stop_worker(worker)
    finally:
        signal.signal(signal.SIGINT, previous)


def receive_worker(worker, progress=False):
    try:
        success, value = worker["connection"].recv()
    except (EOFError, OSError):
        raise CheckError("worker exited without a result") from None
    if not success:
        raise CheckError(value)
    if success == "progress":
        if not progress:
            raise CheckError("unexpected progress from API worker")
        worker["partial"] = value
        return None
    return value


def request_worker(action, payload, args, timeout_message):
    worker = start_worker(action, payload, args)
    try:
        while not worker["connection"].poll(0.05):
            check_quit(getattr(args, "watch_fd", None))
            if time.monotonic() - worker["started"] >= args.timeout:
                raise CheckError(timeout_message)
            if not worker["process"].is_alive():
                raise CheckError("API worker exited without a result")
        return receive_worker(worker)
    finally:
        stop_workers([worker])
