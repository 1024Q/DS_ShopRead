"""One scheduler owns shop browsers in a data directory, including startup."""

import os
from contextlib import contextmanager
from pathlib import Path

_owned_directories = set()


def directory_key(directory):
    value = Path(directory).resolve().as_posix()
    if os.name == "nt":
        if value[:8].casefold() == "//?/unc/":
            value = "//" + value[8:]
        elif value.startswith("//?/"):
            value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def owns(directory):
    """Only this process's live scheduler lease permits stale reader cleanup."""
    return directory_key(directory) in _owned_directories


@contextmanager
def exclusive(directory):
    handle = open(directory / "collector.lock", "a+b")
    try:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def ownership(store):
    with exclusive(store.directory):
        # Startup owns the whole collector; each idempotent repair commits only
        # its own shop. Never pretend that several SQLite files form one transaction.
        for shop in store.list():
            with store.shop_db(shop["id"], write=True) as db:
                db.execute("UPDATE collection_tasks SET lease_until=0 WHERE shop_id=? AND lease_until>0", (shop["id"],))
        directory = directory_key(store.directory)
        _owned_directories.add(directory)
        try:
            yield
        finally:
            _owned_directories.discard(directory)
