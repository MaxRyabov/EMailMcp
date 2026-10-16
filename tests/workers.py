"""Test targets for attachments.run(): the real serve() with a test handler.

They run in a spawn child, which imports this module by name from tests/ (the
parent's sys.path goes to the child), so it imports nothing but the worker.
"""

import os
import sys
import time

from imap_mcp import _extract_worker


def _sleep(job):
    time.sleep(job.get("seconds", 60))
    return {"text": "woke up"}


def _allocate(job):
    block = bytearray(job["bytes"])
    return {"text": str(len(block))}


def _print(job):
    print("stdout print from the worker")
    print("stderr print from the worker", file=sys.stderr)
    os.write(1, b"fd 1 write from the worker\n")
    os.write(2, b"fd 2 write from the worker\n")
    return {"text": "printed"}


def _modules(job):
    return {"text": "\n".join(sorted(sys.modules))}


def _exit(job):
    os._exit(3)


def _pid(job):
    return {"text": str(os.getpid())}


def sleepy(conn):
    _extract_worker.serve(conn, _sleep)


def hungry(conn):
    _extract_worker.serve(conn, _allocate)


def noisy(conn):
    _extract_worker.serve(conn, _print)


def modules(conn):
    _extract_worker.serve(conn, _modules)


def crashing(conn):
    _extract_worker.serve(conn, _exit)


def pid(conn):
    _extract_worker.serve(conn, _pid)


def deaf(conn):
    """Never reads the job: the parent's send of a large job blocks."""
    time.sleep(60)
