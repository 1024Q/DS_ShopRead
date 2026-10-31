"""Rebuildable SQLite list projection. Original snapshots remain authoritative."""

import json
import re
from contextlib import nullcontext

KINDS = {
    "orders",
    "ad_logs",
    "overview",
    "transactions",
    "products",
    "goodsdata",
    "reviews",
    "tickets",
    "packages",
    "invoices",
    "aftersales",
    "evidence",
    "intercepts",
    "navigator",
    "quality",
    "brands",
    "activities",
    "ad_products",
    "ad_reports",
}
SEARCH = (
    "operator_name",
    "operation_module",
    "id",
    "name",
    "code",
    "goods_id",
    "order_number",
    "problem",
    "content",
    "reply",
    "tracking_number",
    "return_tracking_number",
    "invoice_title",
    "invoice_title_type",
    "buyer_taxpayer_number",
    "invoice_number",
    "goods_spec",
    "invoice_other_info",
    "progress",
    "remark",
    "activity_name",
    "activity_id",
    "activity_type",
)
COLUMNS = {"status", "created_at", "priority", "name", "goods_id", "sales", "visitors", "amount", "stock"}


def initialize(store, reconcile=True):
    """Create the projection schema; optionally reconcile it with cached payloads.

    reconcile=False is used when per-shop databases are active: the node database
    then only holds a frozen rollback copy, and shard writes keep their own index.
    """
    from .priority_rules import PROJECTION_VERSION as VERSION, KINDS as FOCUS_KINDS

    with store.connect() as db:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS table_snapshots(shop_id INTEGER,cache_key TEXT,updated REAL,metadata TEXT,row_count INTEGER,PRIMARY KEY(shop_id,cache_key));
          CREATE TABLE IF NOT EXISTS table_rows(shop_id INTEGER,cache_key TEXT,ordinal INTEGER,payload TEXT NOT NULL,
            status TEXT,created_at TEXT,priority INTEGER,name TEXT,goods_id TEXT,sales REAL,visitors REAL,amount REAL,stock REAL,search_text TEXT,thumbnail_url TEXT,
            PRIMARY KEY(shop_id,cache_key,ordinal));
          CREATE INDEX IF NOT EXISTS table_status_date ON table_rows(shop_id,cache_key,status,created_at);
          CREATE INDEX IF NOT EXISTS table_created ON table_rows(shop_id,cache_key,created_at);
          CREATE INDEX IF NOT EXISTS table_priority ON table_rows(shop_id,cache_key,priority);
          CREATE INDEX IF NOT EXISTS table_goods ON table_rows(shop_id,goods_id);
          CREATE INDEX IF NOT EXISTS table_sales ON table_rows(shop_id,cache_key,sales);
          CREATE INDEX IF NOT EXISTS table_visitors ON table_rows(shop_id,cache_key,visitors);
          CREATE INDEX IF NOT EXISTS table_amount ON table_rows(shop_id,cache_key,amount);
          CREATE TABLE IF NOT EXISTS table_names(shop_id INTEGER,goods_id TEXT,name TEXT,cache_key TEXT,PRIMARY KEY(shop_id,goods_id));
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS table_entity_lookup ON table_rows(shop_id,CAST(json_extract(payload,'$.id') AS TEXT),cache_key)"
        )
        if not reconcile:
            return
        db.execute("BEGIN IMMEDIATE")
        # Read before backfill writes advance it; older releases do not maintain table_names.
        mark = db.execute("SELECT value FROM settings WHERE key='table_names_watermark'").fetchone()
        # Compare headers first; decode payloads only for snapshots that need a rebuild or migration.
        source = """SELECT 'c' AS origin,c.shop_id,c.cache_key,c.updated FROM business_cache c UNION ALL SELECT 'a',a.shop_id,a.cache_key,a.updated FROM daily_archive a
            WHERE NOT EXISTS(SELECT 1 FROM business_cache c WHERE c.shop_id=a.shop_id AND c.cache_key=a.cache_key)"""
        snapshots = {
            (r["shop_id"], r["cache_key"]): (r["updated"], r["metadata"])
            for r in db.execute("SELECT shop_id,cache_key,updated,metadata FROM table_snapshots")
        }
        for row in db.execute(source).fetchall():
            kind = row["cache_key"].split(":")[0]
            if kind not in KINDS:
                continue
            old = snapshots.get((row["shop_id"], row["cache_key"]))
            if (
                old
                and old[0] == row["updated"]
                and kind not in FOCUS_KINDS
                and kind not in ("quality", "packages", "ad_products", "activities", "products")
            ):
                continue
            meta = json.loads(old[1]) if old else {}
            migrated = (kind in FOCUS_KINDS and (not old or meta.get("_priority_rules_version") != VERSION)) or (
                kind == "quality" and (not old or meta.get("_quality_rules_version") != 1)
            )
            migrated |= kind == "packages" and (not old or meta.get("_package_timing_version") != 1)
            migrated |= kind in ("ad_products", "activities") and (
                not old or meta.get("_commerce_filters_version") != 1
            )
            if not old or old[0] != row["updated"] or kind == "products" and "_creation_counts" not in meta or migrated:
                payload = db.execute(
                    "SELECT payload FROM "
                    + ("business_cache" if row["origin"] == "c" else "daily_archive")
                    + " WHERE shop_id=? AND cache_key=?",
                    (row["shop_id"], row["cache_key"]),
                ).fetchone()[0]
                write(db, row["shop_id"], row["cache_key"], json.loads(payload), row["updated"])
                if migrated:
                    store.invalidate_responses(row["shop_id"], kind, db=db)
        prune(db)
        refresh_names(db, None if mark is None else float(mark[0]))


