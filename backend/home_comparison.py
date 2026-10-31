"""Yesterday's final saved overview, paired by shop and profile; never invented zeros."""

import json, math
from datetime import datetime, timedelta
from .business_reader import BEIJING
from .partition_reads import rows as partition_rows

FIELDS = ("sales", "orders", "reviews_today", "visitors", "views", "ad_spend", "pending_payment")


def valid(value):
    return type(value) in (int, float) and math.isfinite(value)


def read(store, records, current, now=None, keys=None):
    day = ((now or datetime.now(BEIJING)).date() - timedelta(days=1)).isoformat()
    previous = {}
    origins = {}
    if records:
        ids = [s["id"] for s in records]
        for row in partition_rows(
            store,
            "SELECT h.shop_id,h.payload FROM home_history h JOIN shops s ON h.shop_id=s.id AND h.profile_version=s.profile_version WHERE h.day=? AND h.kind='overview'",
            (day,),
            ids,
        ):
            payload = json.loads(row["payload"])
            if payload.get("complete"):
                previous[row["shop_id"]] = payload.get("metrics", {})
                origins[row["shop_id"]] = {field: "昨日末次成功快照" for field in FIELDS}
        if keys:
            # Match the profile-specific cache key; read saved reports in one batch.
            all_keys = sorted({key for pair in keys.values() for key in pair})
            placeholders = ",".join("?" for _ in all_keys)
            cached = (
                sorted(
                    partition_rows(
                        store,
                        f"SELECT shop_id,cache_key,payload,updated FROM business_cache WHERE cache_key IN ({placeholders}) UNION ALL SELECT shop_id,cache_key,payload,updated FROM daily_archive WHERE cache_key IN ({placeholders})",
                        all_keys + all_keys,
                        ids,
                    ),
                    key=lambda row: row["updated"],
                )
                if all_keys
                else []
            )
            for row in cached:
                sid = row["shop_id"]
                pair = keys.get(sid)
                if not pair or row["cache_key"] not in pair:
                    continue
                payload = json.loads(row["payload"])
                sync = payload.get("_sync", {})
                if not payload.get("complete"):
                    continue
                metrics = payload.get("metrics", {})
                old = previous.setdefault(sid, {})
                labels = origins.setdefault(sid, {})
                if (
                    row["cache_key"] == pair[0]
                    and datetime.fromtimestamp(sync.get("snapshot_started_at") or row["updated"], BEIJING)
                    .date()
                    .isoformat()
                    == day
                ):
                    for field in FIELDS:
                        if not valid(old.get(field)) and valid(metrics.get(field)):
                            old[field] = metrics[field]
                            labels[field] = "昨日末次成功快照"
                if row["cache_key"] == pair[1]:
                    # Yesterday report has a different collection day and is never
                    # relabelled as yesterday after midnight by a stale cache key.
                    scope = sync.get("scope", {})
                    if (
                        scope.get("trade_range") != "yesterday"
                        or scope.get("day") != (now or datetime.now(BEIJING)).date().isoformat()
                    ):
                        continue
                    for field in ("sales", "orders"):
                        value = metrics.get("summary", {}).get(field)
                        if valid(value):
                            old[field] = value
                            labels[field] = "昨日交易报表"
    by_id = {r["shop_id"]: r for r in current if not r.get("day_pending")}
    result = {}
    for field in FIELDS:
        known = [previous[s["id"]][field] for s in records if valid(previous.get(s["id"], {}).get(field))]
        paired = [
            (by_id[s["id"]][field], previous[s["id"]][field])
            for s in records
            if valid(by_id.get(s["id"], {}).get(field)) and valid(previous.get(s["id"], {}).get(field))
        ]
        today = sum(a for a, b in paired)
        yesterday = sum(b for a, b in paired)
        labels = {
            origins.get(s["id"], {}).get(field, "昨日末次成功快照")
            for s in records
            if valid(previous.get(s["id"], {}).get(field))
        }
        result[field] = {
            "value": round(sum(known), 2) if known else None,
            "known": len(known),
            "total": len(records),
            "comparable": len(paired),
            "label": "昨日交易" if labels == {"昨日交易报表"} else "昨日参考" if len(labels) > 1 else "昨日快照",
            "basis": "、".join(sorted(labels)),
            "change": round((today - yesterday) / yesterday * 100, 2) if paired and yesterday else None,
            "direction": "up"
            if today > yesterday
            else "down"
            if today < yesterday
            else "flat"
            if paired
            else "unknown",
            "today_comparable": round(today, 2) if paired else None,
            "yesterday_comparable": round(yesterday, 2) if paired else None,
        }
    return {
        "date": day,
        "fields": result,
        "basis": "成交额、订单数优先使用昨日交易报表，其余使用昨日末次成功快照；增减按双方均有数据的同一批店铺计算，非同时段同比。",
    }
