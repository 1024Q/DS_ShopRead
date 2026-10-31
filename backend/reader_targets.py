"""Persist exact collector-owned tabs so a later collector can reclaim them.

Browser and process UUIDs, rather than URLs or PIDs alone, fence ownership.
Only the process holding the collector directory lease may update this ledger.
"""

import hashlib
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .collector_update import read_json, write_json
from . import collector_lease, process_status

_locks = {}
_locks_guard = threading.Lock()
_log = logging.getLogger(__name__)


def _path_key(path, *, resolve=False):
    value = str(Path(path).resolve()) if resolve else os.path.abspath(path)
    if os.name == "nt":
        # A concurrently replaced Windows file can resolve with or without the
        # extended-length prefix. Both spellings must share one lock/identity.
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def _scope(session):
    store = getattr(session, "store", None)
    directory = getattr(store, "directory", None)
    profile = getattr(session, "profile", None)
    shop = getattr(session, "shop", None)
    if (
        not isinstance(directory, (str, os.PathLike))
        or not isinstance(profile, (str, os.PathLike))
        or not isinstance(shop, dict)
        or type(shop.get("id")) is not int
        or shop["id"] <= 0
    ):
        return None
    # Resolve the stable, existing data root once, not the target JSON that is
    # atomically replaced while concurrent registrations are in progress.
    directory = Path(_path_key(directory, resolve=True))
    if not getattr(collector_lease, "owns", lambda _: False)(directory):
        return None
    normalized = _path_key(profile, resolve=True)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return directory / "reader-targets" / f"{shop['id']}.json", shop["id"], fingerprint


def _browser_id(address):
    try:
        parsed = urlsplit(address)
        if parsed.scheme not in ("ws", "wss"):
            return None
        match = re.fullmatch(r"/devtools/browser/([^/]+)", parsed.path)
        return str(uuid.UUID(match[1])) if match else None
    except AttributeError, TypeError, ValueError:
        return None


def _lock(path):
    key = _path_key(path)
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


def _read(path):
    value = read_json(path, {"version": 1, "targets": []})
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("targets"), list):
        raise ValueError("采集页面归属记录格式无效，保留原文件")
    return value


def has_records(session):
    scope = _scope(session)
    if scope is None:
        return False
    path, sid, profile = scope
    try:
        with _lock(path):
            return any(
                isinstance(r, dict) and r.get("shop_id") == sid and r.get("profile") == profile
                for r in _read(path)["targets"]
            )
    except OSError, ValueError:
        # Let cleanup report a damaged/unreadable ledger without deleting it.
        return True


def register(session, browser_ws, target_id):
    scope = _scope(session)
    if scope is None:
        return None
    browser = _browser_id(browser_ws)
    if not browser or not isinstance(target_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_id):
        raise ValueError("采集页面标识无效，不能登记")
    path, sid, profile = scope
    record = {
        "shop_id": sid,
        "profile": profile,
        "browser_id": browser,
        "target_id": target_id,
        "owner_boot_id": process_status.IDENTITY["boot_id"],
        "owner_pid": process_status.IDENTITY["pid"],
        "created_at": time.time(),
    }
    with _lock(path):
        value = _read(path)
        # A fresh claim supersedes only the identical browser target. Records
        # for other profiles and targets remain available to their owner.
        targets = [
            r
            for r in value["targets"]
            if not isinstance(r, dict)
            or (r.get("profile"), r.get("browser_id"), r.get("target_id")) != (profile, browser, target_id)
        ]
        write_json(path, {**value, "targets": [*targets, record]})
    return record


def forget(session, record):
    scope = _scope(session)
    if scope is None or not isinstance(record, dict):
        return
    path, sid, profile = scope
    if record.get("shop_id") != sid or record.get("profile") != profile:
        return
    with _lock(path):
        value = _read(path)
        # Re-read after any async close. Never overwrite a newer registration
        # or the additions made while the network request was in progress.
        targets = [r for r in value["targets"] if r != record]
        if len(targets) != len(value["targets"]):
            write_json(path, {**value, "targets": targets})


async def cleanup(session, browser_ws, target_ids, close_target):
    scope = _scope(session)
    browser = _browser_id(browser_ws)
    if scope is None or not browser:
        return set()
    path, sid, profile = scope
    try:
        with _lock(path):
            records = list(_read(path)["targets"])
    except OSError, ValueError:
        _log.warning("采集页面归属记录暂不可读，保留页面，稍后重试")
        return set()
    present = set(target_ids)
    closed = set()
    for record in records:
        if (
            _scope(session) != scope
            or not isinstance(record, dict)
            or record.get("shop_id") != sid
            or record.get("profile") != profile
            or not isinstance(record.get("owner_boot_id"), str)
            or not record["owner_boot_id"]
            or record["owner_boot_id"] == process_status.IDENTITY["boot_id"]
        ):
            continue
        target = record.get("target_id")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target):
            continue
        try:
            uuid.UUID(record.get("browser_id"))
        except TypeError, ValueError, AttributeError:
            continue
        # Current-process tabs remain protected even across Session recreation.
        # Old browser UUIDs never authorize a close in the current browser.
        try:
            with _lock(path):
                if record not in _read(path)["targets"]:
                    continue
            if record.get("browser_id") == browser and target in present:
                await close_target(target)  # The caller must confirm closure.
                closed.add(target)
            forget(session, record)
        except Exception as error:
            _log.warning("旧采集页面清理暂未完成（%s），保留记录等待重试", type(error).__name__)
    return closed