def _stage(db, sql, args=()):
    db.execute(
        "CREATE TEMP TABLE IF NOT EXISTS table_name_goods(shop_id INTEGER,goods_id TEXT,PRIMARY KEY(shop_id,goods_id))"
    )
    db.execute("INSERT OR IGNORE INTO temp.table_name_goods(shop_id,goods_id) " + sql, args)


def _resolve_names(db):
    """Materialize each staged product's fallback name: latest snapshot row with a thumbnail, first ordinal."""
    db.execute(
        "DELETE FROM table_names WHERE (shop_id,goods_id) IN (SELECT shop_id,goods_id FROM temp.table_name_goods)"
    )
    db.execute("""INSERT INTO table_names(shop_id,goods_id,name,cache_key) SELECT shop_id,goods_id,name,cache_key FROM (SELECT p.shop_id,p.goods_id,p.name,p.cache_key,
        row_number() OVER(PARTITION BY p.shop_id,p.goods_id ORDER BY t.updated DESC,p.ordinal) AS n FROM temp.table_name_goods g JOIN table_rows p ON p.shop_id=g.shop_id AND p.goods_id=g.goods_id
        JOIN table_snapshots t ON t.shop_id=p.shop_id AND t.cache_key=p.cache_key WHERE p.thumbnail_url<>'') WHERE n=1""")
    db.execute("DELETE FROM temp.table_name_goods")


def _watermark(db, updated):
    db.execute(
        "INSERT INTO settings(key,value) VALUES('table_names_watermark',?) ON CONFLICT(key) DO UPDATE SET value=CAST(max(CAST(settings.value AS REAL),CAST(excluded.value AS REAL)) AS TEXT)",
        (repr(float(updated)),),
    )


def refresh_names(db, mark):
    # Rebuild once, then catch up snapshots that older releases wrote (updated>mark) or removed.
    if mark is None:
        db.execute("DELETE FROM table_names")
        _stage(db, "SELECT DISTINCT shop_id,goods_id FROM table_rows WHERE goods_id<>'' AND thumbnail_url<>''")
    else:
        _stage(
            db,
            "SELECT r.shop_id,r.goods_id FROM table_snapshots t JOIN table_rows r ON r.shop_id=t.shop_id AND r.cache_key=t.cache_key WHERE t.updated>? AND r.goods_id<>''",
            (mark,),
        )
        _stage(
            db,
            "SELECT shop_id,goods_id FROM table_names n WHERE NOT EXISTS(SELECT 1 FROM table_snapshots t WHERE t.shop_id=n.shop_id AND t.cache_key=n.cache_key)",
        )
    _resolve_names(db)
    _watermark(db, db.execute("SELECT coalesce(max(updated),0) FROM table_snapshots").fetchone()[0])


