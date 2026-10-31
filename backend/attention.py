from .filter_scope import matches

"""Independent, complete snapshots of urgent scopes; never replace full business reads."""
import json, time
from datetime import datetime, timedelta
from .business_reader import BEIJING
from .shop_identity import identity
from .priority_rules import KINDS, VERSION, reason
from .operations import decorate

TITLES = {
    "aftersales": "售后工作台",
    "evidence": "商家举证",
    "tickets": "工单列表",
    "intercepts": "拦截快递",
    "packages": "包裹中心",
    "invoices": "订单开票",
}


def supported(platform, kind):
    return platform == "pdd" and kind in KINDS


def scopes(shop):
    from .business import options_for

    for kind in sorted(KINDS):
        if supported(shop.get("platform", "pdd"), kind):
            options = options_for(kind)
            if kind == "tickets":
                options = {
                    **options_for(kind, date_from=(datetime.now(BEIJING).date() - timedelta(days=59)).isoformat()),
                    "_relative_days": 60,
                }
            yield kind, {**options, "_attention": True, "_attention_rules": VERSION}


def initialize(store):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        from .focus_resolution import initialize as initialize_resolutions

        initialize_resolutions(db)
        db.execute(
            "CREATE TABLE IF NOT EXISTS attention_rows(shop_id INTEGER,kind TEXT,id TEXT,payload TEXT NOT NULL,priority INTEGER,deadline TEXT,PRIMARY KEY(shop_id,kind,id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS attention_counts(shop_id INTEGER,kind TEXT,total INTEGER,PRIMARY KEY(shop_id,kind))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS attention_snapshots (shop_id INTEGER,kind TEXT,payload TEXT NOT NULL,updated REAL NOT NULL,started REAL NOT NULL,PRIMARY KEY(shop_id,kind))"
        )
        # Row counts live in a sidecar kept by triggers: an extra snapshot column would break older
        # releases' positional inserts on rollback, while triggers also cover writes by those releases.
        stats = "INSERT OR REPLACE INTO attention_snapshot_stats(shop_id,kind,row_count) VALUES(NEW.shop_id,NEW.kind,json_array_length(NEW.payload,'$.rows'));"
        for sql in (
            "CREATE TABLE IF NOT EXISTS attention_snapshot_stats(shop_id INTEGER,kind TEXT,row_count INTEGER,PRIMARY KEY(shop_id,kind))",
            "CREATE TRIGGER IF NOT EXISTS attention_stats_insert AFTER INSERT ON attention_snapshots BEGIN "
            + stats
            + " END",
            "CREATE TRIGGER IF NOT EXISTS attention_stats_update AFTER UPDATE OF payload,shop_id,kind ON attention_snapshots BEGIN DELETE FROM attention_snapshot_stats WHERE shop_id=OLD.shop_id AND kind=OLD.kind; "
            + stats
            + " END",
            "CREATE TRIGGER IF NOT EXISTS attention_stats_delete AFTER DELETE ON attention_snapshots BEGIN DELETE FROM attention_snapshot_stats WHERE shop_id=OLD.shop_id AND kind=OLD.kind; END",
        ):
            db.execute(sql)
        db.execute(
            "INSERT INTO attention_snapshot_stats(shop_id,kind,row_count) SELECT a.shop_id,a.kind,json_array_length(a.payload,'$.rows') FROM attention_snapshots a WHERE NOT EXISTS(SELECT 1 FROM attention_snapshot_stats t WHERE t.shop_id=a.shop_id AND t.kind=a.kind)"
        )
        db.execute(
            "DELETE FROM attention_snapshot_stats WHERE NOT EXISTS(SELECT 1 FROM attention_snapshots a WHERE a.shop_id=attention_snapshot_stats.shop_id AND a.kind=attention_snapshot_stats.kind)"
        )

        for row in db.execute(
            "SELECT a.*,s.platform FROM attention_snapshots a LEFT JOIN shops s ON s.id=a.shop_id"
        ).fetchall():
            data = json.loads(row["payload"])
            if data.get("_priority_rules_version") != VERSION:
                data["rows"] = [
                    decorate(row["kind"], r, row["platform"])
                    for r in data.get("rows", [])
                    if wanted(row["kind"], r, row["platform"])
                ]
                data["_priority_rules_version"] = VERSION
                db.execute(
                    "UPDATE attention_snapshots SET payload=?,updated=0 WHERE shop_id=? AND kind=?",
                    (json.dumps(data, ensure_ascii=False), row["shop_id"], row["kind"]),
                )
                project(db, row["shop_id"], row["kind"], data["rows"])
                store.invalidate_responses(row["shop_id"], row["kind"], db=db)
            else:
                if "full_snapshot_updated_at" not in data.get("_sync", {}):
                    # One-time repair for content previously changed without advancing
                    # its upload version. Preserve the original complete-read clock.
                    data["_sync"] = {
                        **data.get("_sync", {}),
                        "full_snapshot_updated_at": row["updated"],
                        "snapshot_started_at": data.get("_sync", {}).get("snapshot_started_at", row["started"]),
                    }
                    db.execute(
                        "UPDATE attention_snapshots SET payload=?,updated=? WHERE shop_id=? AND kind=?",
                        (
                            json.dumps(data, ensure_ascii=False),
                            max(time.time(), row["updated"] + 0.000001),
                            row["shop_id"],
                            row["kind"],
                        ),
                    )
                if not db.execute(
                    "SELECT 1 FROM attention_counts WHERE shop_id=? AND kind=?", (row["shop_id"], row["kind"])
                ).fetchone():
                    project(db, row["shop_id"], row["kind"], data["rows"])


