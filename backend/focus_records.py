"""Share observed focus rows with ordinary lists without widening their date coverage."""

import json

from .priority_rules import KINDS


def current_rows(store, shop_id, kind, payload, updated, platform="pdd"):
    rows = payload.get("rows", [])
    if kind not in KINDS or platform != "pdd":
        return rows
    from .operations import decorate

    sync = payload.get("_sync", {})
    with store.connect() as db:
        rows = overlay(db, shop_id, kind, rows, sync.get("snapshot_started_at", updated), sync.get("scope"))
    return [decorate(kind, row, platform) for row in rows]


def covered(kind, row, scope, ordinary_scope=None):
    if kind in ("aftersales", "packages", "invoices"):
        return True
    if not scope:
        return True
    if kind == "intercepts":
        source = ordinary_scope or {}
        return bool(
            source
            and source.get("start", 0) >= scope.get("start", 0)
            and source.get("end", float("inf")) <= scope.get("end", 0)
        )
    day = str(row.get("created_at") or "")[:10]
    return bool(day and scope.get("date_from", "") <= day <= scope.get("date_to", "9999"))


def overlay(db, shop_id, kind, rows, started, ordinary_scope=None):
    if kind not in KINDS:
        return rows
    snapshot = db.execute(
        "SELECT payload,started FROM attention_snapshots WHERE shop_id=? AND kind=?", (shop_id, kind)
    ).fetchone()
    if not snapshot:
        return rows
    payload = json.loads(snapshot["payload"])
    scope = payload.get("_sync", {}).get("scope", {})
    latest = {str(r["id"]): r for r in payload["rows"]}
    from .focus_resolution import records

    resolved = records(db, shop_id, kind)
    output = []
    for original in rows:
        row = dict(original)
        fresh = latest.get(str(row["id"]))
        if fresh and snapshot["started"] > started:
            row.update(fresh)
        if not fresh and snapshot["started"] > started and covered(kind, row, scope, ordinary_scope):
            row["attention_excluded"] = True
        if kind in ("packages", "tickets", "invoices"):
            row.pop("attention_status", None)
            if fresh and (snapshot["started"] > started or fresh.get("status") == row.get("status")):
                row["attention_status"] = fresh.get("attention_status", "")
        fact = resolved.get(str(row["id"]))
        if fact and (fact["started"] >= started or fact["pending"] and fact["row"].get("status") == row.get("status")):
            row.update(fact["row"])
        output.append(row)
    return output


def clear_markers(db, shop_id, kind, current_ids, started, scope=None):
    # A completed same-scope quick filter proves membership ended, not that the
    # order itself closed. Retain its normal status and clear only our marker.
    from .operations import decorate

    records = db.execute(
        "SELECT r.cache_key,r.ordinal,r.payload,s.metadata FROM table_rows r JOIN table_snapshots s ON r.shop_id=s.shop_id AND r.cache_key=s.cache_key WHERE r.shop_id=? AND r.cache_key LIKE ? AND (r.priority>0 OR json_extract(r.payload,'$.attention_status') IS NOT NULL)",
        (shop_id, kind + ":%"),
    ).fetchall()
    for old in records:
        row = json.loads(old["payload"])
        if str(row["id"]) in current_ids:
            continue
        sync = json.loads(old["metadata"]).get("_sync", {})
        if sync.get("snapshot_started_at", 0) > started or not covered(kind, row, scope or {}, sync.get("scope")):
            continue
        row.pop("attention_status", None)
        row["attention_excluded"] = True
        row = decorate(kind, row, "pdd")
        db.execute(
            "UPDATE table_rows SET payload=?,priority=? WHERE shop_id=? AND cache_key=? AND ordinal=?",
            (json.dumps(row, ensure_ascii=False), row["priority"], shop_id, old["cache_key"], old["ordinal"]),
        )


def share(db, shop_id, kind, rows, started):
    # Update only observed IDs already present in a full list. Absence from the
    # focus list does NOT prove closure and cannot delete an ordinary record.
    from .operations import decorate
    from .table_index import SEARCH

    for fresh in rows:
        records = db.execute(
            """SELECT r.cache_key,r.ordinal,r.payload,s.metadata FROM table_rows r
            JOIN table_snapshots s ON r.shop_id=s.shop_id AND r.cache_key=s.cache_key
            WHERE r.shop_id=? AND CAST(json_extract(r.payload,'$.id') AS TEXT)=? AND r.cache_key LIKE ?""",
            (shop_id, str(fresh["id"]), kind + ":%"),
        ).fetchall()
        for old in records:
            if json.loads(old["metadata"]).get("_sync", {}).get("snapshot_started_at", 0) > started:
                continue
            record = db.execute("SELECT platform FROM shops WHERE id=?", (shop_id,)).fetchone()
            row = decorate(kind, {**json.loads(old["payload"]), **fresh}, record["platform"] if record else "pdd")
            db.execute(
                """UPDATE table_rows SET payload=?,status=?,priority=?,created_at=?,search_text=?
                WHERE shop_id=? AND cache_key=? AND ordinal=?""",
                (
                    json.dumps(row, ensure_ascii=False),
                    row.get("status", ""),
                    row.get("priority", 0),
                    row.get("created_at", ""),
                    " ".join(str(row.get(k, "")) for k in SEARCH).casefold(),
                    shop_id,
                    old["cache_key"],
                    old["ordinal"],
                ),
            )
