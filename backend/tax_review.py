"""Append-only tax snapshots and revision-bound review notes."""

import hashlib
import json
import time


def revision(row):
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def append_snapshot(db, shop_id, version, period, row, at=None):
    db.execute(
        "INSERT INTO tax_history(shop_id,profile_version,period,revision,created,payload) VALUES(?,?,?,?,?,?)",
        (shop_id, version, period, revision(row), at or time.time(), json.dumps(row, ensure_ascii=False)),
    )


def initialize(store):
    with store.connect() as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS tax_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER,profile_version INTEGER,period TEXT,revision TEXT,created REAL,payload TEXT);
            CREATE INDEX IF NOT EXISTS tax_history_lookup ON tax_history(shop_id,profile_version,period,id);
            CREATE TABLE IF NOT EXISTS tax_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,shop_id INTEGER,
            profile_version INTEGER,period TEXT,revision TEXT,confirmed INTEGER,note TEXT,created REAL);
            CREATE INDEX IF NOT EXISTS tax_review_lookup ON tax_reviews(shop_id,profile_version,period,revision,id);""")
        if not db.execute("SELECT 1 FROM settings WHERE key='tax_history_migrated'").fetchone():
            for r in db.execute("SELECT * FROM tax_records").fetchall():
                before, after = json.loads(r["baseline"]), json.loads(r["latest"])
                append_snapshot(db, r["shop_id"], r["profile_version"], r["period"], before, r["baseline_at"])
                if revision(before) != revision(after):
                    append_snapshot(db, r["shop_id"], r["profile_version"], r["period"], after, r["checked_at"])
            db.execute("INSERT INTO settings(key,value) VALUES('tax_history_migrated','1')")
        if not db.execute("SELECT 1 FROM settings WHERE key='tax_single_history_v1'").fetchone():
            for r in db.execute("SELECT * FROM tax_records").fetchall():
                latest = json.loads(r["latest"])
                args = (r["shop_id"], r["profile_version"], r["period"])
                from .audit_store import differences

                history = db.execute(
                    "SELECT payload,created FROM tax_history WHERE shop_id=? AND profile_version=? AND period=? ORDER BY id DESC",
                    args,
                ).fetchall()
                previous = next(
                    (
                        h
                        for h in history
                        if not json.loads(h["payload"]).get("_missing_from_source")
                        and differences(json.loads(h["payload"]), latest)
                    ),
                    None,
                )
                raw = previous["payload"] if previous else r["latest"]
                at = previous["created"] if previous else r["checked_at"]
                db.execute(
                    "UPDATE tax_records SET baseline=?,baseline_at=? WHERE shop_id=? AND profile_version=? AND period=?",
                    (raw, time.time(), *args),
                )
                db.execute("DELETE FROM tax_history WHERE shop_id=? AND profile_version=? AND period=?", args)
                if previous:
                    append_snapshot(db, *args, json.loads(raw), at)
            db.execute("INSERT INTO settings(key,value) VALUES('tax_single_history_v1','1')")


def info(db, shop_id, version, period, row):
    rev = revision(row)
    found = db.execute(
        "SELECT confirmed,note,created FROM tax_reviews WHERE shop_id=? AND profile_version=? AND period=? AND revision=? ORDER BY id DESC LIMIT 1",
        (shop_id, version, period, rev),
    ).fetchone()
    return {
        "revision": rev,
        "confirmed": bool(found["confirmed"]) if found else False,
        "note": found["note"] if found else "",
        "reviewed_at": found["created"] if found else None,
    }


def read(store, shop_id, period):
    with store.connect() as db:
        current = db.execute(
            "SELECT t.* FROM tax_records t JOIN shops s ON s.id=t.shop_id AND s.profile_version=t.profile_version WHERE t.shop_id=? AND t.period=?",
            (shop_id, period),
        ).fetchone()
        if not current:
            raise ValueError("该店铺账期尚未保存")
        args = (shop_id, current["profile_version"], period)
        history = [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in db.execute(
                "SELECT id,revision,created,payload FROM tax_history WHERE shop_id=? AND profile_version=? AND period=? ORDER BY id DESC LIMIT 200",
                args,
            )
        ]
        reviews = [
            dict(r)
            for r in db.execute(
                "SELECT revision,confirmed,note,created FROM tax_reviews WHERE shop_id=? AND profile_version=? AND period=? ORDER BY id DESC LIMIT 200",
                args,
            )
        ]
        count = db.execute(
            "SELECT COUNT(*) FROM tax_history WHERE shop_id=? AND profile_version=? AND period=?", args
        ).fetchone()[0]
        latest = json.loads(current["latest"])
        baseline = json.loads(current["baseline"])
        from .audit_store import differences

        diff = differences(baseline, latest)
        return {
            "baseline": baseline,
            "baseline_revision": revision(baseline),
            "differences": diff,
            "can_confirm_entity": False,
            "can_overwrite_amounts": False,
            "can_overwrite": False,
            "review": info(db, *args, latest),
            "history": history,
            "history_total": count,
            "reviews": reviews,
            "latest": latest,
        }


def save(store, shop_id, period, expected, note, confirmed, overwrite=False, action="", baseline_revision=""):
    if action or overwrite:
        raise ValueError("报税信息已自动覆盖，无需手动确认或覆写")
    with store.connect() as db:
        # Serialize revision verification and note insertion with collectors.
        db.execute("BEGIN IMMEDIATE")
        record = db.execute(
            "SELECT t.* FROM tax_records t JOIN shops s ON s.id=t.shop_id AND s.profile_version=t.profile_version WHERE t.shop_id=? AND t.period=?",
            (shop_id, period),
        ).fetchone()
        if not record:
            raise ValueError("该店铺账期尚未保存")
        if revision(json.loads(record["latest"])) != expected:
            raise RuntimeError("数据已更新，请重新核对最新版本后保存")
        if action:
            if action not in ("confirm_entity", "overwrite_amounts"):
                raise ValueError("报税操作无效")
            if overwrite:
                raise ValueError("请分别确认主体和覆写金额")
            before = json.loads(record["baseline"])
            latest = json.loads(record["latest"])
            if revision(before) != baseline_revision:
                raise RuntimeError("数据库原值已变更，请重新核对后操作")
            if latest.get("_missing_from_source"):
                raise ValueError("平台未返回该账期，不能确认或覆写缺失数据")
            from .audit_store import AMOUNTS, differences

            diff = differences(before, latest)
            if action == "confirm_entity":
                if not any(not d["money"] for d in diff):
                    raise ValueError("主体信息没有待确认的变化")
                before["entity"] = latest.get("entity", {})
                before["name"] = latest.get("name", "")
                note = "[确认主体变更] " + note
            else:
                if not any(d["money"] for d in diff):
                    raise ValueError("金额没有需要覆写的差异")
                for field in AMOUNTS:
                    before[field] = latest.get(field)
                    before[field.removesuffix("_cents")] = latest.get(field.removesuffix("_cents"))
                for field in ("summary_through", "partial"):
                    before[field] = latest.get(field)
                note = "[覆写金额原值] " + note
            db.execute(
                "UPDATE tax_records SET baseline=?,baseline_at=? WHERE shop_id=? AND profile_version=? AND period=?",
                (json.dumps(before, ensure_ascii=False), time.time(), shop_id, record["profile_version"], period),
            )
            confirmed = not differences(before, latest)
        if overwrite:
            if json.loads(record["latest"]).get("_missing_from_source"):
                raise ValueError("平台未返回该账期，不能用缺失数据覆盖")
            from .audit_store import differences

            if any(not d["money"] for d in differences(json.loads(record["baseline"]), json.loads(record["latest"]))):
                raise ValueError("请先单独确认主体变更")
            db.execute(
                "UPDATE tax_records SET baseline=latest,baseline_at=checked_at WHERE shop_id=? AND profile_version=? AND period=?",
                (shop_id, record["profile_version"], period),
            )
            note = "[强制覆盖基准] " + note
            confirmed = True
        db.execute(
            "INSERT INTO tax_reviews(shop_id,profile_version,period,revision,confirmed,note,created) VALUES(?,?,?,?,?,?,?)",
            (shop_id, record["profile_version"], period, expected, int(confirmed), note.strip(), time.time()),
        )
    store.log_event(
        "info",
        "tax",
        action or ("baseline_overwritten" if overwrite else "review_saved"),
        period + " · " + note,
        shop_id,
    )
    return read(store, shop_id, period)
