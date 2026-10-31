"""Order hot reads and durable, day-sized idle backfill. Query views never crawl history."""

from datetime import datetime, timedelta
from .business_reader import BEIJING

OPEN = ("待付款", "待发货", "待收货")


def disabled_scope(options):
    return options.get("_order_lane") in ("history", "daily") or (
        not options.get("_order_lane") and not options.get("status")
    )


def view(options):
    return (
        not options.get("_order_lane")
        and not options.get("_list_completion")
        and options.get("status", "") in ("", *OPEN)
    )


def key(shop):
    return "orders:view-v2:" + str(shop.get("profile_version", 0))


def day_scope(day, lane):
    from .business import options_for

    return {**options_for("orders", date_from=day, date_to=day), "_order_lane": lane, "explicit_dates": True}


def requested_refresh(record, stamp):
    import time

    return bool(
        record
        and not record.get("last_success")
        and record.get("last_requested", 0) > stamp
        and record.get("demand_until", 0) > time.time()
    )


def covered(store, shop, scope, record=None):
    """Only successful whole-day publications prove that idle backfill is redundant."""
    if scope.get("_order_lane") != "history":
        return False
    from .table_index import metadata

    saved = metadata(store, shop["id"], key(shop))
    stamp = saved["payload"].get("_order_days", {}).get(scope.get("date_from"), 0) if saved else 0
    return bool(stamp and not requested_refresh(record, stamp))


def live():
    from .business import options_for

    today = datetime.now(BEIJING).date()
    for offset in (0, 1):
        yield day_scope((today - timedelta(days=offset)).isoformat(), "recent")
    # Recent tasks own today/yesterday; status rechecks own older orders only.
    default = options_for("orders")
    last = (today - timedelta(days=2)).isoformat()
    for status in OPEN:
        yield {
            **options_for("orders", date_from=default["date_from"], date_to=last, status=status),
            "_order_lane": "open",
            "_order_partition": 2,
        }
    yield options_for("orders", status="即将逾期发货")
    yield options_for("orders", date_from=today.isoformat(), date_to=today.isoformat(), status="退款成功")


def background(store, shop):
    from .table_index import metadata

    saved = metadata(store, shop["id"], key(shop))
    payload = saved["payload"] if saved else {}
    today = datetime.now(BEIJING).date()
    # Resolve disappeared open orders before loading unrelated history.
    pending = [
        d
        for d in payload.get("_order_recheck_days", [])
        if (today - timedelta(days=89)).isoformat() <= d <= today.isoformat()
    ]
    for day in pending[:2]:
        scope = day_scope(day, "recheck")
        from .order_storage import recheck_windows

        for first, last in recheck_windows(store, shop, scope):
            yield {**scope, "start": first, "end": last}


def normalize(row):
    row = dict(row)
    status = str(row.get("status", ""))
    row["attention_status"] = (
        ""
        if any(v in status for v in ("已取消", "已关闭", "交易成功", "已完成"))
        else next((s for s in OPEN if s in status), "")
    )
    if row.get("_order_status_stale"):
        row["attention_status"] = ""
    return row


def seed(store, shop):
    """Reuse successful current-identity snapshots once; never mark partial reads covered."""
    import json
    from .business import BusinessService

    previous = store.read_cache(shop["id"], "orders:view-v1:" + str(shop.get("profile_version", 0)))
    previous = (previous or {}).get("payload", {})
    result = {str(r["id"]): r for r in previous.get("rows", [])}
    days = dict(previous.get("_order_days", {}))
    with store.connect() as db:
        headers = db.execute(
            "SELECT cache_key,metadata,updated FROM table_snapshots WHERE shop_id=? AND cache_key LIKE 'orders:%' ORDER BY updated",
            (shop["id"],),
        ).fetchall()
    for header in headers:
        meta = json.loads(header["metadata"])
        scope = meta.get("_sync", {}).get("scope")
        if not scope or not meta.get("complete") or scope.get("_order_lane"):
            continue
        expected = BusinessService.key(None, "orders", scope, shop, legacy=True)
        if expected != header["cache_key"]:
            continue
        saved = store.read_cache(shop["id"], header["cache_key"])
        if not saved:
            continue
        stamp = meta.get("_sync", {}).get("snapshot_started_at", header["updated"])
        for row in saved["payload"].get("rows", []):
            rid = str(row["id"])
            if result.get(rid, {}).get("_order_observed", 0) <= stamp:
                result[rid] = normalize({**row, "_order_observed": stamp})
        if not scope.get("status"):
            first = datetime.fromisoformat(scope["date_from"]).date()
            last = datetime.fromisoformat(scope["date_to"]).date()
            while first <= last:
                days[first.isoformat()] = max(days.get(first.isoformat(), 0), stamp)
                first += timedelta(days=1)
    # Preserve finished legacy checkpoint segments without claiming date coverage.
    with store.connect() as db:
        checkpoints = db.execute(
            "SELECT c.cache_key,c.payload,t.options FROM collection_checkpoints c JOIN collection_tasks t USING(shop_id,cache_key) WHERE c.shop_id=? AND t.kind='orders'",
            (shop["id"],),
        ).fetchall()
        for checkpoint in checkpoints:
            scope = json.loads(checkpoint["options"])
            if (
                scope.get("_order_lane")
                or scope.get("status")
                or BusinessService.key(None, "orders", scope, shop, legacy=True) != checkpoint["cache_key"]
            ):
                continue
            saved = json.loads(checkpoint["payload"])
            stamp = saved.get("started_at", 0)
            for part, meta in saved.get("parts", {}).items():
                if not meta.get("done"):
                    continue
                records = (
                    [
                        json.loads(r[0])
                        for r in db.execute(
                            "SELECT payload FROM checkpoint_rows WHERE shop_id=? AND cache_key=? AND part=?",
                            (shop["id"], checkpoint["cache_key"], part),
                        )
                    ]
                    if saved.get("storage_version") == 2
                    else list(meta.get("rows", {}).values())
                )
                for row in records:
                    rid = str(row["id"])
                    if result.get(rid, {}).get("_order_observed", 0) <= stamp:
                        result[rid] = normalize({**row, "_order_observed": stamp})
    return {"rows": list(result.values()), "_order_days": days}


def publish(store, shop, options, payload):
    if not options.get("_order_lane") or not payload.get("complete"):
        return
    from .order_storage import publish as write_orders

    write_orders(store, shop, options, payload)


def request(service, options, shop_id, force, platform):
    from .filter_scope import matches

    if not options.get("status"):
        raise ValueError("全部订单采集已停用，请选择订单状态")
    tasks = []
    for shop in service.store.list():
        if shop.get("platform", "pdd") != "pdd" or not matches(shop_id, shop["id"]) or not matches(platform, "pdd"):
            continue
        scopes = list(live())
        for scope in scopes:
            if scope.get("_order_lane") == "open" and options.get("status") and scope["status"] != options["status"]:
                continue
            result = service.request_sync("orders", scope, shop["id"], force, platform="pdd")
            tasks.extend(result["tasks"])
    return {"tasks": tasks, "mode": "database", "force": force}
