"""Durable net payment figures sourced only from yesterday's transaction report."""

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def numeric(value, money=False):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            return None
        if money:
            return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return int(number) if number == number.to_integral_value() else None
    except InvalidOperation, ValueError, OverflowError:
        return None


def backfill(db, shop_id, key, payload, updated):
    """Fill days missed while the collector was offline from the multi-day report's daily trend.

    Trend values never replace a day already recorded from the yesterday report.
    """
    sync = payload.get("_sync") or {}
    scope = sync.get("scope") or {}
    shop = db.execute("SELECT profile_version,platform FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not shop or shop["platform"] != "pdd" or not payload.get("complete") or not scope.get("day"):
        return 0
    started = sync.get("snapshot_started_at") or updated
    filled = 0
    for row in payload.get("rows") or []:
        try:
            day = date.fromisoformat(row.get("date")).isoformat()
        except TypeError, ValueError:
            continue
        if row.get("series") != "每日" or day >= scope["day"]:
            continue
        old = db.execute(
            "SELECT source_summary,source_started_at FROM payment_daily WHERE shop_id=? AND profile_version=? AND stat_date=?",
            (shop_id, shop["profile_version"], day),
        ).fetchone()
        if old and (
            json.loads(old["source_summary"]).get("basis") != "daily_trend" or old["source_started_at"] > started
        ):
            continue
        sales, refund = [numeric(row.get(k), True) for k in ("sales", "refund_amount")]
        orders, refund_orders = [numeric(row.get(k)) for k in ("orders", "refund_orders")]
        summary = {
            "basis": "daily_trend",
            **{k: row.get(k) for k in ("sales", "refund_amount", "orders", "refund_orders")},
        }
        db.execute(
            """INSERT OR REPLACE INTO payment_daily
          (shop_id,profile_version,platform,stat_date,sales_cents,refund_cents,orders,refund_orders,net_payment_cents,net_orders,complete,source_cache_key,source_summary,source_time,source_started_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                shop_id,
                shop["profile_version"],
                shop["platform"],
                day,
                sales,
                refund,
                orders,
                refund_orders,
                sales - refund if sales is not None and refund is not None else None,
                orders - refund_orders if orders is not None and refund_orders is not None else None,
                int(all(v is not None for v in (sales, refund, orders, refund_orders))),
                key,
                json.dumps(summary, ensure_ascii=False),
                None,
                started,
                updated,
            ),
        )
        filled += 1
    return filled


def missing_days(db, shop_id, today, days=30):
    """Days between the shop's first record (within the trend span) and the day before yesterday with no record."""
    shop = db.execute("SELECT profile_version FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not shop:
        return []
    first = (today - timedelta(days=days)).isoformat()
    recorded = {
        r[0]
        for r in db.execute(
            "SELECT stat_date FROM payment_daily WHERE shop_id=? AND profile_version=? AND stat_date>=?",
            (shop_id, shop["profile_version"], first),
        )
    }
    if not recorded:
        return []
    current = date.fromisoformat(min(recorded))
    last = today - timedelta(days=2)
    result = []
    while current <= last:
        if current.isoformat() not in recorded:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def save(db, shop_id, key, payload, updated):
    if key.split(":")[0] != "transactions":
        return False
    sync = payload.get("_sync") or {}
    scope = sync.get("scope") or {}
    if scope.get("trade_range") in ("week", "month"):
        return bool(backfill(db, shop_id, key, payload, updated))
    if scope.get("trade_range") != "yesterday":
        return False
    try:
        day = (date.fromisoformat(scope["day"]) - timedelta(days=1)).isoformat()
    except KeyError, TypeError, ValueError:
        return False
    shop = db.execute("SELECT profile_version,platform FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not shop:
        return False
    summary = (payload.get("metrics") or {}).get("summary") or {}
    sales, refund = [numeric(summary.get(k), True) for k in ("sales", "refund_amount")]
    orders, refund_orders = [numeric(summary.get(k)) for k in ("orders", "refund_orders")]
    complete = int(bool(payload.get("complete")) and all(v is not None for v in (sales, refund, orders, refund_orders)))
    started = sync.get("snapshot_started_at") or updated
    old = db.execute(
        "SELECT complete,source_started_at FROM payment_daily WHERE shop_id=? AND profile_version=? AND stat_date=?",
        (shop_id, shop["profile_version"], day),
    ).fetchone()
    if old and (old["source_started_at"] > started or old["complete"] and not complete):
        return False
    db.execute(
        """INSERT OR REPLACE INTO payment_daily
      (shop_id,profile_version,platform,stat_date,sales_cents,refund_cents,orders,refund_orders,net_payment_cents,net_orders,complete,source_cache_key,source_summary,source_time,source_started_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            shop_id,
            shop["profile_version"],
            shop["platform"],
            day,
            sales,
            refund,
            orders,
            refund_orders,
            sales - refund if sales is not None and refund is not None else None,
            orders - refund_orders if orders is not None and refund_orders is not None else None,
            complete,
            key,
            json.dumps(summary, ensure_ascii=False),
            (payload.get("metrics") or {}).get("source_time"),
            started,
            updated,
        ),
    )
    return True


def initialize(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS payment_daily (
          shop_id INTEGER NOT NULL,profile_version INTEGER NOT NULL,platform TEXT NOT NULL,stat_date TEXT NOT NULL,
          sales_cents INTEGER,refund_cents INTEGER,orders INTEGER,refund_orders INTEGER,
          net_payment_cents INTEGER,net_orders INTEGER,complete INTEGER NOT NULL,
          source_cache_key TEXT NOT NULL,source_summary TEXT NOT NULL,source_time TEXT,
          source_started_at REAL NOT NULL,updated_at REAL NOT NULL,
          PRIMARY KEY(shop_id,profile_version,stat_date))""")
        db.execute("CREATE INDEX IF NOT EXISTS payment_daily_date ON payment_daily(stat_date,platform)")
        # Backfill and the migration marker publish atomically while the collector writes.
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM settings WHERE key='payment_daily_v1'").fetchone():
            return
        for row in db.execute(
            "SELECT shop_id,cache_key,payload,updated FROM business_cache WHERE cache_key LIKE 'transactions:%' UNION ALL SELECT shop_id,cache_key,payload,updated FROM daily_archive WHERE cache_key LIKE 'transactions:%' ORDER BY updated"
        ):
            save(db, row["shop_id"], row["cache_key"], json.loads(row["payload"]), row["updated"])
        db.execute("INSERT INTO settings(key,value) VALUES('payment_daily_v1','true')")
