"""Shared error journal; original logs remain authoritative if the extra copy fails."""

import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def append(folder, text, now=None):
    # Launcher stdout and business events live in separate processes. Serialize
    # rotation as well as append, without holding collection/database locks.
    if not _lock.acquire(timeout=0.2):
        return
    try:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / "errors.lock").open("a+b") as guard:
            guard.seek(0)
            if os.fstat(guard.fileno()).st_size == 0:
                guard.write(b"0")
                guard.flush()
            deadline = time.monotonic() + 0.2
            while True:
                try:
                    guard.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        return
                    time.sleep(0.01)
            try:
                from .collector_logs import DailyLog

                DailyLog(folder, "errors").write(text, now)
            finally:
                guard.seek(0)
                if os.name == "nt":
                    msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # Never turn an auxiliary log failure into a failed task.
    finally:
        _lock.release()
