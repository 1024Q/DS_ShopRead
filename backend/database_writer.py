"""One lazy write admission gate per database path; committed WAL reads stay free."""

import contextvars, os, re, sqlite3, threading, time, logging
from collections import deque
from pathlib import Path
from contextlib import contextmanager

operation = contextvars.ContextVar("database_operation", default="数据库更新")
_registry = {}
_registry_lock = threading.Lock()


def queue_for(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _registry_lock:
        return _registry.setdefault(key, WriterQueue(key))


class WriterQueue:
    def __init__(self, path=None):
        self.condition = threading.Condition()
        self.waiters = deque()
        self.owner = None
        self.depth = 0
        self.path = path
        self.file = None
        self.started = 0
        self.label = ""
        self.completed = 0
        self.timeouts = 0
        self.last_ms = 0
        self.external = False

    def acquire(self, timeout=60):
        owner = threading.get_ident()
        deadline = time.monotonic() + timeout
        with self.condition:
            if self.owner == owner:
                self.depth += 1
                return True
            ticket = (object(), time.monotonic())
            self.waiters.append(ticket)
            while self.owner is not None or self.waiters[0] is not ticket:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.waiters.remove(ticket)
                    self.timeouts += 1
                    self.condition.notify_all()
                    return False
                self.condition.wait(remaining)
            self.waiters.popleft()
            self.owner = owner
            self.depth = 1
            self.started = time.monotonic()
            self.label = operation.get()
        try:
            if self.path:
                self.file = open(self.path + ".writer.lock", "a+b")
                self.file.seek(0)
                if os.fstat(self.file.fileno()).st_size == 0:
                    self.file.write(b"0")
                    self.file.flush()
                while True:
                    try:
                        self.file.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self.external = False
                        break
                    except OSError:
                        self.external = True
                        if time.monotonic() >= deadline:
                            self.timeouts += 1
                            self.release()
                            return False
                        time.sleep(0.05)
            return True
        except BaseException:
            self.release()
            raise

    def release(self):
        with self.condition:
            if self.owner != threading.get_ident():
                raise RuntimeError("database writer released by non-owner")
            self.depth -= 1
            if self.depth:
                return
            elapsed = time.monotonic() - self.started
            if self.file:
                self.file.close()
                self.file = None
            self.last_ms = round(elapsed * 1000)
            self.completed += 1
            label = self.label
            self.owner = None
            self.external = False
            self.label = ""
            self.condition.notify_all()
        if elapsed >= 2:
            logging.getLogger(__name__).warning("数据库慢写入 · %s · %.2f 秒（含锁等待）", label, elapsed)

    def status(self):
        with self.condition:
            oldest = time.monotonic() - self.waiters[0][1] if self.waiters else 0
            held = time.monotonic() - self.started if self.owner is not None else 0
            pressure = len(self.waiters) >= 4 or oldest >= 2 or held >= 5
            return dict(
                pending=len(self.waiters),
                oldest_seconds=round(oldest, 1),
                active=self.owner is not None,
                current=self.label,
                active_seconds=round(held, 1),
                external_wait=self.external,
                completed=self.completed,
                timeouts=self.timeouts,
                last_ms=self.last_ms,
                pressure=pressure,
            )


class Cursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection.admit(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        self.connection.admit(sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql):
        self.connection.admit("SCRIPT")
        return super().executescript(sql)


class Connection(sqlite3.Connection):
    def admit(self, sql):
        # Unknown SQL (including WITH and PRAGMA) is conservatively admitted.
        text = re.sub(r"^(?:\s|--[^\n]*\n|/\*.*?\*/)+", "", sql, flags=re.S).upper()
        if text.startswith(("SELECT", "EXPLAIN", "ROLLBACK", "COMMIT", "END", "ATTACH", "DETACH")):
            return
        if re.match(r"PRAGMA\s+(?:\w+\.)?(?:TABLE_INFO|TABLE_XINFO|INDEX_INFO|INDEX_LIST)\s*\(", text):
            return
        if text.startswith("WITH") and not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", text):
            return
        if getattr(self, "read_only", False):
            raise sqlite3.OperationalError("attempt to write through explicit read-only connection")
        if not self.admitted:
            if not self.gate.acquire(self.wait_timeout):
                raise sqlite3.OperationalError("database writer is busy")
            self.admitted = True

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql):
        return self.cursor().executescript(sql)

    def cursor(self, factory=Cursor):
        return super().cursor(factory)

    def close(self):
        try:
            super().close()
        finally:
            if self.admitted:
                self.admitted = False
                self.gate.release()


@contextmanager
def connection(path, timeout=60, immediate=False, read_only=False):
    if immediate and read_only:
        raise ValueError("只读连接不能开启写事务")
    db = sqlite3.connect(path, timeout=5, factory=Connection, uri=True)
    db.gate = queue_for(path)
    db.admitted = False
    db.wait_timeout = timeout
    db.row_factory = sqlite3.Row
    db.read_only = read_only
    if read_only:
        # Connection-local safety only; bypass admission because this does not
        # write the database and must never wait behind a business writer.
        sqlite3.Connection.execute(db, "PRAGMA query_only=ON")
    try:
        with db:
            if immediate:
                db.admit("BEGIN")
                # Retry lock acquisition only, before any SQL transaction work.
                for attempt in range(3):
                    try:
                        db.execute("BEGIN IMMEDIATE")
                        break
                    except sqlite3.OperationalError as e:
                        if not any(w in str(e).lower() for w in ("locked", "busy")) or attempt == 2:
                            raise
                        time.sleep(0.1 * (attempt + 1))
            yield db
    finally:
        db.close()
