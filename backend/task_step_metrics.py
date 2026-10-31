"""On-demand, read-only timings for independently scheduled collection steps.

The estimate is the mean of the latest successful batch for each shop/step, not
a claimed history of all runs. Timing reads stay in each shop database and never
copy business rows or enqueue collection work.
"""

import copy
import json
import math
import sqlite3
import time
from datetime import datetime

from .business_reader import BEIJING
from .frequency_policy import task_id

TTL_SECONDS = 30
MAX_SNAPSHOTS_PER_SHOP = 512
READ_BUDGET_SECONDS = 3
SAMPLE_BASIS = "latest_successful_batch_per_shop"

_QUERY = """WITH recent AS (
        SELECT 'cache' AS origin,shop_id,cache_key,updated FROM business_cache WHERE shop_id=?
        UNION ALL
        SELECT 'archive',shop_id,cache_key,updated FROM daily_archive WHERE shop_id=?
        ORDER BY updated DESC LIMIT ?
    ) SELECT c.cache_key,c.updated,
    json_extract(coalesce(t.metadata,b.payload,a.payload),'$._sync.scope') AS scope,
    json_extract(coalesce(t.metadata,b.payload,a.payload),'$._sync.read_seconds') AS read_seconds
    FROM recent c LEFT JOIN table_snapshots t
    ON t.shop_id=c.shop_id AND t.cache_key=c.cache_key AND t.updated=c.updated
    LEFT JOIN business_cache b ON c.origin='cache' AND b.shop_id=c.shop_id AND b.cache_key=c.cache_key
    LEFT JOIN daily_archive a ON c.origin='archive' AND a.shop_id=c.shop_id AND a.cache_key=c.cache_key
    ORDER BY c.updated DESC"""


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _identity(kind, scope, updated):
    # A yesterday-retained snapshot of a "today" read still measured today's
    # task at that time. Do not relabel its timing each time the date changes.
    if kind == "orders" and scope.get("_order_lane") == "recent":
        day = datetime.fromtimestamp(updated, BEIJING).date().isoformat()
        return "orders@today" if scope.get("date_from") == day else "orders@yesterday"
    return task_id(kind, scope)


def combine(values):
    """Merge disjoint node/shop samples with a weighted estimate."""
    combined = {}
    for metrics in values:
        for platform, steps in metrics.items():
            target = combined.setdefault(platform, {})
            for identifier, sample in steps.items():
                count = sample.get("sample_count", 0)
                seconds = sample.get("estimated_read_seconds")
                if type(count) is not int or count <= 0 or not _number(seconds):
                    continue
                item = target.setdefault(
                    identifier,
                    {
                        "sample_count": 0,
                        "total_read_seconds": 0,
                        "sampled_at": 0,
                        "last_read_seconds": None,
                        "sample_basis": SAMPLE_BASIS,
                    },
                )
                item["sample_count"] += count
                item["total_read_seconds"] += sample.get("total_read_seconds", seconds * count)
                if sample.get("sampled_at", 0) >= item["sampled_at"]:
                    item["sampled_at"] = sample.get("sampled_at", 0)
                    item["last_read_seconds"] = sample.get("last_read_seconds")
    for steps in combined.values():
        for item in steps.values():
            item["estimated_read_seconds"] = round(item["total_read_seconds"] / item["sample_count"], 1)
    return combined


def snapshot(store, platform=""):
    """Return bounded measurements only when the settings UI requests them."""
    now = time.monotonic()
    cache = getattr(store, "_task_step_metrics", {})
    previous = cache.get(platform)
    if previous and now - previous[0] < TTL_SECONDS:
        return copy.deepcopy(previous[1])

    from .business import BusinessService
    from .platform_support import supports
    from .task_catalog import REMOVED

    shops = [s for s in store.list() if not platform or s.get("platform", "pdd") == platform]
    samples = []
    unavailable = 0
    for shop in shops:
        if time.monotonic() - now >= READ_BUDGET_SECONDS:
            unavailable += 1
            continue
        try:
            with store.shop_db(shop["id"], timeout=1) as db:
                db.set_progress_handler(lambda: int(time.monotonic() - now >= READ_BUDGET_SECONDS), 10000)
                rows = db.execute(_QUERY, (shop["id"], shop["id"], MAX_SNAPSHOTS_PER_SHOP)).fetchall()
        except sqlite3.Error:
            unavailable += 1
            continue
        latest = {}
        for row in rows:
            seconds = row["read_seconds"]
            if not _number(seconds):
                continue
            key = row["cache_key"].removeprefix("summary:")
            kind = key.split(":", 1)[0]
            if not supports(shop.get("platform", "pdd"), kind):
                continue
            try:
                scope = json.loads(row["scope"] or "{}")
            except TypeError, ValueError:
                continue
            if not isinstance(scope, dict):
                continue
            # Ignore data collected before the shop's login identity changed.
            # Cache identity is reused unchanged; no ledger or checkpoint writes.
            if BusinessService.key(None, kind, scope, shop) != key:
                continue
            identifier = _identity(kind, scope, row["updated"])
            if identifier in REMOVED or identifier in latest:
                continue
            latest[identifier] = {
                "sample_count": 1,
                "last_read_seconds": round(seconds, 1),
                "estimated_read_seconds": round(seconds, 1),
                "total_read_seconds": seconds,
                "sampled_at": row["updated"],
                "sample_basis": SAMPLE_BASIS,
            }
        samples.append({shop.get("platform", "pdd"): latest})
    result = {
        "step_metrics": combine(samples),
        "step_metrics_status": {
            "complete": unavailable == 0,
            "unavailable_shops": unavailable,
            "updated_at": time.time(),
            "sample_limit_per_shop": MAX_SNAPSHOTS_PER_SHOP,
        },
    }
    cache[platform] = (now, result)
    store._task_step_metrics = cache
    return copy.deepcopy(result)
