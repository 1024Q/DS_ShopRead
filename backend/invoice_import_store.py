"""Durable review queue. Files remain local until an explicit, revision-bound confirmation."""

import hashlib, json, time, uuid
from .invoice_pdf import conflicts
from .partition_reads import rows as partition_rows


def initialize(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS invoice_import_batches(id TEXT PRIMARY KEY,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS invoice_import_files(
          id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,name TEXT NOT NULL,sha TEXT NOT NULL,content BLOB NOT NULL,
          parsed TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'exception',error TEXT NOT NULL DEFAULT '',
          candidates TEXT NOT NULL DEFAULT '[]',selected TEXT NOT NULL DEFAULT '{}',revision INTEGER NOT NULL DEFAULT 1,
          created REAL NOT NULL,updated REAL NOT NULL,invoice_key TEXT NOT NULL DEFAULT '',upload_url TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS invoice_import_batch ON invoice_import_files(batch_id,created);
        CREATE INDEX IF NOT EXISTS invoice_import_sha ON invoice_import_files(sha);
        CREATE TABLE IF NOT EXISTS invoice_upload_claims(invoice_key TEXT PRIMARY KEY,order_key TEXT UNIQUE NOT NULL,file_id TEXT UNIQUE NOT NULL);
        """)


def entity(store, shop_id):
    with store.connect() as db:
        row = db.execute(
            """SELECT t.latest FROM tax_records t JOIN shops s ON s.id=t.shop_id
            WHERE t.shop_id=? AND t.profile_version=s.profile_version ORDER BY t.checked_at DESC,t.period DESC LIMIT 1""",
            (shop_id,),
        ).fetchone()
    return (json.loads(row[0]).get("entity") or {}) if row else {}


def applications(store):
    shops = {s["id"]: s for s in store.list() if s.get("platform", "pdd") == "pdd"}
    seen = set()
    result = []
    caches = sorted(
        partition_rows(
            store,
            "SELECT shop_id,payload,updated FROM business_cache WHERE cache_key LIKE 'invoices:%'",
            shop_ids=shops,
        ),
        key=lambda row: row["updated"],
        reverse=True,
    )
    for cache in caches:
        sid = cache["shop_id"]
        if sid not in shops:
            continue
        p = json.loads(cache["payload"])
        scope = p.get("_sync", {}).get("scope", {})
        if scope.get("_attention"):
            continue
        for r in p.get("rows", []):
            key = (sid, r["id"])
            if key in seen:
                continue
            seen.add(key)
            if r.get("status") != "待开票":
                continue
            result.append(
                {
                    **r,
                    "shop_id": sid,
                    "shop_name": shops[sid]["name"],
                    "profile_version": shops[sid].get("profile_version", 0),
                    "source_updated": cache["updated"],
                }
            )
    return result


COLUMNS = "id,batch_id,name,sha,parsed,state,error,candidates,selected,revision,created,updated,invoice_key,upload_url"


def get_file(store, fid, include_content=False):
    with store.connect() as db:
        r = db.execute(
            "SELECT " + COLUMNS + (",content" if include_content else "") + " FROM invoice_import_files WHERE id=?",
            (fid,),
        ).fetchone()
    if not r:
        raise ValueError("文件记录不存在")
    return dict(r)


def public(r):
    return {
        k: (json.loads(v) if k in ("parsed", "candidates", "selected") else v)
        for k, v in dict(r).items()
        if k not in ("content", "sha", "invoice_key", "upload_url")
    }


def batch(store, bid):
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM invoice_import_batches WHERE id=?", (bid,)).fetchone():
            raise ValueError("导入批次不存在")
        rows = db.execute(
            "SELECT " + COLUMNS + " FROM invoice_import_files WHERE batch_id=? ORDER BY created", (bid,)
        ).fetchall()
    return {"id": bid, "files": [public(r) for r in rows]}


def create_batch(store):
    bid = uuid.uuid4().hex
    with store.connect() as db:
        db.execute("INSERT INTO invoice_import_batches(id,created) VALUES(?,?)", (bid, time.time()))
    return bid


def match(store, fid, selection=None):
    record = get_file(store, fid)
    if record["state"] not in ("ready", "exception"):
        raise ValueError("已确认的文件不能改配")
    parsed = json.loads(record["parsed"])
    candidates = []
    near = []
    selected = {}
    error = ""
    if not parsed:
        return public(record)
    entities = {}
    for row in applications(store):
        sid = row["shop_id"]
        if sid not in entities:
            entities[sid] = entity(store, sid)
        issues = conflicts(parsed, row, entities[sid])
        candidate = {
            k: row.get(k)
            for k in (
                "shop_id",
                "shop_name",
                "profile_version",
                "id",
                "order_number",
                "invoice_title",
                "invoice_title_type",
                "amount",
                "source_updated",
            )
        }
        candidate.update(company_name=entities[sid].get("company_name", ""), issues=issues)
        if not issues:
            candidates.append(candidate)
        elif (
            row.get("order_number") in parsed.get("order_numbers", [])
            or row.get("invoice_title") == parsed["buyer_name"]
        ):
            near.append(candidate)
    with store.connect() as db:
        dup = db.execute(
            "SELECT id FROM invoice_import_files WHERE id<>? AND created<? AND (sha=? OR invoice_key=?) AND state NOT IN ('cancelled') LIMIT 1",
            (fid, record["created"], record["sha"], record["invoice_key"]),
        ).fetchone()
        claimed = db.execute(
            "SELECT file_id FROM invoice_upload_claims WHERE invoice_key=?", (record["invoice_key"],)
        ).fetchone()
        issued = db.execute(
            "SELECT DISTINCT shop_id FROM table_rows WHERE cache_key LIKE 'invoices:%' AND json_extract(payload,'$.invoice_number')=?",
            (parsed["invoice_number"],),
        ).fetchall()
    existing = any(
        (entity(store, r["shop_id"]).get("credit_code") or entity(store, r["shop_id"]).get("taxpayer_number"))
        == parsed["seller_tax"]
        for r in issued
    )
    if dup or claimed or existing:
        error = "文件或发票号码已导入或已用于开票，请核对原记录，避免重复上传"
    elif selection:
        selected = next(
            (r for r in candidates if r["shop_id"] == selection["shop_id"] and r["id"] == selection["id"]), {}
        )
        if not selected:
            raise ValueError("所选订单校验未通过，请重新匹配")
    elif len(candidates) == 1:
        selected = candidates[0]
    elif len(candidates) > 1:
        error = "匹配到多个订单，请核对 PDF 并选择对应申请"
    else:
        error = "未找到信息一致的待开票申请，请同步未开票数据后重新匹配"
    state = "ready" if selected and not error else "exception"
    with store.connect() as db:
        db.execute(
            "UPDATE invoice_import_files SET state=?,error=?,candidates=?,selected=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
            (
                state,
                error,
                json.dumps(candidates if candidates else near[:20], ensure_ascii=False),
                json.dumps(selected, ensure_ascii=False),
                time.time(),
                fid,
                record["revision"],
            ),
        )
    return public(get_file(store, fid))


def add_file(store, bid, name, content, parsed, error=""):
    batch(store, bid)
    with store.connect() as db:
        if db.execute("SELECT count(*) FROM invoice_import_files WHERE batch_id=?", (bid,)).fetchone()[0] >= 100:
            raise ValueError("每批最多 100 个 PDF")
    fid = uuid.uuid4().hex
    now = time.time()
    sha = hashlib.sha256(content).hexdigest()
    key = (parsed["seller_tax"] + ":" + parsed["invoice_number"]) if parsed else ""
    with store.connect() as db:
        db.execute(
            "INSERT INTO invoice_import_files(id,batch_id,name,sha,content,parsed,error,created,updated,invoice_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fid, bid, name[:200], sha, content, json.dumps(parsed, ensure_ascii=False), error, now, now, key),
        )
    return match(store, fid) if parsed else public(get_file(store, fid))


def confirm(store, bid, items):
    if not items or len(items) > 100 or len({i["id"] for i in items}) != len(items):
        raise ValueError("请选择不重复的待确认文件")
    records = []
    for item in items:
        r = get_file(store, item["id"])
        if r["batch_id"] != bid or r["revision"] != item["revision"] or r["state"] != "ready":
            raise ValueError("匹配结果已变化，请刷新并重新确认")
        records.append(r)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for r in records:
            selection = json.loads(r["selected"])
            order_key = f"{selection['shop_id']}:{selection['profile_version']}:{selection['id']}"
            try:
                db.execute(
                    "INSERT INTO invoice_upload_claims(invoice_key,order_key,file_id) VALUES(?,?,?)",
                    (r["invoice_key"], order_key, r["id"]),
                )
            except Exception:
                raise ValueError("所选文件含重复发票或重复订单，或该订单已在上传队列中")
            changed = db.execute(
                "UPDATE invoice_import_files SET state='queued',error='',revision=revision+1,updated=? WHERE id=? AND revision=? AND state='ready'",
                (time.time(), r["id"], r["revision"]),
            ).rowcount
            if changed != 1:
                raise ValueError("匹配结果已变化，请重新确认")
    return batch(store, bid)


def update(store, fid, state, error="", **values):
    allowed = {"upload_url"}
    if set(values) - allowed:
        raise ValueError("不支持的字段")
    with store.connect() as db:
        assignments = "".join(", " + k + "=?" for k in values)
        db.execute(
            "UPDATE invoice_import_files SET state=?,error=?,updated=?,revision=revision+1"
            + assignments
            + " WHERE id=?",
            (state, error[:500], time.time(), *values.values(), fid),
        )
