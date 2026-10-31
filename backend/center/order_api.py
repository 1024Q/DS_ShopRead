import time
from pydantic import BaseModel, Field
from fastapi import Depends, HTTPException
from sqlalchemy import select, update, tuple_
from .database import order_rows, nodes, settings


class Change(BaseModel):
    revision: int = Field(gt=0)
    shop: str = Field(pattern=r"^[1-9]\d*$")
    scope: str = Field(pattern=r"^orders:view-v2:\d+$")
    id: str = Field(min_length=1, max_length=150)
    payload: dict | None = None
    observed: float = Field(ge=0)


class Changes(BaseModel):
    stream: str = Field(pattern=r"^[a-f0-9]{32}$")
    rows: list[Change] = Field(min_length=1, max_length=200)


def receipt_key(node, stream):
    return f"order-receipt:{node}:{stream}"


def ingest(database, node, body):
    revisions = [r.revision for r in body.rows]
    if revisions != sorted(set(revisions)):
        raise ValueError("订单版本顺序无效")
    with database.engine.begin() as db:
        db.execute(select(nodes.c.id).where(nodes.c.id == node).with_for_update()).one()
        key = receipt_key(node, body.stream)
        old = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar() or 0
        ignored = set(db.execute(select(settings.c.key).where(settings.c.key.like(f"retired-shop:{node}:%"))).scalars())
        values = []
        for r in body.rows:
            if r.payload is not None and str(r.payload.get("id")) != r.id:
                raise ValueError("订单编号不一致")
            if r.revision <= old or f"retired-shop:{node}:{r.shop}" in ignored:
                continue
            values.append(
                {
                    "node": node,
                    "stream": body.stream,
                    "shop": r.shop,
                    "scope": r.scope,
                    "id": r.id,
                    "payload": r.payload,
                    "observed": r.observed,
                    "revision": r.revision,
                }
            )
        if values:
            from importlib import import_module

            statement = (
                import_module("sqlalchemy.dialects." + database.engine.dialect.name).insert(order_rows).values(values)
            )
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=["node", "stream", "shop", "scope", "id"],
                    set_={k: getattr(statement.excluded, k) for k in ("payload", "observed", "revision")},
                    where=statement.excluded.revision > order_rows.c.revision,
                )
            )
            # Existing SSE and query-cache revision track snapshot timestamps.
            database.upsert(
                db,
                [
                    {
                        "node": node,
                        "shop": "0",
                        "scope": "order-change-clock",
                        "updated": time.time(),
                        "payload": {"revision": revisions[-1]},
                    }
                ],
            )
        revision = max(old, revisions[-1])
        if old:
            db.execute(update(settings).where(settings.c.key == key).values(value=revision))
        else:
            db.execute(settings.insert().values(key=key, value=revision))
        db.execute(update(nodes).where(nodes.c.id == node).values(seen=time.time()))
    if values:
        database.changes.notify(node, [{"scope": "order-change-clock"}])
    return revision


def expand(database, loaded):
    """Batch-load row storage for v2 views; older collectors keep snapshot reads."""
    from .business import chunks

    wanted = {
        (r["node"], r["shop"], r["scope"])
        for group in loaded.values()
        for r in group
        if r["payload"].get("_order_rows_v2")
    }
    receipt_keys = {
        receipt_key(r["payload"].get("_order_receipt_node", r["node"]), r["payload"]["_order_stream"])
        for group in loaded.values()
        for r in group
        if r["payload"].get("_order_rows_v2")
    }
    rows = {}
    receipts = {}
    with database.engine.connect() as db:
        for chunk in chunks(sorted(wanted)):
            for r in db.execute(
                select(order_rows).where(tuple_(order_rows.c.node, order_rows.c.shop, order_rows.c.scope).in_(chunk))
            ).mappings():
                group = rows.setdefault((r["node"], r["shop"], r["scope"]), {})
                if r["id"] not in group or r["observed"] >= group[r["id"]]["observed"]:
                    group[r["id"]] = r
        for chunk in chunks(sorted(receipt_keys)):
            receipts.update(db.execute(select(settings.c.key, settings.c.value).where(settings.c.key.in_(chunk))).all())
    result = {}
    for pair, group in loaded.items():
        result[pair] = []
        for original in group:
            r = dict(original)
            p = dict(r["payload"])
            if p.get("_order_rows_v2"):
                p["rows"] = [
                    v["payload"]
                    for v in rows.get((r["node"], r["shop"], r["scope"]), {}).values()
                    if v["payload"] is not None
                ]
                confirmed = (
                    receipts.get(receipt_key(p.get("_order_receipt_node", r["node"]), p["_order_stream"]), 0)
                    >= p["_order_revision"]
                )
                p["complete"] = bool(p.get("complete") and confirmed)
                p["scope_note"] = f"中心已接收 {len(p['rows'])} 单；" + (
                    "当前订单变更已确认" if confirmed else "订单变更回传中，先显示已接收数据"
                )
                r["payload"] = p
            result[pair].append(r)
    return result


def install(app, database, agent):
    @app.get("/agent/order-receipt")
    def receipt(stream: str, node=Depends(agent)):
        with database.engine.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == receipt_key(node, stream))).scalar()
        return {"revision": value or 0}

    @app.post("/agent/order-changes")
    def changes(body: Changes, node=Depends(agent)):
        try:
            return {"revision": ingest(database, node, body)}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
