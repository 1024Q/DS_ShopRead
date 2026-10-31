"""Authenticated stored overview projection. Reading never creates collection demand."""

import math
import threading
import time
from datetime import datetime, timedelta
import re

from fastapi import Request, Depends
from fastapi.responses import JSONResponse

from ..business import options_for
from ..business_reader import BEIJING
from ..promotions import options_for as promotion_options
from .business import selected_node


FIELDS = (
    "sales",
    "orders",
    "visitors",
    "views",
    "reviews_today",
    "pending_payment",
    "pending_shipping",
    "pending_aftersales",
    "pending_tickets",
    "ad_spend",
)
PLATFORMS = frozenset(("pdd", "taobao", "jd", "douyin", "taofactory", "xiaohongshu", "wechat", "other"))
EXTRA_FIELDS = ("inquiry_conversion", "shipping_due", "aftersales_due")
COUNT_TARGETS = {
    "pending_shipping": ("orders", "待发货"),
    "shipping_due": ("orders", "即将逾期发货"),
    "pending_aftersales": ("aftersales", "待商家处理"),
    "aftersales_due": ("aftersales", "待处理即将逾期"),
    "pending_tickets": ("tickets", "待处理"),
}


def text(value, limit=120):
    return value[:limit] if isinstance(value, str) else ""


