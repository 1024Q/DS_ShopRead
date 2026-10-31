"""Persistent collection policy and completion ledger shared by UI and scheduler."""

import json
import time
from datetime import datetime, timedelta
from .business_reader import BEIJING

REALTIME = {
    "orders",
    "ad_logs",
    "ad_products",
    "ad_reports",
    "overview",
    "transactions",
    "goodsdata",
    "reviews",
    "tickets",
    "aftersales",
    "evidence",
    "packages",
    "intercepts",
    "invoices",
}
DEFAULTS = {
    "enabled": True,
    "realtime_seconds": 60,
    "daily_time": "03:00",
    "keepalive_enabled": True,
    "keepalive_seconds": 900,
    "module_modes": {},
    "max_concurrent": 20,
    "realtime_concurrent": 3,
    "daily_concurrent": 1,
    "borrow_idle": True,
}
DEFAULTS["platform_intervals"] = {}
DEFAULTS["platform_modes"] = {}
DEFAULTS["platform_queues"] = {}
DEFAULTS["platform_priorities"] = {}
DEFAULTS["custom_seconds"] = 300
CUSTOM_MODES = ("custom", "custom2", "custom3")
DEFAULTS.update(custom2_seconds=600, custom3_seconds=1800)
DEFAULTS["frequency_names"] = {
    "realtime": "实时采集",
    "daily": "每日采集",
    "custom": "自定义采集间隔",
    "custom2": "自定义频率 2",
    "custom3": "自定义频率 3",
}
# Frequency groups are the only interval authority, including priority tasks.
SCHEDULE_FIELDS = {
    "enabled",
    "realtime_seconds",
    "daily_time",
    "custom_seconds",
    "custom2_seconds",
    "custom3_seconds",
    "frequency_names",
    "keepalive_enabled",
    "keepalive_seconds",
    "stagger_seconds",
    "retry_concurrent",
    "batch_pages",
    "batch_seconds",
    "adaptive_enabled",
    "change_adaptive_enabled",
}
DEFAULTS["shop_concurrent"] = 3
DEFAULTS["stagger_seconds"] = 60
DEFAULTS["retry_concurrent"] = 1
DEFAULTS.update(batch_pages=12, batch_seconds=20, adaptive_enabled=True, change_adaptive_enabled=True)


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS collection_tasks (
            shop_id INTEGER NOT NULL,cache_key TEXT NOT NULL,kind TEXT NOT NULL,options TEXT NOT NULL,
            last_requested REAL NOT NULL DEFAULT 0,last_success REAL NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL DEFAULT 0,failures INTEGER NOT NULL DEFAULT 0,
            lease_until REAL NOT NULL DEFAULT 0,last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(shop_id,cache_key));
            CREATE TABLE IF NOT EXISTS daily_archive (
            shop_id INTEGER NOT NULL,cache_key TEXT NOT NULL,payload TEXT NOT NULL,updated REAL NOT NULL,
            PRIMARY KEY(shop_id,cache_key));""")
        db.execute(
            "CREATE TABLE IF NOT EXISTS collection_demands(shop_id INTEGER,cache_key TEXT,kind TEXT,options TEXT,sources TEXT,PRIMARY KEY(shop_id,cache_key))"
        )
        columns = {r["name"] for r in db.execute("PRAGMA table_info(collection_tasks)")}
        if "demand_until" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN demand_until REAL NOT NULL DEFAULT 0")
        if "schedule_state" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN schedule_state TEXT NOT NULL DEFAULT 'pending'")
        if "continuation" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN continuation INTEGER NOT NULL DEFAULT 0")
        if "last_attempt" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN last_attempt REAL NOT NULL DEFAULT 0")
        if "error_kind" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN error_kind TEXT NOT NULL DEFAULT ''")
        if "blocked" not in columns:
            db.execute("ALTER TABLE collection_tasks ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0")
        for name, definition in [
            ("unchanged_count", "INTEGER NOT NULL DEFAULT 0"),
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("adaptive_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            if name not in columns:
                db.execute(f"ALTER TABLE collection_tasks ADD COLUMN {name} {definition}")
        from .collection_checkpoint import initialize

        initialize(db)
        db.execute(
            "UPDATE collection_tasks SET error_kind='rate_limit' WHERE kind='orders' AND error_kind IN ('','transient') AND (last_error LIKE '%操作太过频繁%' OR last_error LIKE '%限流%')"
        )
        # Old explicit denials can be classified without touching saved business data.
        db.execute(
            "UPDATE collection_tasks SET blocked=1,error_kind='permission' WHERE error_kind='' AND (last_error LIKE '%没有当前模块的查看权限%' OR last_error LIKE '%权限不足%' OR last_error LIKE '%无查看权限%')"
        )


def settings(store):
    with _policy_lock:
        return _settings(store)


def _settings(store):
    with store.connect() as db:
        modern = db.execute("SELECT value FROM settings WHERE key='collection_policy_v3'").fetchone()
        row = (
            db.execute("SELECT value FROM settings WHERE key='collection_policy'").fetchone() if not modern else modern
        )
    result = {**DEFAULTS, **(json.loads(row["value"]) if row else {})}
    result.pop("full_detail_seconds", None)
    result.pop("attention_seconds", None)
    result["frequency_names"] = {**DEFAULTS["frequency_names"], **result.get("frequency_names", {})}
    if not modern:
        from .three_queues import migrate

        result = migrate(result)
        with store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES('collection_policy_v3',?)", (json.dumps(result),)
            )
            result = json.loads(
                db.execute("SELECT value FROM settings WHERE key='collection_policy_v3'").fetchone()["value"]
            )
    from .frequency_policy import migrate, catalog, present

    updated = migrate(result)
    from .home_only_pause import apply, MARKER, REVISION, BACKUP

    updated = apply(updated, catalog())
    if updated != result:
        with store.connect() as db:
            if result.get(MARKER) != REVISION:
                db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (BACKUP, json.dumps(result)))
            db.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('collection_policy_v3',?)", (json.dumps(updated),)
            )
    return present(updated)


import threading

_policy_lock = threading.RLock()


def save_settings(store, value):
    with _policy_lock:
        return _save_settings(store, value)


def _save_settings(store, value):
    from .frequency_policy import save

    if not isinstance(value, dict):
        raise ValueError("设置内容应为对象")
    # settings() always loads the current frequency-group policy. Retired queue,
    # priority and per-module interval payloads are rejected by the one writer.
    return save(store, value, settings(store))


def uses_custom_interval(kind, config, platform="pdd"):
    return module_mode(kind, config, platform) in CUSTOM_MODES


def module_mode(kind, config, platform="pdd"):
    return (
        config.get("platform_modes", {})
        .get(platform, {})
        .get(kind, (config.get("module_modes", {}).get(kind) if platform == "pdd" else None))
    )


def frequency_name(kind, options, config, platform="pdd"):
    if config.get("frequency_version"):
        from .frequency_policy import group

        g = group(kind, options, config, platform)
        return g["name"] if g else "暂停"
    cadence = mode(kind, options, config, platform)
    if cadence == "off":
        return "暂停"
    if options.get("_order_lane") == "history":
        return "历史补采（空闲）"
    if options.get("_order_lane") == "daily":
        return "每日核对（空闲）"
    if options.get("_order_lane") == "recheck":
        return "订单状态回查"
    key = (
        module_mode(kind, config, platform)
        if cadence == "realtime" and uses_custom_interval(kind, config, platform)
        else cadence
    )
    name = config.get("frequency_names", DEFAULTS["frequency_names"]).get(key, DEFAULTS["frequency_names"][key])
    if options.get("_order_lane") == "recent":
        return "近期订单 · " + name
    if options.get("_order_lane") == "open":
        return options.get("status", "未完结") + "回查 · " + name
    return name


def mode(kind, options, config, platform="pdd"):
    from .shop_rotation import home_paused

    if home_paused(kind, options, config):
        return "off"
    if config.get("frequency_version"):
        from .frequency_policy import group

        g = group(kind, options, config, platform)
        return "off" if not g else "daily" if g.get("time") else "realtime"
    if config.get("queue_version") == 3:
        selected = module_mode(kind, config, platform)
        return "off" if selected == "off" else "daily" if selected == "daily" else "realtime"
    if module_mode(kind, config, platform) == "off":
        return "off"
    if kind == "orders" and options.get("_order_lane") in ("history", "daily"):
        return "daily"
    if options.get("_attention"):
        selected = module_mode(kind, config, platform)
        return "realtime" if not selected or selected in CUSTOM_MODES else selected
    if options.get("_exact_dates"):
        return "daily"
    # Historical trade reports never refresh every minute.
    if (
        kind in ("transactions", "goodsdata", "ad_products", "ad_reports")
        and options.get("trade_range", "realtime") != "realtime"
    ):
        return "daily"
    selected = module_mode(kind, config, platform)
    if selected in CUSTOM_MODES:
        return "realtime"
    return selected or ("realtime" if kind in REALTIME else "daily")


def interval_seconds(kind, config, platform="pdd", options=None):
    if config.get("frequency_version"):
        from .frequency_policy import group

        g = group(kind, options or {}, config, platform)
        return g["seconds"] if g else 86400
    selected = module_mode(kind, config, platform)
    if selected in CUSTOM_MODES:
        return config.get(selected + "_seconds", DEFAULTS[selected + "_seconds"])
    return config["realtime_seconds"]


def due(kind, options, last_success, config, now=None, platform="pdd", adaptive_seconds=0):
    if options.get("_order_lane") == "history" and last_success:
        return False
    if options.get("_invoice_details_refresh") and last_success:
        return False
    now = time.time() if now is None else now
    cadence = mode(kind, options, config, platform)
    if cadence == "off":
        return False
    if not last_success:
        return True
    if options.get("_attention") and cadence == "realtime":
        return now - last_success >= effective_interval(kind, options, config, platform, adaptive_seconds)
    if (
        kind in ("overview", "transactions", "ad_reports")
        and not options.get("_exact_dates")
        and options.get("trade_range", "realtime") == "realtime"
        and datetime.fromtimestamp(last_success, BEIJING).date() < datetime.fromtimestamp(now, BEIJING).date()
    ):
        return True
    if cadence == "realtime":
        return now - last_success >= effective_interval(kind, options, config, platform, adaptive_seconds)
    current = datetime.fromtimestamp(now, BEIJING)
    from .frequency_policy import group

    g = group(kind, options, config, platform) if config.get("frequency_version") else None
    hour, minute = map(int, (g.get("time") if g else config["daily_time"]).split(":"))
    boundary = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current < boundary:
        boundary -= timedelta(days=1)
    return datetime.fromtimestamp(last_success, BEIJING).date() < boundary.date()


def effective(config):
    from .platforms import PLATFORMS
    from .business import KINDS
    from .platform_support import supports
    from .attention import supported

    return {
        p: {
            k: {
                "name": frequency_name(k, {}, config, p),
                "mode": mode(k, {}, config, p),
                "seconds": interval_seconds(k, config, p),
                "attention_seconds": effective_interval(k, {"_attention": True}, config, p)
                if supported(p, k) and mode(k, {"_attention": True}, config, p) == "realtime"
                else None,
            }
            for k in KINDS
            if supports(p, k)
        }
        for p in PLATFORMS
    }


def register(store, shop_id, key, kind, options, cached=None, requested=True):
    from .status_cache import invalidating

    with invalidating(store, shop_id), store.connect() as db:
        db.execute(
            """INSERT INTO collection_tasks(shop_id,cache_key,kind,options,last_requested,last_success)
            VALUES(?,?,?,?,?,?) ON CONFLICT(shop_id,cache_key) DO UPDATE SET
            options=excluded.options,
            last_requested=CASE WHEN excluded.last_requested>0 THEN excluded.last_requested ELSE collection_tasks.last_requested END,
            last_success=MAX(collection_tasks.last_success,excluded.last_success)""",
            (
                shop_id,
                key,
                kind,
                json.dumps(options),
                time.time() if requested else 0,
                cached["updated"] if cached else 0,
            ),
        )
        if requested:
            db.execute(
                "UPDATE collection_tasks SET demand_until=?,schedule_state=CASE WHEN schedule_state='periodic' THEN schedule_state ELSE 'pending' END WHERE shop_id=? AND cache_key=?",
                (time.time() + 3600, shop_id, key),
            )


def state(store, shop_id, key):
    with store.connect() as db:
        row = db.execute("SELECT * FROM collection_tasks WHERE shop_id=? AND cache_key=?", (shop_id, key)).fetchone()
        return dict(row) if row else None


def claim(store, shop_id, key, force=False, now=None, resume=False):
    now = time.time() if now is None else now
    config = settings(store)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT t.*,s.platform FROM collection_tasks t JOIN shops s ON s.id=t.shop_id WHERE t.shop_id=? AND t.cache_key=?",
            (shop_id, key),
        ).fetchone()
        if not row or row["lease_until"] > now or (not force and not resume and row["retry_at"] > now):
            return False
        # A platform "too frequent" answer cools the whole module of that shop:
        # sibling scopes must not spend collection slots on the same refusal.
        if db.execute(
            "SELECT 1 FROM collection_tasks WHERE shop_id=? AND kind=? AND error_kind='rate_limit' AND retry_at>? LIMIT 1",
            (shop_id, row["kind"], now),
        ).fetchone():
            return False
        if row["blocked"] and not force:
            return False
        if resume:
            pass
        elif force:
            if row["last_success"] and now - row["last_success"] < 10:
                return False
        elif not config["enabled"] or mode(row["kind"], json.loads(row["options"]), config, row["platform"]) == "off":
            return False
        elif (
            not row["continuation"]
            and not row["failures"]
            and not due(
                row["kind"],
                json.loads(row["options"]),
                row["last_success"],
                config,
                now,
                platform=row["platform"],
                adaptive_seconds=row["adaptive_seconds"],
            )
        ):
            return False
        from .frequency_policy import group

        g = (
            group(row["kind"], json.loads(row["options"]), config, row["platform"])
            if config.get("frequency_version")
            else None
        )
        db.execute(
            "UPDATE collection_tasks SET lease_until=?,last_attempt=? WHERE shop_id=? AND cache_key=?",
            (now + max(600, (g["timeout"] + 60) if g else 600), now, shop_id, key),
        )
    from .status_cache import touch

    touch(store, shop_id)
    return True


def finish(store, shop_id, key, error=None, cancelled=False, error_kind=None, blocked=False, retry_after=None):
    from .status_cache import invalidating

    with invalidating(store, shop_id), store.write_connection() as db:
        if cancelled:
            db.execute("UPDATE collection_tasks SET lease_until=0 WHERE shop_id=? AND cache_key=?", (shop_id, key))
            return
        if error:
            if error_kind is None:
                from .collection_errors import classify

                error_kind, blocked = classify(ValueError(error))
            row = db.execute(
                "SELECT failures FROM collection_tasks WHERE shop_id=? AND cache_key=?", (shop_id, key)
            ).fetchone()
            failures = (row["failures"] if row else 0) + 1
            db.execute(
                "UPDATE collection_tasks SET lease_until=0,failures=?,retry_at=?,last_error=?,error_kind=?,blocked=? WHERE shop_id=? AND cache_key=?",
                (
                    failures,
                    time.time() + (max(1, min(20, retry_after)) if error_kind == "shop_cooldown" and retry_after is not None else retry_delay(failures, error_kind)),
                    error,
                    error_kind,
                    int(blocked),
                    shop_id,
                    key,
                ),
            )
        else:
            db.execute(
                "UPDATE collection_tasks SET lease_until=0,failures=0,retry_at=0,blocked=0,error_kind='',last_error='',last_success=? WHERE shop_id=? AND cache_key=?",
                (time.time(), shop_id, key),
            )


def yield_batch(store, shop_id, key):
    """A saved successful batch is progress, not another failed retry.

    Keep last_success unchanged until the whole snapshot is published, but
    remove the earlier failure's backoff so the continuation can run normally.
    """
    from .status_cache import invalidating

    with invalidating(store, shop_id), store.write_connection() as db:
        db.execute(
            "UPDATE collection_tasks SET lease_until=0,failures=0,retry_at=0,blocked=0,error_kind='',last_error='' WHERE shop_id=? AND cache_key=?",
            (shop_id, key),
        )


def retry_delay(failures, error_kind=""):
    if error_kind == "shop_cooldown":
        return 20
    delay = (60, 120, 300, 900, 1800, 3600)[min(max(1, failures), 6) - 1]
    if error_kind == "database_busy":
        return min(60, 15 * max(1, failures))
    if error_kind == "network":
        return min(300, delay)
    if error_kind == "rate_limit":
        return max(300, delay)
    return delay


def archive(store, shop_id, key, kind, options, payload):
    with store.connect() as db:
        shop = db.execute("SELECT platform FROM shops WHERE id=?", (shop_id,)).fetchone()
    if mode(kind, options, settings(store), shop["platform"] if shop else "pdd") != "daily":
        return
    from .status_cache import invalidating

    with invalidating(store, shop_id), store.connect() as db:
        updated = time.time()
        db.execute(
            "INSERT OR REPLACE INTO daily_archive(shop_id,cache_key,payload,updated) VALUES(?,?,?,?)",
            (shop_id, key, json.dumps(payload, ensure_ascii=False), updated),
        )
        if not db.execute("SELECT 1 FROM business_cache WHERE shop_id=? AND cache_key=?", (shop_id, key)).fetchone():
            from .table_index import write

            write(db, shop_id, key, payload, updated)


def cleanup_shop(db, shop_id):
    db.execute("DELETE FROM collection_demands WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM focus_resolutions WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM attention_snapshots WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM attention_rows WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM attention_counts WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM collection_checkpoints WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM collection_tasks WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM daily_archive WHERE shop_id=?", (shop_id,))


def effective_interval(kind, options, config, platform="pdd", adaptive_seconds=0):
    base = interval_seconds(kind, config, platform, options)
    return max(base, min(300, adaptive_seconds)) if config.get("change_adaptive_enabled", True) else base


def record_change(store, shop, key, kind, options, payload, config, platform="pdd"):
    if kind not in (
        "reviews",
        "tickets",
        "packages",
        "invoices",
        "evidence",
        "aftersales",
        "intercepts",
    ) or not payload.get("complete"):
        return
    import hashlib

    volatile = {
        "priority",
        "priority_reason",
        "hours_until_timeout",
        "remaining_hours",
        "overdue",
        "attention_excluded",
        "focus_pending",
    }
    rows = sorted(
        ({k: v for k, v in r.items() if k not in volatile} for r in payload.get("rows", [])),
        key=lambda r: str(r.get("id", "")),
    )
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with store.connect() as db:
        old = db.execute(
            "SELECT content_hash,unchanged_count FROM collection_tasks WHERE shop_id=? AND cache_key=?", (shop, key)
        ).fetchone()
        count = old["unchanged_count"] + 1 if old and old["content_hash"] == digest else 0
        # Do not slow down active urgent cases, failures, or a manually forced read.
        active = any(r.get("priority") for r in payload.get("rows", []))
        delay = 0 if active else 300 if count >= 6 else 120 if count >= 3 else 0
        if old and old["content_hash"] != digest:
            db.execute(
                "UPDATE collection_tasks SET unchanged_count=0,adaptive_seconds=0 WHERE shop_id=? AND kind=?",
                (shop, kind),
            )
        db.execute(
            "UPDATE collection_tasks SET content_hash=?,unchanged_count=?,adaptive_seconds=? WHERE shop_id=? AND cache_key=?",
            (digest, count, delay, shop, key),
        )