def write(db, shop_id, key, payload, updated, reuse=False):
    """Project a snapshot into table rows. reuse=True (normal publication) may keep
    identical rows in place; migrations and rebuilds always rewrite."""
    kind = key.split(":")[0]
    if kind not in KINDS:
        return
    if payload.get("_order_rows_v2"):
        from .order_storage import rebuild

        rebuild(db, shop_id, key, payload, updated)
        return
    from .operations import decorate
    from .product_images import thumbnail

    rows = payload.get("rows") or []
    from .focus_records import overlay

    rows = overlay(
        db,
        shop_id,
        kind,
        rows,
        payload.get("_sync", {}).get("snapshot_started_at", updated),
        payload.get("_sync", {}).get("scope"),
    )
    metadata = {k: v for k, v in payload.items() if k != "rows"}
    from .priority_rules import PROJECTION_VERSION as VERSION

    metadata["_priority_rules_version"] = VERSION
    if kind == "quality":
        metadata["_quality_rules_version"] = 1
    if kind == "packages":
        metadata["_package_timing_version"] = 1
    record = db.execute("SELECT platform FROM shops WHERE id=?", (shop_id,)).fetchone()
    platform = record["platform"] if record else "pdd"
    if kind in ("ad_products", "activities"):
        metadata["_commerce_filters_version"] = 1
    if kind == "products":
        counts = {}
        unknown = 0
        unique = {str(r.get("goods_id") or r["id"]): r for r in rows}
        for row in unique.values():
            day = str(row.get("created_at") or "")[:10]
            if day:
                counts[day] = counts.get(day, 0) + 1
            else:
                unknown += 1
        metadata["_creation_counts"] = {"days": counts, "unknown": unknown}
    from .priority_rules import KINDS as FOCUS_KINDS

    # Re-collected lists are often identical. For kinds without time-dependent
    # focus overlays, an unchanged row set only refreshes the snapshot header and
    # skips re-encoding/upserting every row (the main local write cost).
    digest = None
    if kind not in FOCUS_KINDS:
        import hashlib

        digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        metadata["_rows_sha256"] = digest + ":" + platform
    old = db.execute(
        "SELECT metadata,row_count FROM table_snapshots WHERE shop_id=? AND cache_key=?", (shop_id, key)
    ).fetchone()
    unchanged = False
    if reuse and digest and old and old["row_count"] == len(rows):
        try:
            previous = json.loads(old["metadata"])
            # Projection versions must match too, otherwise the rows need a rebuild.
            unchanged = all(
                previous.get(field) == metadata.get(field)
                for field in (
                    "_rows_sha256",
                    "_priority_rules_version",
                    "_quality_rules_version",
                    "_package_timing_version",
                    "_commerce_filters_version",
                )
            )
        except ValueError:
            unchanged = False
    _stage(
        db, "SELECT shop_id,goods_id FROM table_rows WHERE shop_id=? AND cache_key=? AND goods_id<>''", (shop_id, key)
    )
    db.execute(
        "INSERT OR REPLACE INTO table_snapshots(shop_id,cache_key,updated,metadata,row_count) VALUES(?,?,?,?,?)",
        (shop_id, key, updated, json.dumps(metadata, ensure_ascii=False), len(rows)),
    )
    if unchanged:
        _resolve_names(db)
        _watermark(db, updated)
        return
    # Keep unchanged projection rows in place; trim only removed tail rows.
    db.execute("DELETE FROM table_rows WHERE shop_id=? AND cache_key=? AND ordinal>=?", (shop_id, key, len(rows)))

    def values():
        for ordinal, original in enumerate(rows):
            row = decorate(kind, original, platform)
            if kind == "ad_products" and platform == "pdd":
                from .promotion_status import normalize

                row = normalize(row)
            if kind == "activities":
                from .activities import groups

                row = {**row, "activity_groups": row.get("activity_groups", groups(row))}
            yield (
                shop_id,
                key,
                ordinal,
                json.dumps(row, ensure_ascii=False),
                row.get("status") or "",
                row.get("created_at") or "",
                row.get("priority", 0),
                row.get("name") or "",
                str(row.get("goods_id") or ""),
                row.get("sales"),
                row.get("visitors"),
                row.get("amount"),
                row.get("stock"),
                " ".join(str(row.get(k, "")) for k in SEARCH).casefold(),
                thumbnail(row),
            )

    db.executemany(
        "INSERT INTO table_rows(shop_id,cache_key,ordinal,payload,status,created_at,priority,name,goods_id,sales,visitors,amount,stock,search_text,thumbnail_url) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(shop_id,cache_key,ordinal) DO UPDATE SET payload=excluded.payload,status=excluded.status,created_at=excluded.created_at,priority=excluded.priority,name=excluded.name,goods_id=excluded.goods_id,sales=excluded.sales,visitors=excluded.visitors,amount=excluded.amount,stock=excluded.stock,search_text=excluded.search_text,thumbnail_url=excluded.thumbnail_url WHERE table_rows.payload IS NOT excluded.payload OR table_rows.status IS NOT excluded.status OR table_rows.created_at IS NOT excluded.created_at OR table_rows.priority IS NOT excluded.priority OR table_rows.name IS NOT excluded.name OR table_rows.goods_id IS NOT excluded.goods_id OR table_rows.sales IS NOT excluded.sales OR table_rows.visitors IS NOT excluded.visitors OR table_rows.amount IS NOT excluded.amount OR table_rows.stock IS NOT excluded.stock OR table_rows.search_text IS NOT excluded.search_text OR table_rows.thumbnail_url IS NOT excluded.thumbnail_url",
        values(),
    )
    _stage(
        db, "SELECT shop_id,goods_id FROM table_rows WHERE shop_id=? AND cache_key=? AND goods_id<>''", (shop_id, key)
    )
    _resolve_names(db)
    _watermark(db, updated)


