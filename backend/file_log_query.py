"""Read structured node log files without copying events into a database."""

import hashlib
import heapq
import json
import sqlite3
import time
from datetime import datetime, timedelta
from .log_spool import get, ZONE
from .platforms import PLATFORMS
from .shop_identity import shop_label
from .filter_scope import matches


def read_logs(
    store,
    level="",
    shop_id=None,
    keyword="",
    limit=100,
    module="",
    date_from="",
    date_to="",
    before=0,
    before_id=0,
    after=0,
):
    start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=ZONE).timestamp() if date_from else 0
    end = (
        (datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=ZONE) + timedelta(days=1)).timestamp()
        if date_to
        else float("inf")
    )
    cutoff = (datetime.fromtimestamp(time.time(), ZONE).date() - timedelta(days=6)).isoformat()
    operators = {}
    if keyword:
        try:
            with store.connect() as db:
                for row in db.execute("SELECT shop_id,name FROM shop_operators"):
                    operators.setdefault(row["shop_id"], []).append(row["name"])
        except sqlite3.Error:
            pass

    def records():
        for path in get(store).files():
            if path.name[:10] < cutoff:
                continue
            if date_from and path.name[:10] < date_from:
                continue
            if date_to and path.name[:10] > date_to:
                continue
            try:
                with path.open("rb") as stream:
                    while True:
                        offset = stream.tell()
                        raw = stream.readline()
                        if not raw or not raw.endswith(b"\n"):
                            break
                        try:
                            row = json.loads(raw)
                            stamp = float(row["created"])
                            if not start <= stamp < end or after and stamp < after:
                                continue
                            if level and row.get("level") != level:
                                continue
                            if module and row.get("module") != module:
                                continue
                            if not matches(shop_id, row.get("shop_id")):
                                continue
                            platform = row.get("platform", "")
                            row["platform_name"] = PLATFORMS.get(platform, "")
                            text = " ".join(
                                str(row.get(k, "")) for k in ("message", "module", "shop_name", "platform_name")
                            )
                            if (
                                keyword
                                and keyword.casefold()
                                not in (text + " " + " ".join(operators.get(row.get("shop_id"), []))).casefold()
                            ):
                                continue
                            # Stable across restarts, exactly representable by JavaScript numbers.
                            ident = int(hashlib.sha256(f"{path.name}:{offset}".encode()).hexdigest()[:13], 16)
                            if before and (stamp, ident) >= (before, before_id):
                                continue
                            row.update(id=ident, last_seen=stamp, occurrences=1)
                            row["shop_label"] = shop_label(row) if row.get("shop_id") is not None else "系统"
                            yield row
                        except ValueError, KeyError, TypeError:
                            continue
            except FileNotFoundError:
                continue  # retention may run during a query

    # Bounded result memory, including searches over all seven daily files.
    return heapq.nlargest(limit, records(), key=lambda r: (r["last_seen"], r["id"]))