def project(db, shop_id, kind, rows):
    unique = {str(r["id"]): r for r in rows}
    db.execute("DELETE FROM attention_rows WHERE shop_id=? AND kind=?", (shop_id, kind))
    db.executemany(
        "INSERT INTO attention_rows(shop_id,kind,id,payload,priority,deadline) VALUES(?,?,?,?,?,?)",
        [
            (shop_id, kind, key, json.dumps(r, ensure_ascii=False), 3, r.get("deadline") or "9999")
            for key, r in unique.items()
        ],
    )
    db.execute(
        "INSERT OR REPLACE INTO attention_counts(shop_id,kind,total) VALUES(?,?,?)", (shop_id, kind, len(unique))
    )


def wanted(kind, row, platform=None):
    return bool(reason(kind, row, platform))


def publish(store, shop_id, kind, payload):
    if not payload.get("complete"):
        raise ValueError("重点范围尚未读取完整，保留上次结果")
    stamp = time.time()
    started = payload.get("_sync", {}).get("snapshot_started_at", stamp)
    shop = next((s for s in store.list() if s["id"] == shop_id), None)
    if not shop or not supported(shop.get("platform", "pdd"), kind):
        return
    rows = [
        decorate(kind, {**r, "attention_excluded": False, "focus_pending": False}, "pdd")
        for r in payload.get("rows", [])
        if wanted(kind, r, "pdd")
    ]
    data = {
        **payload,
        "rows": rows,
        "_priority_rules_version": VERSION,
        "_sync": {**payload.get("_sync", {}), "full_snapshot_updated_at": stamp},
    }
    with store.connect() as db:
        old = db.execute(
            "SELECT started FROM attention_snapshots WHERE shop_id=? AND kind=?", (shop_id, kind)
        ).fetchone()
        if old and old["started"] > started:
            return
        from . import focus_resolution as resolutions

        previous = {
            r["id"]: json.loads(r["payload"])
            for r in db.execute("SELECT id,payload FROM attention_rows WHERE shop_id=? AND kind=?", (shop_id, kind))
        }
        current = {str(r["id"]) for r in rows}
        for key, row in previous.items():
            if key not in current:
                resolutions.save(db, shop_id, kind, row, started, True)
        for key in current:
            resolutions.reopen(db, shop_id, kind, key)
        from .operations_insights import attention_changed

        attention_changed(db, shop_id, kind, rows)
        db.execute(
            "INSERT OR REPLACE INTO attention_snapshots(shop_id,kind,payload,updated,started) VALUES(?,?,?,?,?)",
            (shop_id, kind, json.dumps(data, ensure_ascii=False), stamp, started),
        )
        project(db, shop_id, kind, rows)
        from .focus_records import share, clear_markers

        clear_markers(
            db, shop_id, kind, {str(r["id"]) for r in rows}, started, payload.get("_sync", {}).get("scope", {})
        )
        share(db, shop_id, kind, rows, started)
        store.invalidate_responses(shop_id, kind, db=db)

    from .direct_refunds import notify

    notify(store, shop_id, kind)