def prune(db, shop_id=None):
    args = [] if shop_id is None else [shop_id]
    scope = "" if shop_id is None else "s.shop_id=? AND "
    missing = (
        "SELECT s.shop_id,s.cache_key FROM table_snapshots s WHERE "
        + scope
        + "NOT EXISTS(SELECT 1 FROM business_cache c WHERE c.shop_id=s.shop_id AND c.cache_key=s.cache_key) AND NOT EXISTS(SELECT 1 FROM daily_archive a WHERE a.shop_id=s.shop_id AND a.cache_key=s.cache_key)"
    )
    db.execute("DELETE FROM table_rows WHERE (shop_id,cache_key) IN (" + missing + ")", args)
    db.execute("DELETE FROM table_snapshots WHERE (shop_id,cache_key) IN (" + missing + ")", args)
    _stage(
        db,
        "SELECT n.shop_id,n.goods_id FROM table_names n WHERE "
        + ("n.shop_id=? AND " if shop_id is not None else "")
        + "NOT EXISTS(SELECT 1 FROM table_snapshots t WHERE t.shop_id=n.shop_id AND t.cache_key=n.cache_key)",
        args,
    )
    _resolve_names(db)


def prune_batch(db, shop_id, snapshots=5, rows=2000):
    """Remove a bounded slice of orphaned projection rows; True while work remains.

    The single-statement prune() has to finish inside one short maintenance
    transaction; with many old rows it was always interrupted and rolled back, so
    nothing was ever removed. Each slice here is small and committed by the caller.
    """
    orphans = db.execute(
        "SELECT s.cache_key FROM table_snapshots s WHERE s.shop_id=? AND NOT EXISTS(SELECT 1 FROM business_cache c WHERE c.shop_id=s.shop_id AND c.cache_key=s.cache_key) AND NOT EXISTS(SELECT 1 FROM daily_archive a WHERE a.shop_id=s.shop_id AND a.cache_key=s.cache_key) LIMIT ?",
        (shop_id, snapshots),
    ).fetchall()
    budget = rows
    finished = []
    for (key,) in orphans:
        deleted = db.execute(
            "DELETE FROM table_rows WHERE rowid IN (SELECT rowid FROM table_rows WHERE shop_id=? AND cache_key=? LIMIT ?)",
            (shop_id, key, budget),
        ).rowcount
        budget -= deleted
        if budget <= 0:
            return True
        finished.append(key)
    for key in finished:
        db.execute("DELETE FROM table_snapshots WHERE shop_id=? AND cache_key=?", (shop_id, key))
        _stage(
            db,
            "SELECT n.shop_id,n.goods_id FROM table_names n WHERE n.shop_id=? AND n.cache_key=?",
            (shop_id, key),
        )
    if finished:
        _resolve_names(db)
    return len(orphans) == snapshots


def remove_shop(db, shop_id):
    db.execute("DELETE FROM table_rows WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM table_snapshots WHERE shop_id=?", (shop_id,))
    db.execute("DELETE FROM table_names WHERE shop_id=?", (shop_id,))


def metadata(store, shop_id, key):
    with store.connect() as db:
        row = db.execute("SELECT * FROM table_snapshots WHERE shop_id=? AND cache_key=?", (shop_id, key)).fetchone()
    return (
        {"updated": row["updated"], "payload": {**json.loads(row["metadata"]), "_row_count": row["row_count"]}}
        if row
        else None
    )


