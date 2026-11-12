"""Deadline helper for blocking SDK calls made by read-only tool modules."""

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar


T = TypeVar("T")

VERIFY_TIMEOUT_SECONDS: float = 5.0
TIMEOUT_MESSAGE = "Connection timed out. Check credentials and network."


def run_with_timeout(func: Callable[[], T], timeout: float) -> T:
    """Run ``func`` on a daemon thread and wait at most ``timeout`` seconds.

    SDK calls (credential discovery, HTTP requests) cannot be interrupted, so
    a call that overruns keeps running in the background on a daemon thread
    that will not block interpreter shutdown.

    Raises:
        TimeoutError: if ``func`` has not finished within ``timeout`` seconds.
        Exception: any exception raised by ``func`` is re-raised unchanged.
    """
    future: Future[T] = Future()

    def _runner() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(func())
        except BaseException as error:  # propagate everything to the caller
            future.set_exception(error)

    threading.Thread(target=_runner, name="verify-access", daemon=True).start()
    return future.result(timeout=timeout)
