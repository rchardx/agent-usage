"""Atomic snapshots, cooperative index locking and safe storage diagnostics."""

import errno
import json
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


class CacheError(Exception):
    """A safe, actionable cache failure without raw database or transcript content."""


REBUILD_HINT = (
    "Run 'doctor --rebuild-index' with the same path options, or choose a new --cache-dir."
)


def cache_error_message(exc: Exception) -> str:
    if isinstance(exc, CacheError):
        return str(exc)
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return "Usage index is busy. Close other agent-usage processes and retry."
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return f"Usage index is damaged. {REBUILD_HINT}"
    if code == sqlite3.SQLITE_FULL or getattr(exc, "errno", None) in (
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    ):
        return "Cannot write the usage index: disk is full. Free space and retry."
    if isinstance(exc, OSError) or code in (
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
    ):
        return (
            "Cannot access the usage index. Check cache directory permissions and storage, "
            "or choose a writable --cache-dir."
        )
    return f"Usage index could not be read or updated. {REBUILD_HINT}"


@contextmanager
def index_lock(cache: Path):
    """Serialize this tool's index readers, writers and rebuilds without stale PID locks.

    The lock file must never be unlinked: all processes must lock the same inode.
    Closing the descriptor (including on process exit) releases the OS lock.
    """
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = cache / "index.lock"
    if path.is_symlink():
        raise CacheError("Index lock is a symbolic link; choose a different --cache-dir.")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CacheError("Index lock is not a regular file; choose a different --cache-dir.")
        try:
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise CacheError(
                    "Usage index is busy. Close other agent-usage processes and retry."
                ) from exc
            raise
        yield
    finally:
        os.close(fd)


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