def reconcile(store, shop_id, kind, payload):
    if kind not in KINDS or not payload.get("complete"):
        return
    # Only records actually observed by the full read may update urgent records.
    # Absence from a differently dated/filtered full list is not proof of closure.
    incoming = {str(r["id"]): r for r in payload.get("rows", [])}
    started = payload.get("_sync", {}).get("snapshot_started_at", 0)
    with store.connect() as db:
        old = db.execute("SELECT * FROM attention_snapshots WHERE shop_id=? AND kind=?", (shop_id, kind)).fetchone()
        if not old:
            return
        from . import focus_resolution as resolutions

        facts = resolutions.records(db, shop_id, kind)
        data = json.loads(old["payload"])
        active = {str(r["id"]): r for r in data["rows"]}
        changed = False
        for key, fresh in incoming.items():
            row = active.get(key)
            fact = facts.get(key)
            if row and old["started"] > started or fact and fact["started"] > started:
                continue
            if row is not None or fact is not None:
                fresh = {**fresh, "attention_excluded": False, "focus_pending": False}
                # Ordinary shipping/ticket/invoice statuses cannot establish
                # membership in the platform's separate quick-filter result.
                if row and kind in ("packages", "tickets", "invoices") and fresh.get("status") == row.get("status"):
                    fresh = {**fresh, "attention_status": row.get("attention_status", "")}
                if not row and fact and old["started"] > started:
                    # A newer complete focus snapshot still excludes this ID.
                    # The broad read may verify its normal status but cannot
                    # resurrect older membership in the urgent list.
                    fresh["attention_excluded"] = True
                if wanted(kind, fresh):
                    active[key] = decorate(kind, fresh)
                    resolutions.reopen(db, shop_id, kind, key)
                    from .focus_records import share

                    share(db, shop_id, kind, [active[key]], started)
                else:
                    active.pop(key, None)
                    resolutions.save(db, shop_id, kind, fresh, started, False)
                changed = True
        if changed:
            rows = list(active.values())
            data["rows"] = rows
            # Content publication and a complete scope verification are different clocks.
            # Ordinary reads may resolve records without proving the whole urgent scope fresh.
            data["_sync"] = {
                **data.get("_sync", {}),
                "full_snapshot_updated_at": data.get("_sync", {}).get("full_snapshot_updated_at", old["updated"]),
                "snapshot_started_at": data.get("_sync", {}).get("snapshot_started_at", old["started"]),
            }
            from .operations_insights import attention_changed

            attention_changed(db, shop_id, kind, rows)
            db.execute(
                "UPDATE attention_snapshots SET payload=?,started=?,updated=? WHERE shop_id=? AND kind=?",
                (
                    json.dumps(data, ensure_ascii=False),
                    max(old["started"], started),
                    max(time.time(), old["updated"] + 0.000001),
                    shop_id,
                    kind,
                ),
            )
            project(db, shop_id, kind, rows)
            store.invalidate_responses(shop_id, kind, db=db)


    from .direct_refunds import notify

    notify(store, shop_id, kind)


