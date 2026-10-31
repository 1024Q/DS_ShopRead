"""Capability-gated deltas from the last snapshot; unsupported adapters always perform full reads."""

import time
from datetime import datetime, timedelta
from .business_reader import BEIJING, ReadPage, fetch_list
from .operations import paginate, collect_operations
from .read_errors import LoginPending, PageReadError
from .read_page_pool import borrow

# Re-read a little before the previous snapshot: platform lists can index new records late.
OVERLAP = 3600
# Status-filtered reads that return every record whose platform state can still change.
OPEN_QUERIES = {
    "aftersales": (),
    "tickets": ({"status": "待处理"}, {"status": "处理中"}),
    "orders": ({"status": "待付款"}, {"status": "待发货"}, {"status": "待收货"}),
    "reviews": ({"reply": "unreplied"},),
    "evidence": ({"status": "待举证"}, {"status": "已举证"}),
    "invoices": ({"status": "待开票"}, {"status": "开票失败"}),
}
OPEN_STATUS = {
    "tickets": ("待处理", "处理中", "2小时内将逾期"),
    "evidence": ("待举证", "已举证"),
    "invoices": ("待开票", "开票失败"),
}


def boundary(config, now=None):
    current = datetime.fromtimestamp(time.time() if now is None else now, BEIJING)
    h, m = map(int, config["daily_time"].split(":"))
    result = current.replace(hour=h, minute=m, second=0, microsecond=0)
    if current < result:
        result -= timedelta(days=1)
    return result.timestamp()


def supported(session, kind, options):
    if session.shop.get("platform", "pdd") != "pdd" or kind not in ("packages", *OPEN_QUERIES):
        return False
    if (
        options.get("status")
        or options.get("_attention")
        or options.get("_exact_dates")
        or options.get("_filtered_read")
        or options.get("_invoice_details_refresh")
    ):
        return False
    if kind == "orders" and options.get("_order_lane") != "recent":
        return False
    return not options.get("stars") and not options.get("reply")


def since(options, previous):
    """Start of the delta window: the previous snapshot's start, never before the scope."""
    started = previous.get("_sync", {}).get("snapshot_started_at")
    return max(options["start"], min(int(started), options["end"]) - OVERLAP) if started else None


AFTERSALES_CLOSED = {
    "未发货退款，退款成功",
    "退款，退款成功",
    "退款，退款关闭",
    "退货退款，退款成功",
    "退货退款，退款关闭",
    "换货，换货关闭",
    "换货，确认完成换货",
    "补寄，补寄关闭",
    "补寄，确认完成补寄",
}


def is_open(kind, row):
    if kind == "aftersales":
        return "".join(str(row.get("status", "")).split()) not in AFTERSALES_CLOSED
    if kind == "orders":
        return not any(s in str(row.get("status", "")) for s in ("已取消", "已关闭", "交易成功", "已完成"))
    return not row.get("replied") if kind == "reviews" else row.get("status") in OPEN_STATUS[kind]


def created(row):
    try:
        return int(datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING).timestamp())
    except KeyError, TypeError, ValueError:
        return None


