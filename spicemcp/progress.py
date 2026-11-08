"""Progress and time limits for long calls, without threading them through every call.

A long tool runs its work on a worker thread. That thread installs a `Tracker`; the
engine calls `report()` after each simulation (or batch) and `out_of_time()` before
starting more. With no tracker installed (tests, scripts) both are no-ops.
"""
from __future__ import annotations

import threading
import time

_local = threading.local()


class Tracker:
    def __init__(self, budget_s: float | None = None):
        self.deadline = time.monotonic() + budget_s if budget_s else None
        self.done, self.total, self.message = 0, None, ""
        self.lock = threading.Lock()

    def update(self, done=None, total=None, message=None):
        with self.lock:
            if done is not None:
                self.done = done
            if total is not None:
                self.total = total
            if message is not None:
                self.message = message

    def snapshot(self) -> dict:
        with self.lock:
            return {"done": self.done, "total": self.total, "message": self.message}


def install(t: Tracker | None) -> None:
    _local.tracker = t


def current() -> Tracker | None:
    return getattr(_local, "tracker", None)


def report(done=None, total=None, message=None) -> None:
    t = current()
    if t:
        t.update(done, total, message)


def add(n: int = 1, message=None) -> None:
    t = current()
    if t:
        with t.lock:
            t.done += n
            if message is not None:
                t.message = message


def out_of_time() -> bool:
    t = current()
    return bool(t and t.deadline is not None and time.monotonic() >= t.deadline)