def date_text(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None


def display_source(row, shops):
    """The display projection includes shop identity, never credentials or account secrets."""
    shop = shops.get(str(row.get("shop_id")), {})
    platform = shop.get("platform") or row.get("platform") or "pdd"
    platform = platform if platform in PLATFORMS else "other"
    return {
        "shop_id": shop.get("id", row.get("shop_id")),
        "shop_name": text(shop.get("name")),
        "platform": platform,
        "operator": text(shop.get("operator")),
        "operators": [text(v) for v in shop.get("operators", []) if isinstance(v, str)],
        "updated_at": timestamp(row.get("updated_at")),
        "center_received_at": timestamp(row.get("center_received_at")),
        "source_time": text(row.get("source_time"), 80),
        "day_pending": bool(row.get("day_pending")),
        "stale": bool(row.get("stale")),
        "complete": bool(row.get("complete")),
        "waiting_login": bool(row.get("waiting_login")),
        "syncing": False,
        "error": "数据待更新" if row.get("error") else "",
    }


def previous_projection(value):
    fields = {}
    for key in ("sales", "orders", "reviews_today", "visitors", "views", "ad_spend", "pending_payment"):
        item = value.get("fields", {}).get(key, {})
        fields[key] = {
            name: number(item.get(name))
            for name in ("value", "known", "total", "comparable", "change", "today_comparable", "yesterday_comparable")
        }
        fields[key].update(
            direction=item.get("direction")
            if item.get("direction") in ("up", "down", "flat", "unknown")
            else "unknown",
            label="昨日参考",
            basis="按中心已存数据的相同店铺比较",
        )
    return {"date": date_text(value.get("date")), "fields": fields, "basis": "按双方都有数据的相同店铺比较"}


def trend_projection(data, shops, now):
    from ..transactions import FIELDS as TRADE_FIELDS

    days = {now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()}
    metrics = []
    for row in data.get("metrics", []):
        points = []
        for point in row.get("trend", []):
            if point.get("date") not in days or point.get("series") not in ("今日累计", "昨日累计"):
                continue
            raw_hour = point.get("hour")
            hour = (
                int(raw_hour) if isinstance(raw_hour, str) and re.fullmatch(r"\d{1,2}", raw_hour) else number(raw_hour)
            )
            if hour is None or not 0 <= hour <= 23 or hour != int(hour):
                continue
            points.append(
                {
                    "date": point["date"],
                    "series": point["series"],
                    "hour": int(hour),
                    **{key: number(point.get(key)) for key in ("sales", "orders", "buyers")},
                }
            )
        metrics.append(
            {
                **display_source(row, shops),
                "summary": {k: number(row.get("summary", {}).get(k)) for k in TRADE_FIELDS},
                "trend": points,
            }
        )
    return {
        "metrics": metrics,
        "sources": [display_source(row, shops) for row in data.get("sources", [])],
        "scope": {"day": now.date().isoformat(), "trade_range": "realtime"},
    }


def ad_projection(data, shops, now):
    from ..promotions import FIELDS as AD_FIELDS, summarize

    metrics = []
    for row in data.get("metrics", []):
        stamp = timestamp(row.get("updated_at"))
        current = bool(stamp and stamp[:10] == now.date().isoformat())
        metrics.append(
            {**display_source(row, shops), **{key: number(row.get(key)) if current else None for key in AD_FIELDS}}
        )
    return {
        "metrics": metrics,
        "sources": [display_source(row, shops) for row in data.get("sources", [])],
        "ad_totals": summarize(metrics),
    }


def count_projection(service, kind, status):
    from ..home_counts import project as count_project

    options = options_for(kind, status=status)
    start, end = (options["date_from"], options["date_to"]) if kind in ("orders", "tickets") else ("", "")
    data = service.home_counts(
        kind,
        options,
        shop_id=None,
        platform=None,
        filters={"status": status, "date_from": start, "date_to": end},
        page_size=1,
    )
    value = count_project(data, kind, options)
    for row in value["rows"]:
        row["snapshot_scope"] = {
            k: row.get("snapshot_scope", {}).get(k)
            for k in ("status", "date_from", "date_to", "_attention", "explicit_dates")
            if k in row.get("snapshot_scope", {})
        }
        row["updated_at"] = timestamp(row.get("updated_at"))
        row["last_received_at"] = timestamp(row.get("last_received_at"))
    return {
        "home_counts": value,
        "scope": {
            key: options.get(key) for key in ("status", "date_from", "date_to", "explicit_dates") if key in options
        },
    }


def new_projection(data, shops, now):
    from ..new_products import summarize

    value = summarize(data, now.date().isoformat(), now.year)

    def counts(row):
        return {key: number(row.get(key)) for key in ("count", "previous", "unknown_dates", "ready")}

    def periods(row, key, pattern):
        return {k: number(v) for k, v in row.get(key, {}).items() if re.fullmatch(pattern, k)}

    return {
        "date": value["date"],
        "previous_date": value["previous_date"],
        "year": value["year"],
        "total": value["total"],
        "previous_total": value["previous_total"],
        "ready": value["ready"],
        "complete": value["complete"],
        "monthly": [{"period": p["period"], "count": number(p["count"])} for p in value["monthly"]],
        "shops": [
            {
                **display_source(row, shops),
                **counts(row),
                "days": periods(row, "days", r"\d{4}-\d{2}-\d{2}"),
                "months": periods(row, "months", r"\d{4}-\d{2}"),
            }
            for row in value["shops"]
        ],
        "operators": [
            {
                "name": text(row.get("name")),
                "shops": [s for s in row.get("shops", []) if str(s) in shops],
                **counts(row),
                "days": periods(row, "days", r"\d{4}-\d{2}-\d{2}"),
                "months": periods(row, "months", r"\d{4}-\d{2}"),
            }
            for row in value["operators"]
        ],
        "sources": [display_source(row, shops) for row in value.get("sources", [])],
    }


def attention_projection(service):
    """Same priority membership and freshness as the authenticated compact panel."""
    from sqlalchemy import select
    from .database import snapshots
    from .freshness import configurations, describe
    from ..attention import TITLES
    from ..operations import decorate

    policies = configurations(service.database)
    total = pending = stale = 0
    for shop in service.store.list():
        if shop.get("platform") != "pdd":
            continue
        with service.database.engine.connect() as db:
            records = {
                r["scope"].split(":")[1]: dict(r)
                for r in db.execute(
                    select(snapshots).where(
                        snapshots.c.node == shop["node_id"],
                        snapshots.c.shop == str(shop["local_id"]),
                        snapshots.c.scope.like("attention:%"),
                    )
                ).mappings()
            }
        for kind in TITLES:
            saved = records.get(kind)
            payload = saved["payload"] if saved else {}
            fresh = describe(
                kind,
                {**payload.get("_sync", {}).get("scope", {}), "_attention": True},
                payload,
                saved["updated"] if saved else 0,
                policies.get(shop["node_id"]),
                "pdd",
            )
            pending += not bool(saved and saved["updated"])
            stale += bool(not saved or fresh["overdue"] or not shop["collector_online"])
            total += sum(bool(decorate(kind, row, "pdd").get("priority")) for row in payload.get("rows", []))
    return {"total": total, "pending": pending, "stale": stale, "interval_seconds": 60}


def sections(service, shops, ads, now):
    from ..transactions import options_for as trade_options

    known = {str(shop["id"]): shop for shop in shops}
    result = {"ads": {"available": True, **ad_projection(ads, known, now)}, "counts": {}}

    def load(name, callback):
        try:
            return {"available": True, **callback()}
        except Exception:
            return {"available": False, "error": "该板块数据暂不可用，请稍后重试"}

    result["trades"] = load(
        "trades",
        lambda: trend_projection(
            service.data("transactions", {**trade_options("realtime"), "day": now.date().isoformat()}, read_only=True),
            known,
            now,
        ),
    )
    result["new_products"] = load(
        "new_products",
        lambda: new_projection(service.data("products", options_for("products"), read_only=True), known, now),
    )
    for metric, (kind, status) in COUNT_TARGETS.items():
        result["counts"][metric] = load(
            metric, lambda kind=kind, status=status: count_projection(service, kind, status)
        )
    result["attention"] = load("attention", lambda: attention_projection(service))

    def refunds():
        day = now.date().isoformat()
        data = service.data("orders", options_for("orders", day, day, "退款成功"), platform="pdd", read_only=True)
        return {
            "metrics": [
                {
                    **display_source(row, known),
                    **{k: number(row.get(k)) for k in ("refund_orders", "refund_amount")},
                    "refund_date": date_text(row.get("refund_date")),
                    "refund_basis": "order_created_success"
                    if row.get("refund_basis") == "order_created_success"
                    else None,
                    "refund_complete": bool(row.get("refund_complete")),
                }
                for row in data.get("metrics", [])
            ],
            "sources": [display_source(row, known) for row in data.get("sources", [])],
        }

    result["refunds"] = load("refunds", refunds)
    return result


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def timestamp(value):
    try:
        if type(value) in (int, float):
            parsed = datetime.fromtimestamp(value, BEIJING)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
        else:
            return None
        return parsed.isoformat()
    except ValueError, TypeError, OverflowError, OSError:
        return None


def totals(rows):
    result = {}
    for key in FIELDS:
        values = [value for row in rows if (value := number(row.get(key))) is not None]
        result[key] = round(sum(values), 2) if values else None
    return result


def project(shops, overview, ads, now):
    """Only explicitly allowed numbers and timestamps cross the public boundary."""
    metrics = {str(row.get("shop_id")): row for row in overview.get("metrics", [])}
    sources = {str(row.get("shop_id")): row for row in overview.get("sources", [])}
    promotions = {str(row.get("shop_id")): row for row in ads.get("metrics", [])}
    rows = []
    for shop in shops:
        identifier = str(shop["id"])
        metric, source, promotion = (
            metrics.get(identifier, {}),
            sources.get(identifier, {}),
            promotions.get(identifier, {}),
        )
        platform = shop.get("platform") or "pdd"
        platform = platform if platform in PLATFORMS else "other"
        values = {key: number(metric.get(key)) for key in FIELDS}
        ad_time = timestamp(promotion.get("updated_at"))
        # Current-day report keys already isolate days; also reject a stale
        # publication if a legacy snapshot was copied under today's key.
        values["ad_spend"] = (
            number(promotion.get("total_spend" if platform == "pdd" else "ad_spend"))
            if ad_time and ad_time[:10] == now.date().isoformat()
            else None
        )
        rows.append(
            {
                **display_source({**source, "shop_id": shop["id"]}, {identifier: shop}),
                **values,
                **{key: number(metric.get(key)) for key in EXTRA_FIELDS},
                "platform": platform,
                "read": bool(source.get("updated_at")),
                "stale": bool(source.get("stale")),
                "collected": timestamp(source.get("updated_at")),
                "received": timestamp(source.get("center_received_at")),
            }
        )
    return {
        "day": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "latest_collected_at": max((r["collected"] for r in rows if r["collected"]), default=None),
        "latest_received_at": max((r["received"] for r in rows if r["received"]), default=None),
        "totals": totals(rows),
        "coverage": {key: sum(number(row.get(key)) is not None for row in rows) for key in FIELDS},
        "shops": {
            "total": len(rows),
            "read": sum(r["read"] for r in rows),
            "stale": sum(r["stale"] and r["read"] for r in rows),
            "missing": sum(not r["read"] for r in rows),
        },
        "platforms": [
            {"platform": platform, "shops": len(group), "read": sum(r["read"] for r in group), "totals": totals(group)}
            for platform in sorted({r["platform"] for r in rows})
            if (group := [r for r in rows if r["platform"] == platform])
        ],
        "rows": [{k: v for k, v in row.items() if k not in ("read", "collected", "received")} for row in rows],
        "previous": previous_projection(overview.get("previous") or {}),
    }


class OverviewCache:
    def __init__(self, service, ttl=15, clock=time.monotonic, now=None):
        self.service, self.ttl, self.clock = service, ttl, clock
        self.now = now or (lambda: datetime.now(BEIJING))
        self.lock = threading.Lock()
        self.saved = None
        self.expires = 0

    def read(self):
        with self.lock:
            now = self.now()
            if self.saved and self.saved["day"] == now.date().isoformat() and self.clock() < self.expires:
                return self.saved
            overview = self.service.data("overview", options_for("overview"), read_only=True)
            ads = self.service.data("ad_reports", promotion_options("realtime", today=now.date()), read_only=True)
            shops = self.service.store.list()
            value = project(shops, overview, ads, now)
            value["sections"] = sections(self.service, shops, ads, now)
            # Failed reads never replace the last good value or extend its TTL.
            self.saved = value
            self.expires = self.clock() + self.ttl
            return value


def install(app, service, admin):
    cache = OverviewCache(service)

    @app.get('/api/home/snapshot', dependencies=[Depends(admin)])
    def overview(request: Request):
        headers = {'Cache-Control': 'no-store'}
        if selected_node.get() is not None:
            return JSONResponse({'error': '接口不存在'}, status_code=404, headers=headers)
        if request.query_params:
            return JSONResponse({'error': '概览不接受查询参数'}, status_code=400, headers=headers)
        try:
            return JSONResponse(cache.read(), headers=headers)
        except Exception:
            return JSONResponse({'error': '实时概览暂不可用，请稍后重试'}, status_code=503, headers=headers)