async def package_delta(session, options, previous, start=None):
    """Refresh all open parcels, new shipments, and parcels leaving the open set."""
    base = {
        "packStatus": 0,
        "shipId": 0,
        "orderSn": None,
        "trackNo": None,
        "processStatus": 0,
        "pageNumber": 1,
        "exceptionStatus": 0,
        "electricTrack": 0,
        "queryType": 0,
        "pageSize": 20,
        "shippingTimeStart": options["start"],
        "shippingTimeEnd": options["end"],
    }
    from .collection_checkpoint import current

    checkpoint = current.get()
    now = checkpoint.data["started_at"] if checkpoint else time.time()
    recent = start if start is not None else max(options["start"], int(now) - 36 * 3600)
    parts = []
    async with borrow(session, "packages", ReadPage) as page:
        await page.open("packages")

        async def read(**overrides):
            part = await paginate(page, "packages", {**base, **overrides}, "pageNumber", "orderListDTO", "count")
            if not part["complete"]:
                raise ValueError("增量范围读取不完整，改为完整核对")
            parts.append(part)
            return part["rows"]

        incoming = {}
        for row in await read(shippingTimeStart=recent):
            incoming[row["id"]] = row
        from .operations import STATUS

        for status in (10, 20, 30, 40):
            pending = [
                r
                for r in previous["rows"]
                if r["id"] not in incoming and r.get("status") == STATUS["packages"][status // 10 - 1]
            ]
            if not pending:
                continue
            times = [created(r) for r in pending]
            bounds = (
                {}
                if None in times
                else {
                    "shippingTimeStart": max(options["start"], min(times) - OVERLAP),
                    "shippingTimeEnd": min(options["end"], max(times) + OVERLAP),
                }
            )
            if bounds and bounds["shippingTimeStart"] > bounds["shippingTimeEnd"]:
                bounds = {}
            for row in await read(packStatus=status, **bounds):
                incoming[row["id"]] = row
        missing = [r for r in previous["rows"] if r.get("status") != "已签收" and r["id"] not in incoming]
        # A large transition set is cheaper and safer to reconcile in one full scan.
        if len(missing) > 20:
            raise ValueError("变化记录较多，改为完整核对")
        for old in missing:
            if not old.get("order_number") or not old.get("tracking_number"):
                raise ValueError("缺少包裹标识，改为完整核对")
            rows = await read(orderSn=old["order_number"], trackNo=old["tracking_number"])
            found = next((r for r in rows if r["id"] == old["id"]), None)
            if found is None:
                raise ValueError("包裹离开列表，改为完整核对")
            incoming[found["id"]] = found
    merged = {r["id"]: r for r in previous["rows"]}
    merged.update(incoming)
    return {
        **previous,
        "rows": list(merged.values()),
        "source_total": len(merged),
        "source_loaded": sum(p["source_loaded"] for p in parts),
        "complete": True,
        "_sync": {
            "collection_mode": "incremental",
            "full_at": previous["_sync"]["full_at"],
            "delta_records": len(incoming),
            "incremental_since": recent,
        },
        "scope_note": "增量更新上次采集以来发货及未完成包裹；每日完整核对已完成记录",
    }


async def list_delta(session, kind, options, previous, start):
    """Records created since the last snapshot, every open record, and records that left the open set."""
    if kind in ("tickets", "reviews"):
        return await merge_delta(kind, options, previous, start, lambda value: fetch_list(session, kind, value))
    if kind == "orders":
        from .orders import OrdersPage, collect

        async with borrow(session, "orders", OrdersPage) as page:
            await page.open("orders")
            return await merge_delta(kind, options, previous, start, lambda value: collect(page, value))
    async with borrow(session, kind, ReadPage) as page:
        await page.open(kind)
        return await merge_delta(kind, options, previous, start, lambda value: collect_operations(page, kind, value))


async def merge_delta(kind, options, previous, start, reader):
    # Invoices apply a date filter only on explicit ranges; the default scope covers every age.
    dated = {"_exact_dates": True} if kind == "invoices" else {}
    parts = []

    async def read(**overrides):
        part = await reader({**options, **overrides})
        if not part.get("complete"):
            raise ValueError("增量范围读取不完整，改为完整核对")
        parts.append(part)
        return {r["id"]: r for r in part["rows"]}

    incoming = await read(start=start, **dated)
    for query in OPEN_QUERIES[kind]:
        # The recent unfiltered read already contains every newly-created row.
        # Only old still-open rows can require a second status query. Unknown
        # creation times retain the original full range as a safe fallback.
        pending = [r for r in previous["rows"] if is_open(kind, r) and r["id"] not in incoming]
        if kind not in ("orders", "reviews"):
            wanted = query.get("status")
            pending = [
                r
                for r in pending
                if r.get("status") == wanted
                or kind == "tickets"
                and wanted == "待处理"
                and r.get("status") == "2小时内将逾期"
            ]
        if not pending:
            continue
        times = [created(r) for r in pending]
        bounds = (
            {}
            if None in times
            else {
                "start": max(options["start"], min(times) - OVERLAP),
                "end": min(options["end"], max(times) + OVERLAP),
            }
        )
        # Invoices require explicit date filtering; do not pretend an undated
        # record or an out-of-window pending row was covered by a narrow scan.
        if bounds and bounds["start"] > bounds["end"]:
            bounds = {}
        incoming.update(await read(**query, **bounds, **(dated if bounds else {})))
    missing = [r for r in previous["rows"] if is_open(kind, r) and r["id"] not in incoming]
    if missing:
        # Closed records keep their creation time; re-read only the span that contains them.
        times = [created(r) for r in missing]
        if None in times:
            raise ValueError("缺少记录创建时间，改为完整核对")
        found = {}
        for first, last in recheck_ranges(times, options):
            found.update(await read(start=first, end=last, **dated))
        if any(r["id"] not in found for r in missing):
            raise ValueError("记录离开列表，改为完整核对")
        incoming = {**found, **incoming}
    merged = {r["id"]: r for r in previous["rows"]}
    merged.update(incoming)
    return {
        **previous,
        "rows": list(merged.values()),
        "source_total": len(merged),
        "source_loaded": sum(p.get("source_loaded", len(p["rows"])) for p in parts),
        "complete": True,
        "_sync": {
            "collection_mode": "incremental",
            "full_at": previous["_sync"]["full_at"],
            "delta_records": len(incoming),
            "incremental_since": start,
        },
        "scope_note": "增量更新上次采集以来的新记录及全部未完结记录；每日完整核对一次",
    }


def recheck_ranges(times, options):
    """Recheck separated transitions without reading the empty days between them."""
    spans = []
    for stamp in sorted(set(times)):
        first = max(options["start"], stamp)
        last = min(options["end"], stamp)
        if first > last:
            raise ValueError("未完结记录超出查询范围，改为完整核对")
        if spans and first - spans[-1][1] <= 3600:
            spans[-1] = (spans[-1][0], last)
        else:
            spans.append((first, last))
    # More requests can cost more than a single bounded scan; cap fanout.
    return spans if len(spans) <= 8 else [(spans[0][0], spans[-1][1])]


async def read(session, kind, options, cached, config, force, reader):
    previous = (cached or {}).get("payload", {})
    if options.get("_relative_days") and kind != "invoices" and "rows" in previous:
        # Keep unknown dates and old urgent cases: absence of a date is not proof of expiry.
        previous = {
            **previous,
            "rows": [
                r
                for r in previous["rows"]
                if created(r) is None
                or options["start"] <= created(r) <= options["end"]
                or kind == "aftersales"
                and r.get("priority")
            ],
        }
    capable = supported(session, kind, options)
    full_at = previous.get("_sync", {}).get("full_at", 0)
    # Report adapters use date_from/date_to, never the list API's timestamp range.
    capable = (
        capable and isinstance(options.get("start"), (int, float)) and isinstance(options.get("end"), (int, float))
    )
    start = since(options, previous) if capable else None
    ready = ("rows" in previous and start is not None) or (kind == "packages" and bool(previous.get("rows")))
    can_delta = capable and not force and previous.get("complete") and full_at >= boundary(config) and ready
    from .collection_checkpoint import current

    checkpoint = current.get()
    if checkpoint:
        can_delta = bool(checkpoint.data.setdefault("incremental", can_delta)) and can_delta
        checkpoint.data["incremental"] = can_delta
        if can_delta:
            start = checkpoint.data.setdefault("incremental_since", start)
    if can_delta:
        try:
            return await (
                package_delta(session, options, previous, start)
                if kind == "packages"
                else list_delta(session, kind, options, previous, start)
            )
        except LoginPending, PageReadError:
            raise
        except ValueError, KeyError:
            if checkpoint:
                checkpoint.data["incremental"] = False
    payload = await reader(session, kind, options)
    return {
        **payload,
        "_sync": {
            **payload.get("_sync", {}),
            "collection_mode": "full",
            "full_at": (checkpoint.data["started_at"] if checkpoint else time.time()) if payload.get("complete") else 0,
            "incremental_supported": capable,
        },
    }
