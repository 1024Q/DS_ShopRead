"""Small durable operational projections; reads never enqueue browser work."""

import json, time, uuid, math
from datetime import datetime, timedelta
from .business_reader import BEIJING
from .shop_identity import identity
from .partition_reads import rows as partition_rows


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS collection_runs(
            id INTEGER PRIMARY KEY,shop_id INTEGER,kind TEXT,finished REAL,seconds REAL,success INTEGER,error_kind TEXT);
            CREATE INDEX IF NOT EXISTS collection_runs_recent ON collection_runs(finished,shop_id);
            CREATE TABLE IF NOT EXISTS home_history(shop_id INTEGER,profile_version INTEGER,day TEXT,kind TEXT,
                payload TEXT,updated REAL,started REAL,PRIMARY KEY(shop_id,profile_version,day,kind));
            CREATE TABLE IF NOT EXISTS attention_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,shop_id INTEGER,kind TEXT,
                record_id TEXT,change TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS shop_priority(shop_id INTEGER PRIMARY KEY,until_time REAL);
        """)
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('insights_identity',?)", (str(uuid.uuid4()),))
        if "context" not in {r["name"] for r in db.execute("PRAGMA table_info(attention_changes)")}:
            db.execute("ALTER TABLE attention_changes ADD COLUMN context TEXT NOT NULL DEFAULT '{}'")


def record_run(store, shop_id, kind, started, success, error_kind=""):
    with store.connect() as db:
        db.execute(
            "INSERT INTO collection_runs(shop_id,kind,finished,seconds,success,error_kind) VALUES(?,?,?,?,?,?)",
            (shop_id, kind, time.time(), round(max(0, time.time() - started), 1), int(success), error_kind),
        )


def archive_home(db, shop_id, kind, payload, updated):
    if kind not in ("overview", "transactions", "ad_reports"):
        return
    scope = payload.get("_sync", {}).get("scope", {})
    if scope.get("_exact_dates") or scope.get("trade_range", "realtime") != "realtime":
        return
    started = payload.get("_sync", {}).get("snapshot_started_at", updated)
    day = datetime.fromtimestamp(started, BEIJING).date().isoformat()
    shop = db.execute("SELECT profile_version FROM shops WHERE id=?", (shop_id,)).fetchone()
    if not shop:
        return
    slim = {"metrics": payload.get("metrics", {}), "complete": bool(payload.get("complete"))}
    old = db.execute(
        "SELECT payload,started FROM home_history WHERE shop_id=? AND profile_version=? AND day=? AND kind=?",
        (shop_id, shop[0], day, kind),
    ).fetchone()
    if old and (old["started"] > started or json.loads(old["payload"]).get("complete") and not slim["complete"]):
        return
    encoded = json.dumps(slim, ensure_ascii=False)
    if old and old["payload"] == encoded:
        # Unchanged daily metrics: skip the rewrite and the auxiliary re-upload
        # it would trigger. The next real change replaces it with new stamps.
        return
    db.execute(
        "INSERT OR REPLACE INTO home_history(shop_id,profile_version,day,kind,payload,updated,started) VALUES(?,?,?,?,?,?,?)",
        (shop_id, shop[0], day, kind, encoded, updated, started),
    )


def attention_changed(db, shop_id, kind, rows):
    # Initial import establishes a baseline; only subsequent changes notify.
    if not db.execute("SELECT 1 FROM attention_snapshots WHERE shop_id=? AND kind=?", (shop_id, kind)).fetchone():
        return
    before = {
        r["id"]: json.loads(r["payload"])
        for r in db.execute("SELECT id,payload FROM attention_rows WHERE shop_id=? AND kind=?", (shop_id, kind))
    }
    after = {str(r["id"]): r for r in rows}
    signature = lambda r: tuple(r.get(k) for k in ("status", "attention_status", "deadline", "priority_reason"))
    for key in before.keys() | after.keys():
        change = (
            "new"
            if key not in before
            else "resolved"
            if key not in after
            else "changed"
            if signature(before[key]) != signature(after[key])
            else None
        )
        if change:
            row = after.get(key, before.get(key, {}))
            context = {
                k: row.get(k) for k in ("order_number", "tracking_number", "deadline", "status", "attention_status")
            }
            db.execute(
                "INSERT INTO attention_changes(shop_id,kind,record_id,change,created,context) VALUES(?,?,?,?,?,?)",
                (shop_id, kind, key, change, time.time(), json.dumps(context, ensure_ascii=False)),
            )


def notification_history(
    store,
    before=0,
    change="",
    shop_id=None,
    platform="",
    kind="",
    keyword="",
    page=1,
    sort_by="",
    sort_direction="desc",
):
    from .filter_scope import sql_filter

    where = []
    args = []
    if before:
        where.append("a.id<?")
        args.append(before)
    if change:
        where.append("a.change=?")
        args.append(change)
    if shop_id:
        where.append(sql_filter("a.shop_id", str(shop_id), args))
    if platform:
        where.append(sql_filter("s.platform", platform, args))
    if kind:
        where.append("a.kind=?")
        args.append(kind)
    if keyword:
        where.append(
            "(instr(a.record_id,?)>0 OR instr(a.context,?)>0 OR EXISTS(SELECT 1 FROM shop_operators o WHERE o.shop_id=a.shop_id AND instr(lower(o.name),lower(?))>0))"
        )
        args.extend([keyword, keyword, keyword])
    clause = " WHERE " + " AND ".join(where) if where else ""
    join = " FROM attention_changes a JOIN shops s ON s.id=a.shop_id"
    fields = {
        "created": "a.created",
        "shop_name": "coalesce(nullif(s.alias,''),s.name)",
        "change": "a.change",
        "order_number": "coalesce(json_extract(a.context,'$.order_number'),a.record_id)",
        "operators": "(SELECT group_concat(name,',') FROM shop_operators WHERE shop_id=a.shop_id)",
    }
    order = fields.get(sort_by, "a.id") + (" ASC" if sort_direction == "asc" else " DESC") + ",a.id DESC"
    with store.connect() as db:
        total = db.execute("SELECT count(*)" + join + clause, args).fetchone()[0]
        pages = max(1, (total + 49) // 50)
        page = min(max(page, 1), pages)
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT a.*,s.platform,CASE WHEN s.alias<>'' THEN s.alias ELSE s.name END AS shop_name"
                + join
                + clause
                + " ORDER BY "
                + order
                + " LIMIT 51 OFFSET ?",
                args + [(page - 1) * 50],
            )
        ]
    return {
        "rows": [{**r, **identity(r), "context": json.loads(r["context"])} for r in rows[:50]],
        "next_before": rows[49]["id"] if len(rows) > 50 else None,
        "total": total,
        "page": page,
        "page_count": pages,
    }


def notifications(store, after=None):
    with store.connect() as db:
        latest = db.execute("SELECT coalesce(max(id),0) FROM attention_changes").fetchone()[0]
        machine = db.execute("SELECT value FROM settings WHERE key='insights_identity'").fetchone()[0]
        rows = (
            []
            if after is None
            else [
                dict(r)
                for r in db.execute(
                    "SELECT a.*,s.platform,CASE WHEN s.alias<>'' THEN s.alias ELSE s.name END AS shop_name FROM attention_changes a JOIN shops s ON s.id=a.shop_id WHERE a.id>? ORDER BY a.id LIMIT 100",
                    (after,),
                )
            ]
        )
    return {
        "rows": [{**r, **identity(r)} for r in rows],
        "cursor": rows[-1]["id"] if rows else latest,
        "machine": machine,
        "latest": latest,
    }


def history(store, days=7, platform="", shop_id=None, comparison="strict"):
    from .platform_support import supports

    today = datetime.now(BEIJING).date()
    start = today - timedelta(days=days * 2)
    shops = [
        s
        for s in store.list()
        if supports(s["platform"], "overview")
        and (not platform or s["platform"] == platform)
        and (shop_id is None or s["id"] == shop_id)
    ]
    raw = {
        (r["shop_id"], r["day"]): json.loads(r["payload"])
        for r in partition_rows(
            store,
            "SELECT h.* FROM home_history h JOIN shops s ON h.shop_id=s.id AND h.profile_version=s.profile_version WHERE h.kind='overview' AND day>=? AND day<?",
            (start.isoformat(), today.isoformat()),
            [s["id"] for s in shops],
        )
    }
    eligible = len(shops)
    if comparison == "comparable":

        def valid(shop):
            for i in range(days * 2):
                value = raw.get((shop["id"], (start + timedelta(days=i)).isoformat()), {})
                sales = value.get("metrics", {}).get("sales")
                if not value.get("complete") or type(sales) not in (int, float) or not math.isfinite(sales):
                    return False
            return True

        shops = [s for s in shops if valid(s)]

    def period(offset):
        result = []
        for i in range(days):
            day = (today - timedelta(days=offset + days - i)).isoformat()
            snapshots = [raw.get((s["id"], day)) for s in shops]
            values = [s.get("metrics", {}).get("sales") for s in snapshots if s and s.get("complete")]
            values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
            result.append(
                {
                    "day": day,
                    "sales": sum(values) if values else None,
                    "known": len(values),
                    "total": len(shops),
                    "missing": sum(s is None for s in snapshots),
                    "partial": sum(bool(s and not s.get("complete")) for s in snapshots),
                }
            )
        return result

    current = period(0)
    previous = period(days)
    complete = lambda rows: bool(shops) and all(r["known"] == r["total"] for r in rows)
    a = sum(r["sales"] or 0 for r in current) if complete(current) else None
    b = sum(r["sales"] or 0 for r in previous) if complete(previous) else None
    return {
        "rows": current,
        "previous": previous,
        "days": days,
        "total": a,
        "previous_total": b,
        "change_percent": round((a - b) / b * 100, 2) if a is not None and b else None,
        "comparison": comparison,
        "participating_shops": len(shops),
        "eligible_shops": eligible,
        "note": "截至昨日的每日最后成功快照；不是平台日结。缺失不补零，完整可比周期才计算变化。新启用前没有保存的日期不回填。",
    }


def health(service):
    now = time.time()
    stats = {
        r["shop_id"]: dict(r)
        for r in partition_rows(
            service.store,
            "SELECT shop_id,count(*) AS runs,sum(success) AS succeeded,avg(seconds) AS seconds,max(finished) AS last_finished FROM collection_runs WHERE finished>? GROUP BY shop_id",
            (now - 86400,),
        )
    }
    failures = {
        r["shop_id"]: dict(r)
        for r in partition_rows(
            service.store,
            "SELECT shop_id,count(*) AS modules,max(failures) AS consecutive FROM collection_tasks WHERE schedule_state<>'archived' AND last_error<>'' GROUP BY shop_id",
        )
    }
    priorities = {
        r["shop_id"]: r["until_time"]
        for r in partition_rows(service.store, "SELECT * FROM shop_priority WHERE until_time>?", (now,))
    }
    rows = []
    for shop in service.store.list():
        sid = shop["id"]
        s = stats.get(sid, {})
        state = service.session_getter(sid).state
        running = [m for k, m in service.progress.items() if k[0] == sid and m.get("phase") == "running"]
        elapsed = max((now - m.get("started_at", now) for m in running), default=0)
        rows.append(
            {
                **identity(shop),
                "shop_id": sid,
                "state": state,
                "runs": s.get("runs", 0),
                "success_rate": round(s["succeeded"] / s["runs"] * 100, 1) if s else None,
                "average_seconds": round(s["seconds"], 1) if s else None,
                "last_finished": s.get("last_finished"),
                "failed_modules": failures.get(sid, {}).get("modules", 0),
                "consecutive": failures.get(sid, {}).get("consecutive", 0),
                "browser_status": "响应偏慢"
                if elapsed > 120
                else "采集中"
                if running
                else "最近采集正常"
                if s and s["last_finished"] > now - 600
                else "暂无近期响应记录",
                "priority_until": priorities.get(sid),
            }
        )
    return {"rows": rows, "window_hours": 24}