def read(
    service, platform="", shop_id=None, kind="", page=1, summary=False, keyword="", sort_by="", sort_direction="asc"
):
    from .collection import settings, due, effective_interval

    store = service.store
    sources = []
    now = time.time()
    selected = []
    config = settings(store)
    with store.connect() as db:
        db.create_function("casefold", 1, lambda s: str(s or "").casefold(), deterministic=True)
        db.execute("BEGIN")
        snapshots = {
            (r["shop_id"], r["kind"]): dict(r)
            for r in db.execute("SELECT shop_id,kind,updated FROM attention_snapshots")
        }
        tasks = {
            (r["shop_id"], r["cache_key"]): dict(r)
            for r in db.execute("SELECT shop_id,cache_key,last_error FROM collection_tasks")
        }
        counts = {(r["shop_id"], r["kind"]): r["total"] for r in db.execute("SELECT * FROM attention_counts")}
        for shop in store.list():
            if not matches(platform, shop.get("platform", "pdd")) or not matches(shop_id, shop["id"]):
                continue
            for module, options in scopes(shop):
                if kind and kind != module:
                    continue
                key = service.key(module, options, shop)
                task = tasks.get((shop["id"], key), {})
                saved = snapshots.get((shop["id"], module))
                updated = saved["updated"] if saved else None
                coverage = {
                    "tickets": "近60天 · 2小时内将逾期",
                    "evidence": "近180天 · 待举证",
                    "intercepts": "近90天 · 拦截失败",
                    "aftersales": "平台介入待商家处理 / 平台处理中（不限创建日期）",
                    "packages": "即将发货-揽收超时（平台快捷筛选）",
                    "invoices": "3天内需开票（平台快捷筛选）",
                }
                source = {
                    **identity(shop),
                    "shop_id": shop["id"],
                    "shop_name": shop["name"],
                    "kind": module,
                    "updated_at": updated,
                    "stale": not updated or due(module, options, updated, config, now, shop.get("platform", "pdd")),
                    "error": task.get("last_error", ""),
                    "syncing": (shop["id"], key) in service.jobs,
                    "coverage": coverage[module],
                }
                source["interval_seconds"] = effective_interval(module, options, config, shop.get("platform", "pdd"))
                sources.append(source)
                selected.append((shop["id"], module))
        total = sum(counts.get(k, 0) for k in selected)
        rows = []
        marks = ",".join("(?,?)" for _ in selected)
        where = "(shop_id,kind) IN (" + marks + ")"
        args = [v for k in selected for v in k]
        if selected and keyword:
            where += (
                " AND ("
                + " OR ".join(
                    "instr(casefold(coalesce(json_extract(payload,'$." + f + "'),'')),?)>0"
                    for f in ("id", "name", "order_number", "tracking_number", "problem")
                )
                + " OR EXISTS(SELECT 1 FROM shop_operators o WHERE o.shop_id=attention_rows.shop_id AND instr(casefold(o.name),?)>0))"
            )
            args.extend([keyword.casefold()] * 6)
            total = db.execute("SELECT count(*) FROM attention_rows WHERE " + where, args).fetchone()[0]
        pages = max(1, (total + 19) // 20)
        page = min(page, pages)
        if selected and not summary:
            fields = {
                "shop_name": "(SELECT coalesce(nullif(alias,''),name) FROM shops WHERE id=attention_rows.shop_id)",
                "operators": "(SELECT group_concat(name,',') FROM shop_operators WHERE shop_id=attention_rows.shop_id)",
                "module_name": "kind",
                "order_number": "coalesce(json_extract(payload,'$.order_number'),id)",
                "status": "coalesce(json_extract(payload,'$.priority_reason'),json_extract(payload,'$.status'))",
                "deadline": "deadline",
                "updated_at": "(SELECT updated FROM attention_snapshots s WHERE s.shop_id=attention_rows.shop_id AND s.kind=attention_rows.kind)",
            }
            order = (
                (fields[sort_by] + (" DESC" if sort_direction == "desc" else " ASC") + ",shop_id,id")
                if sort_by in fields
                else "priority DESC,deadline,shop_id,id"
            )
            records = db.execute(
                "SELECT * FROM attention_rows WHERE " + where + " ORDER BY "
                "CASE WHEN kind='aftersales' AND json_extract(payload,'$.status')='平台处理中' "
                "AND coalesce(json_extract(payload,'$.attention_excluded'),0)=0 THEN 0 ELSE 1 END," + order + " LIMIT 20 OFFSET ?",
                args + [(page - 1) * 20],
            ).fetchall()
            by_source = {(r["shop_id"], r["kind"]): r for r in sources}
            for row in records:
                source = by_source[(row["shop_id"], row["kind"])]
                rows.append(
                    {
                        **json.loads(row["payload"]),
                        **source,
                        "priority": row["priority"],
                        "module_name": TITLES[row["kind"]] if source.get("platform") != "taobao" else "包裹预警",
                    }
                )
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_count": pages,
        "sources": [] if summary else sources,
        "interval_seconds": min((s["interval_seconds"] for s in sources), default=config["realtime_seconds"]),
        "pending": sum(not s["updated_at"] for s in sources),
        "stale": sum(s["stale"] for s in sources),
    }
