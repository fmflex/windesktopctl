"""Single dedicated worker thread for ALL core ops.

UI Automation needs COM initialized on the thread that uses it, so every
core operation (capture, UIA, OCR, input, window enumeration) is submitted
to this one-thread executor. MCP tools and the CLI both go through here.
"""

import concurrent.futures
import threading

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _init_worker() -> None:
    try:
        import comtypes

        comtypes.CoInitializeEx()
    except Exception:
        pass


def _get_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _lock:
            if _executor is None:
                _executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="windesktopctl-core",
                    initializer=_init_worker,
                )
    return _executor


def submit(fn, *args, **kwargs):
    """Submit fn(*args, **kwargs) to the core worker; returns a Future."""
    return _get_executor().submit(fn, *args, **kwargs)


def run(fn, *args, **kwargs):
    """Submit and block for the result (re-raises worker exceptions)."""
    return submit(fn, *args, **kwargs).result()
