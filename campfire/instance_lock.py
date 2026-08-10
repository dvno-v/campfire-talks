"""A small Unix lock that separates online snapshots from offline restore."""

import contextlib
import os

try:
    import fcntl
except ImportError:  # pragma: no cover - production deployment is Linux
    fcntl = None


class InstanceBusy(RuntimeError):
    """The requested offline operation overlaps a running Campfire process."""


@contextlib.contextmanager
def _file_lock(lock_path, exclusive, blocking):
    if fcntl is None:
        if exclusive:
            raise InstanceBusy("Process locking is unavailable on this platform")
        yield
        return
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as failure:
            raise InstanceBusy(
                "Campfire is running or another data operation is in progress") from failure
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def operation_lock(database_path, exclusive=False, blocking=True):
    """Share the lock for serving/backups; take it exclusively for restore."""
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = database_path.with_name(database_path.name + ".operations.lock")
    with _file_lock(lock_path, exclusive=exclusive, blocking=blocking):
        yield


@contextlib.contextmanager
def server_lock(database_path):
    """Refuse a second server process for the single-process architecture."""
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = database_path.with_name(database_path.name + ".server.lock")
    with _file_lock(lock_path, exclusive=True, blocking=False):
        yield
