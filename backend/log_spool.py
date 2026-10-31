"""File-only daily event journal with seven-day retention; no database ingestion."""

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ZONE = timezone(timedelta(hours=8))
_instances = {}
_lock = threading.Lock()


def get(store):
    key = str(store.path.resolve())
    with _lock:
        if key not in _instances:
            _instances[key] = Spool(store.path.parent / "log-spool")
        return _instances[key]


def reason(error):
    code = getattr(error, "sqlite_errorname", "")
    text = str(error).lower()
    if "full" in text or getattr(error, "errno", None) == 28:
        return "磁盘空间不足，请释放采集节点磁盘空间"
    if "readonly" in text or "permission" in text or getattr(error, "errno", None) == 13:
        return "文件或数据库无写入权限，请检查采集节点目录权限"
    if "disk i/o" in text:
        return "磁盘读写异常，请检查磁盘及文件系统"
    from .audit_store import clean_message

    return f"{type(error).__name__}" + (f"（{code}）" if code else "") + "：" + clean_message(error)


class Spool:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.writer = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.inventory = {}
        self.last_error = ""
        self.state = {}
        self.segment = 0

    def append(self, record):
        raw = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        day = datetime.fromtimestamp(record["created"], ZONE).date().isoformat()
        with self.lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                path = self.folder / f"{day}-{self.writer}-{self.segment:05d}.jsonl"
                if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
                    self.segment += 1
                    path = self.folder / f"{day}-{self.writer}-{self.segment:05d}.jsonl"
                with path.open("ab") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.last_error = ""
                return True
            except OSError as error:
                self.last_error = "日志文件未保存：" + reason(error)
                logging.getLogger(__name__).error(
                    "%s · [%s] shop=%s %s",
                    self.last_error,
                    record.get("module"),
                    record.get("shop_id"),
                    record.get("message"),
                )
                return False

    def files(self):
        return sorted(self.folder.glob("????-??-??-*.jsonl"))

    def status(self):
        return {**self.state, **({"error": self.last_error} if self.last_error else {})}

    def flush(self, store=None, limit=200, now=None):
        """Compatibility entry point: maintain files only, never write to SQLite."""
        now = time.time() if now is None else now
        cutoff = (datetime.fromtimestamp(now, ZONE).date() - timedelta(days=6)).isoformat()
        error = ""
        with self.lock:
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                for path in self.files():
                    if path.name[:10] < cutoff:
                        path.unlink()
                        self.inventory.pop(path.name, None)
            except OSError as failure:
                error = reason(failure)
            self.state = {"storage": "files", "retention_days": 7, "pending": 0, "error": error, "updated": now}
            try:
                self.write_status(self.folder / "status.json", self.status())
            except OSError as failure:
                self.last_error = "日志状态未保存：" + reason(failure)
        return self.status()

    @staticmethod
    def write_status(path, value):
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)


def status(store):
    spool = get(store)
    if spool.state or spool.last_error:
        return spool.status()
    return {"storage": "files", "retention_days": 7, "pending": 0, "error": spool.last_error}


async def run(store):
    while True:
        try:
            await asyncio.to_thread(get(store).flush)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("日志文件清理失败，下轮重试")
        await asyncio.sleep(300)
