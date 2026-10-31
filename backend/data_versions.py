"""Transactional, cross-process data revisions; no browser work on the read path."""

import time
from .filter_scope import sql_filter

from .priority_rules import KINDS as FOCUS


def initialize(db):
    db.executescript("""CREATE TABLE IF NOT EXISTS data_changes (
        version INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER NOT NULL, kind TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS data_versions (
        shop_id INTEGER NOT NULL,kind TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(shop_id,kind));""")


def publish(db, shop_id=0, kind="*"):
    if hasattr(db, "manager"):
        version = max(
            time.time_ns() // 1_000_000 * 1024 + (int(shop_id) % 1024),
            db.execute(
                "SELECT coalesce(max(version),0)+1024 FROM data_versions WHERE shop_id=?", (shop_id,)
            ).fetchone()[0],
        )
        db.execute("INSERT INTO data_changes(version,shop_id,kind) VALUES(?,?,?)", (version, shop_id, kind))
    else:
        version = db.execute("INSERT INTO data_changes(shop_id,kind) VALUES(?,?)", (shop_id, kind)).lastrowid
    db.execute("INSERT OR REPLACE INTO data_versions(shop_id,kind,version) VALUES(?,?,?)", (shop_id, kind, version))
    db.execute(
        "DELETE FROM data_changes WHERE shop_id=? AND version NOT IN (SELECT version FROM data_changes WHERE shop_id=? ORDER BY version DESC LIMIT 1024)",
        (shop_id, shop_id),
    )
    return version


def changes(store, after=None):
    with store.connect() as db:
        db.execute("BEGIN")
        version = db.execute("SELECT coalesce(max(version),0) FROM data_versions").fetchone()[0]
        rows = db.execute(
            "SELECT c.*,s.platform FROM data_changes c LEFT JOIN shops s ON s.id=c.shop_id WHERE version>? ORDER BY version",
            (after or 0,),
        ).fetchall()
    reset = (
        after is None
        or (rows and rows[0]["version"] > after + 1 and version < 10**15)
        or (after is not None and after > version)
    )
    return {"version": version, "reset": bool(reset), "changes": [dict(r) for r in rows] if not reset else []}


def dependencies(path):
    if path in ("/api/todos", "/api/attention"):
        return FOCUS
    if path.startswith("/api/data/"):
        kind = path.rsplit("/", 1)[-1]
        from .business import KINDS

        if kind not in KINDS:
            return None
        if kind == "overview":
            return {"overview", "transactions"}
        if kind == "invoices":
            return {"invoices", "tax", "products", "goodsdata"}
        from .product_images import KINDS as IMAGE_KINDS

        return {kind, "products", "goodsdata"} if kind in IMAGE_KINDS else {kind}
    return None


def revision(store, path, query):
    kinds = dependencies(path)
    with store.connect() as db:
        if kinds is None:
            return db.execute("SELECT coalesce(max(version),0) FROM data_versions").fetchone()[0]
        where = ["(v.kind='*' OR v.kind IN (" + ",".join("?" for _ in kinds) + "))"]
        args = list(sorted(kinds))
        sid = query.get("shop_id")
        platform = query.get("platform")
        if sid:
            where.append("(v.shop_id=0 OR " + sql_filter("v.shop_id", sid, args) + ")")
        if platform:
            where.append("(v.shop_id=0 OR " + sql_filter("s.platform", platform, args) + ")")
        return db.execute(
            "SELECT coalesce(max(v.version),0) FROM data_versions v LEFT JOIN shops s ON s.id=v.shop_id WHERE "
            + " AND ".join(where),
            args,
        ).fetchone()[0]