def query(store, selections, kind, **options):
    with store.connect() as db:
        db.execute("BEGIN")
        return _query(store, selections, kind, _db=db, **options)


def _query(
    store,
    selections,
    kind,
    *,
    _db=None,
    keyword="",
    status="",
    stars=(),
    reply="",
    date_from="",
    date_to="",
    min_price=None,
    max_price=None,
    low_stock=None,
    priority_only=False,
    carrier="",
    activity_type="",
    brand_reason="",
    activity_group="",
    sort_by="",
    sort_direction="desc",
    page=1,
    page_size=20,
    export=False,
    include_focus=True,
    count_only=False,
):
    if not selections:
        return {"rows": [], "total": 0, "priority_count": 0, "statuses": [], "activity_types": [], "page_count": 1}
    if sort_direction not in ("asc", "desc") or (sort_by and not re.fullmatch(r"[a-z_][a-z0-9_]{0,59}", sort_by)):
        raise ValueError("排序条件无效")
    companies = {}
    if kind == "invoices":
        from .shop_entities import company_names

        companies = company_names(store)
    args = []
    for shop, key in selections:
        operators = "、".join(shop.get("operators") or []) or shop.get("operator", "")
        args.extend((shop["id"], key, shop["name"], companies.get(shop["id"], ""), operators, operators.casefold()))
    base = (
        "WITH selected(shop_id,cache_key,shop_name,company_name,operators,operators_key) AS (VALUES "
        + ",".join("(?,?,?,?,?,?)" for _ in selections)
        + ") "
    )
    base += "FROM table_rows r JOIN selected s ON r.shop_id=s.shop_id AND r.cache_key=s.cache_key "
    # Separately keep the CTE since SELECT must precede FROM.
    cte, relation = base.split("FROM table_rows", 1)
    relation = "FROM table_rows" + relation
    selected_cte, selected_args = cte, list(args)
    from .priority_rules import KINDS as FOCUS_KINDS

    selection_alias = "s"
    if kind in FOCUS_KINDS and include_focus:
        # Most focus IDs already exist in the full projection. Only build the
        # JSON union when this same read snapshot contains genuinely new IDs.
        include_focus = bool(
            _db.execute(
                cte
                + """SELECT 1 FROM attention_rows a JOIN selected s ON s.shop_id=a.shop_id
          JOIN shops p ON p.id=a.shop_id AND p.platform='pdd' WHERE a.kind=? AND NOT EXISTS(
          SELECT 1 FROM table_rows r WHERE r.shop_id=a.shop_id AND r.cache_key=s.cache_key
          AND CAST(json_extract(r.payload,'$.id') AS TEXT)=a.id) LIMIT 1""",
                args + [kind],
            ).fetchone()
        )
    if kind in FOCUS_KINDS and include_focus:
        # A 60s focus read can discover orders before the slower full list does.
        # Union only missing IDs; apply the same UI filters below to both sources.
        def cell(field):
            return "json_extract(a.payload,'$." + field + "')"

        text = lambda field: "coalesce(" + cell(field) + ",'')"
        search = " || ' ' || ".join("coalesce(CAST(" + cell(f) + " AS TEXT),'')" for f in SEARCH)
        projected = ",".join(
            [
                "a.shop_id",
                "s.cache_key",
                "-a.rowid",
                "a.payload",
                text("status"),
                text("created_at"),
                "a.priority",
                text("name"),
                text("goods_id"),
                cell("sales"),
                cell("visitors"),
                cell("amount"),
                cell("stock"),
                "casefold(" + search + ")",
                text("thumbnail_url"),
            ]
        )
        cte = (
            cte.rstrip()
            + ", resolved_rows AS (SELECT r.*,x.shop_name,x.company_name,x.operators,x.operators_key FROM table_rows r JOIN selected x ON r.shop_id=x.shop_id AND r.cache_key=x.cache_key UNION ALL SELECT "
            + projected
            + """,s.shop_name,s.company_name,s.operators,s.operators_key FROM attention_rows a JOIN selected s ON a.shop_id=s.shop_id
            JOIN shops p ON p.id=a.shop_id AND p.platform='pdd' WHERE a.kind=? AND NOT EXISTS(
            SELECT 1 FROM table_rows r WHERE r.shop_id=a.shop_id AND r.cache_key=s.cache_key AND CAST(json_extract(r.payload,'$.id') AS TEXT)=a.id)) """
        )
        args.append(kind)
        # Selected identity is already part of each branch. A second join would
        # materialize and index tens of thousands of large JSON payloads.
        relation = "FROM resolved_rows r "
        selection_alias = "r"

    def expr(field):
        if field == "operators":
            return selection_alias + ".operators"
        if field == "company_name":
            return selection_alias + ".company_name"
        if field == "name":
            return "coalesce(nullif(r.name,''),(SELECT n.name FROM table_names n WHERE n.shop_id=r.shop_id AND n.goods_id=r.goods_id),'')"
        return (
            selection_alias + ".shop_name"
            if field == "shop_name"
            else "r." + field
            if field in COLUMNS
            else "json_extract(r.payload,'$." + field + "')"
        )

    clauses = []
    params = []

    def add(sql, *values):
        clauses.append(sql)
        params.extend(values)

    if keyword:
        add(
            "(instr(r.search_text,?)>0 OR instr("
            + selection_alias
            + ".operators_key,?)>0 OR (r.name='' AND instr(casefold("
            + expr("name")
            + "),?)>0))",
            keyword.casefold(),
            keyword.casefold(),
            keyword.casefold(),
        )
    if status:
        add("(r.status=? OR json_extract(r.payload,'$.attention_status')=?)", status, status)
    if kind == "orders" and status in ("待付款", "待发货", "待收货"):
        add("coalesce(json_extract(r.payload,'$._order_status_stale'),0)=0")
    if kind == "orders" and status == "即将逾期发货":
        add(
            "instr(r.status,'待发货')>0 AND instr(r.status,'已发货')=0 AND instr(r.status,'已取消')=0 AND instr(r.status,'已关闭')=0"
        )
    if kind == "aftersales" and status == "待处理即将逾期":
        from .aftersales_warning import ENDED

        actual = "coalesce(nullif(json_extract(r.payload,'$.progress'),''),r.status)"
        add("instr(" + actual + ",'待商家')>0")
        for ended in ENDED:
            add("instr(" + actual + ",?)=0", ended)
    if priority_only:
        add("r.priority>0")
    if carrier:
        add("instr(casefold(coalesce(json_extract(r.payload,'$.carrier'),'')),?)>0", carrier.casefold())
    if kind == "activities" and activity_type:
        add(expr("activity_type") + "=?", activity_type)
    if kind == "brands" and brand_reason:
        add("instr('；'||coalesce(json_extract(r.payload,'$.problem'),'')||'；',?)>0", "；" + brand_reason + "；")
    if kind == "activities" and activity_group:
        add(
            "EXISTS(SELECT 1 FROM json_each(json_extract(r.payload,'$.activity_groups')) WHERE value=?)", activity_group
        )
    if kind == "reviews":
        if stars:
            add(expr("stars") + " IN (" + ",".join("?" for _ in stars) + ")", *stars)
        if reply:
            add(expr("replied") + "=?", int(reply == "replied"))
    if date_from:
        add("r.created_at<>'' AND r.created_at>=?", date_from)
    if date_to:
        add("r.created_at<>'' AND r.created_at<=?", date_to + "\uffff")
    if min_price is not None:
        add(expr("price_max") + ">=?", min_price)
    if max_price is not None:
        add(expr("price_min") + "<=?", max_price)
    if low_stock is not None:
        add("r.stock<=?", low_stock)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    order = "r.priority DESC,CASE WHEN r.priority>1 THEN coalesce(nullif(json_extract(r.payload,'$.deadline'),''),'9999') ELSE '9999' END ASC,r.created_at DESC,CAST(json_extract(r.payload,'$.id') AS TEXT) DESC,r.shop_id,r.ordinal"
    if sort_by:
        value = expr(sort_by)
        direction = sort_direction.upper()
        if sort_by in COLUMNS and sort_by != "name":
            order = f"({value} IS NULL) ASC,{value} {direction}," + order
        else:
            order = (
                f"({value} IS NULL OR {value}='') ASC,CASE WHEN typeof({value}) IN ('integer','real') THEN 0 ELSE 1 END {direction},CASE WHEN typeof({value}) IN ('integer','real') THEN {value} ELSE casefold(CAST({value} AS TEXT)) END {direction},"
                + order
            )
    from .priority_rules import KINDS as FOCUS_KINDS

    if kind in FOCUS_KINDS:
        order = "(r.priority>0) DESC," + order
    with nullcontext(_db) as db:
        db.create_function("casefold", 1, lambda s: str(s or "").casefold(), deterministic=True)
        # Count, rows and focus-presence checks share the caller's read snapshot.
        if count_only:
            from .home_counts import source_fields
            from .business_reader import BEIJING
            from datetime import datetime

            snapshots = {}
            for row in db.execute(
                selected_cte
                + "SELECT t.shop_id,t.updated,t.metadata FROM table_snapshots t JOIN selected s ON t.shop_id=s.shop_id AND t.cache_key=s.cache_key",
                selected_args,
            ):
                payload = json.loads(row["metadata"])
                snapshots[row["shop_id"]] = {
                    "complete": bool(payload.get("complete")),
                    "updated_at": datetime.fromtimestamp(row["updated"], BEIJING).isoformat(),
                    **source_fields(payload),
                }
            groups = db.execute(
                cte
                + "SELECT r.shop_id,count(*),coalesce(sum(r.priority>0),0) "
                + relation
                + where
                + " GROUP BY r.shop_id",
                args + params,
            ).fetchall()
            total = sum(row[1] for row in groups)
            return {
                "rows": [],
                "total": total,
                "priority_count": sum(row[2] for row in groups),
                "shop_counts": {row[0]: row[1] for row in groups},
                "count_sources": snapshots,
                "statuses": [],
                "page_count": max(1, (total + page_size - 1) // page_size),
            }
        counts = db.execute(
            cte + "SELECT count(*),coalesce(sum(r.priority>0),0) " + relation + where, args + params
        ).fetchone()
        statuses = [
            r[0]
            for r in db.execute(
                cte + "SELECT DISTINCT r.status " + relation + " WHERE r.status<>'' ORDER BY r.status", args
            )
        ]
        reasons = (
            sorted(
                {
                    v
                    for r in db.execute(cte + "SELECT DISTINCT json_extract(r.payload,'$.problem') " + relation, args)
                    for v in str(r[0] or "").split("；")
                    if v
                }
            )
            if kind == "brands"
            else []
        )
        types = (
            [
                r[0]
                for r in db.execute(cte + "SELECT DISTINCT json_extract(r.payload,'$.activity_type') " + relation, args)
            ]
            if kind == "activities"
            else []
        )
        sql = cte + "SELECT r.shop_id,r.payload " + relation + where + " ORDER BY " + order
        bound = args + params
        page = max(1, min(page, max(1, (counts[0] + page_size - 1) // page_size)))
        if not export:
            sql += " LIMIT ? OFFSET ?"
            bound += [page_size, (page - 1) * page_size]
        records = db.execute(sql, bound).fetchall()
    from .shop_identity import identity

    shops = {s["id"]: s for s, _ in selections}
    rows = []
    for record in records:
        shop = shops[record["shop_id"]]
        rows.append(
            {
                **json.loads(record["payload"]),
                **identity(shop),
                "shop_id": shop["id"],
                "shop_name": shop["name"],
                "operator": shop.get("operator", ""),
            }
        )
    if kind == "aftersales":
        from .aftersales_warning import actual_status

        rows = [actual_status(row) for row in rows]
    if kind == "invoices":
        rows = [{**r, "company_name": companies.get(r["shop_id"], "")} for r in rows]
    if kind == "packages":
        from .package_timing import fields

        rows = [{**row, **fields(row, remaining=True)} for row in rows]
    # Resolve only the visible products; never deserialize all shops' image catalogs per sort.
    from .product_images import enrich, thumbnail

    for shop_id in {r["shop_id"] for r in rows}:
        subset = [r for r in rows if r["shop_id"] == shop_id]
        missing = {r.get("goods_id") for r in subset if r.get("goods_id") and (not thumbnail(r) or not r.get("name"))}
        catalog = store.product_images(shop_id, missing) if missing else {}
        enriched = iter(enrich(subset, catalog))
        rows = [next(enriched) if r["shop_id"] == shop_id else r for r in rows]
    return {
        "page": page,
        "rows": rows,
        "total": counts[0],
        "priority_count": counts[1],
        "statuses": statuses,
        "activity_types": [t for t in types if t],
        "brand_reasons": reasons,
        "page_count": max(1, (counts[0] + page_size - 1) // page_size),
    }
