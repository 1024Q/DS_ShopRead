"""Fill missing SKU costs using derived immutable books and resumable recalculation."""

import asyncio, contextlib, csv, io, time, uuid, hashlib, json
from fastapi import Depends, HTTPException, Body
from fastapi.responses import Response
from sqlalchemy import MetaData, Table, Column, String, JSON, Float, Integer, Text, select, func
from .cost_tables import books, entries, catalog, Upload, parse, digest, read_upload
from .reconciliation import bindings, results, snapshot
from .report_view import selected_documents, available_report_ids

meta = MetaData()
jobs = Table(
    "cost_supplement_jobs",
    meta,
    Column("id", String(32), primary_key=True),
    Column("created", Float),
    Column("payload", JSON),
)

sku_links = Table(
    "report_manual_sku_links",
    meta,
    Column("id", String(64), primary_key=True),
    Column("shop_id", Integer),
    Column("product", Text),
    Column("spec", Text),
    Column("sku", String(256)),
    Column("updated", Float),
    Column("history", JSON),
)


def missing(engine, scope="current"):
    if scope not in ("current", "history"):
        raise ValueError("报表范围无效")
    with engine.connect() as db:
        docs = {d["id"]: d for d in selected_documents(db, available_report_ids(db), history=scope == "history")}
        names = {
            r["job_id"]: r["summary"] or {}
            for r in db.execute(select(bindings).where(bindings.c.job_id.in_(docs))).mappings()
        }
        cols = [results.c.job_id] + [results.c.cost[k].as_string() for k in ("sku", "product", "spec", "state")]
        found = db.execute(
            select(*cols, func.count())
            .where(results.c.job_id.in_(docs), results.c.cost["total"].as_string().is_(None))
            .group_by(*cols)
        )
        grouped = {}
        for jid, sku, product, spec, state, count in found:
            key = sku.strip().casefold() if sku else ("", product, spec)
            item = grouped.setdefault(
                key,
                dict(
                    sku=sku or "",
                    products=set(),
                    specs=set(),
                    shops=set(),
                    reasons=set(),
                    rows=0,
                    scopes={},
                    reports={},
                ),
            )
            item["products"].add(product or "")
            item["specs"].add(spec or "")
            item["shops"].add(names.get(jid, {}).get("shop_name", ""))
            item["reasons"].add(state or "未匹配成本")
            item["rows"] += count
            summary = names.get(jid, {})
            doc = docs[jid]
            item["reports"][jid] = dict(
                job_id=jid, month=(doc["start"] or "")[:7], created=doc["created"], shop_name=doc["shop_name"]
            )
            if not sku:
                item["scopes"][str(summary.get("shop_id"))] = dict(
                    shop_id=summary.get("shop_id"),
                    shop_name=summary.get("shop_name", ""),
                    product=product or "",
                    spec=spec or "",
                )
        return sorted(
            [
                {
                    k: sorted(v) if isinstance(v, set) else list(v.values()) if k in ("scopes", "reports") else v
                    for k, v in x.items()
                }
                for x in grouped.values()
            ],
            key=lambda x: -x["rows"],
        )


def create(engine, body):
    preview, added = parse(body)
    if not preview["valid"]:
        raise ValueError("；".join(preview["errors"]))
    sm = preview["mapping"]
    additions = {r["sku"].casefold(): r for r in added}
    jid = hashlib.sha256(
        json.dumps(
            [body.filename, preview["mapping"], [(r["sku"], str(r["unit_cost"]), r["cells"]) for r in added]],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:32]
    with engine.begin() as db:
        active = db.execute(select(catalog.c.active_id).where(catalog.c.id == 1).with_for_update()).scalar()
        old = db.execute(select(jobs.c.payload).where(jobs.c.id == jid)).scalar()
        if old:
            return {"id": jid, **old}
        if any(p["state"] == "pending" for p in db.execute(select(jobs.c.payload)).scalars()):
            raise ValueError("上一份补充成本仍在重算，请完成后再上传下一份")
        # Only unresolved rows matching uploaded SKUs become recalculation targets.
        targets = {}
        for job, sku in db.execute(
            select(results.c.job_id, results.c.cost["sku"].as_string())
            .where(results.c.cost["total"].as_string().is_(None))
            .distinct()
        ):
            if sku and sku.strip().casefold() in additions:
                targets[job] = None
        bound = {
            r["job_id"]: dict(r) for r in db.execute(select(bindings).where(bindings.c.job_id.in_(targets))).mappings()
        }
        sources = {r["book_id"] for r in bound.values()} | {active}
        derived = {}
        new_count = 0
        for source in sources:
            book = db.execute(select(books).where(books.c.id == source)).mappings().first() if source else None
            original = list(db.execute(select(entries).where(entries.c.book_id == source)).mappings()) if source else []
            existing = {r["sku"].strip().casefold() for r in original}
            extra = [r for k, r in additions.items() if k not in existing]
            if not extra:
                derived[source] = source
                continue
            mode = book["mapping"].get("shipping_mode", "piece") if book else sm["shipping_mode"]
            if sm["shipping_column"] is not None and mode != sm["shipping_mode"]:
                raise ValueError("补充快递费计费方式与受影响历史版本不同，请使用相同方式")
            bid = uuid.uuid4().hex
            derived[source] = bid
            values = []
            for r in original:
                col = book["mapping"].get("shipping_column")
                fee = r["cells"][col] if col is not None else ""
                values.append((r["sku"], r["unit_cost"], fee))
            for r in extra:
                col = sm["shipping_column"]
                values.append((r["sku"], r["unit_cost"], r["cells"][col] if col is not None else ""))
            db.execute(
                books.insert().values(
                    id=bid,
                    name=("补充成本 · " + body.filename)[:200],
                    created=time.time(),
                    headers=["商家编码", "成本价", "快递费"],
                    mapping=dict(sku_column=0, cost_column=1, shipping_column=2, shipping_mode=mode),
                    count=len(values),
                )
            )
            for offset in range(0, len(values), 1000):
                db.execute(
                    entries.insert(),
                    [
                        dict(
                            book_id=bid,
                            sku_hash=digest(sku),
                            sku=sku,
                            unit_cost=cost,
                            cells=[sku, str(cost), str(fee)],
                            position=offset + i + 2,
                        )
                        for i, (sku, cost, fee) in enumerate(values[offset : offset + 1000])
                    ],
                )
            new_count += len(extra)
        if not new_count:
            raise ValueError("上传SKU均已有成本；补充上传不会覆盖已有价格，请使用完整成本表更新")
        db.execute(catalog.update().where(catalog.c.id == 1).values(active_id=derived[active]))
        tasks = [
            dict(
                job_id=k,
                shop_name=bound[k]["summary"].get("shop_name", ""),
                expected_book=bound[k]["book_id"],
                book_id=derived[bound[k]["book_id"]],
                state="pending",
                error="",
            )
            for k in targets
            if k in bound
        ]
        payload = dict(
            filename=body.filename, sku_count=len(added), tasks=tasks, state="pending" if tasks else "completed"
        )
        db.execute(jobs.insert().values(id=jid, created=time.time(), payload=payload))
        return {"id": jid, **payload}


def save_manual_sku(engine, body):
    shop = body.get("shop_id")
    product = body.get("product")
    spec = body.get("spec")
    sku = body.get("sku")
    if (
        type(shop) is not int
        or not isinstance(product, str)
        or not product
        or not isinstance(spec, str)
        or not isinstance(sku, str)
        or not sku.strip()
        or len(sku.strip()) > 256
    ):
        raise ValueError("请选择具体店铺商品并填写有效商家编码")
    sku = sku.strip()
    key = hashlib.sha256(json.dumps([shop, product, spec], ensure_ascii=False).encode()).hexdigest()
    from .report_view import documents, rows

    with engine.begin() as db:
        db.execute(select(catalog.c.id).where(catalog.c.id == 1).with_for_update()).first()
        old = db.execute(select(sku_links).where(sku_links.c.id == key)).mappings().first()
        if old and body.get("updated") != old["updated"]:
            raise ValueError("对应关系已变化，请刷新后重试")
        if any(p["state"] == "pending" for p in db.execute(select(jobs.c.payload)).scalars()):
            raise ValueError("有成本核算任务正在处理，请完成后再保存")
        targets = []
        for doc in db.execute(select(documents).where(documents.c.shop_id == shop, documents.c.ready == 1)).mappings():
            headers = doc["headers"]
            si = next((i for i, h in enumerate(headers) if h in ("商家编码-规格维度", "商家编码－规格维度")), None)
            if si is None or "商品" not in headers or "商品规格" not in headers:
                continue
            where = [
                rows.c.job_id == doc["id"],
                func.trim(func.coalesce(rows.c.cells[si].as_string(), "")) == "",
                func.trim(rows.c.cells[headers.index("商品")].as_string()) == product,
                func.trim(rows.c.cells[headers.index("商品规格")].as_string()) == spec,
            ]
            if db.execute(select(rows.c.position).where(*where).limit(1)).first():
                targets.append(dict(job_id=doc["id"], shop_name=doc["shop_name"], state="pending", error=""))
        if not targets:
            raise ValueError("该店铺没有此商品规格的空SKU记录，未保存")
        history = list(old["history"] or []) if old else []
        if old:
            history.append(dict(sku=old["sku"], updated=old["updated"]))
        value = dict(shop_id=shop, product=product, spec=spec, sku=sku, updated=time.time(), history=history)
        if old:
            db.execute(sku_links.update().where(sku_links.c.id == key).values(**value))
        else:
            db.execute(sku_links.insert().values(id=key, **value))
        jid = uuid.uuid4().hex
        payload = dict(kind="manual_sku", filename="手动商家编码 · " + sku, sku_count=1, tasks=targets, state="pending")
        db.execute(jobs.insert().values(id=jid, created=time.time(), payload=payload))
        return dict(id=jid, **payload)


def process(engine, jid):
    with engine.connect() as db:
        payload = db.execute(select(jobs.c.payload).where(jobs.c.id == jid)).scalar()
    for task in payload["tasks"]:
        if task["state"] != "pending":
            continue
        try:
            if payload.get("kind") == "manual_sku":
                snapshot(engine, task["job_id"], refresh_rules=True)
            else:
                snapshot(
                    engine,
                    task["job_id"],
                    refresh_rules=True,
                    book_override=task["book_id"],
                    expected_book=task["expected_book"],
                )
            task.update(state="completed", error="")
        except Exception as e:
            task.update(state="error", error=str(e)[:300])
        payload["state"] = "pending"
        with engine.begin() as db:
            db.execute(jobs.update().where(jobs.c.id == jid).values(payload=payload))
    payload["state"] = "error" if any(t["state"] == "error" for t in payload["tasks"]) else "completed"
    with engine.begin() as db:
        db.execute(jobs.update().where(jobs.c.id == jid).values(payload=payload))


def install(app, database, admin):
    engine = database.engine
    meta.create_all(engine)

    @app.get("/api/cost-supplements", dependencies=[Depends(admin)])
    def listing(scope: str = "current"):
        if scope not in ("current", "history"):
            raise HTTPException(400, "报表范围无效")
        with engine.connect() as db:
            history = [
                {"id": r["id"], "created": r["created"], **r["payload"]}
                for r in db.execute(select(jobs).order_by(jobs.c.created.desc()).limit(20)).mappings()
            ]
        with engine.connect() as db:
            shop_names = {
                s.get("shop_id"): s.get("shop_name", "") for s in db.execute(select(bindings.c.summary)).scalars() if s
            }
            links = [
                {**dict(r), "shop_name": shop_names.get(r["shop_id"], "店铺 " + str(r["shop_id"]))}
                for r in db.execute(select(sku_links).order_by(sku_links.c.updated.desc())).mappings()
            ]
        return dict(items=missing(engine, scope), jobs=history, sku_links=links, scope=scope)

    @app.post("/api/cost-supplements/manual-sku", dependencies=[Depends(admin)])
    def manual(body: dict = Body(...)):
        try:
            return save_manual_sku(engine, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/cost-supplements/template", dependencies=[Depends(admin)])
    def template(scope: str = "current"):
        if scope not in ("current", "history"):
            raise HTTPException(400, "报表范围无效")
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            ["商家编码", "商品名称", "规格", "成本价", "快递费", "涉及店铺", "原因", "来源月份", "报表版本"]
        )

        def safe(v):
            return "'" + v if v.startswith(("=", "+", "-", "@")) else v

        for r in missing(engine, scope):
            if r["sku"]:
                writer.writerow(
                    [
                        safe(r["sku"]),
                        safe("；".join(r["products"])),
                        safe("；".join(r["specs"])),
                        "",
                        "",
                        safe("；".join(r["shops"])),
                        safe("；".join(r["reasons"])),
                        "；".join(sorted({p["month"] for p in r["reports"]})),
                        "；".join(p["job_id"] for p in r["reports"]),
                    ]
                )
        return Response(
            ("\ufeff" + stream.getvalue()).encode("utf-8"),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="missing-cost-template.csv"'},
        )

    @app.post("/api/cost-supplements", dependencies=[Depends(admin)])
    def save(body: Upload = Depends(read_upload)):
        try:
            return create(engine, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/cost-supplements/{jid}/retry", dependencies=[Depends(admin)])
    def retry(jid: str):
        with engine.begin() as db:
            value = db.execute(select(jobs.c.payload).where(jobs.c.id == jid).with_for_update()).scalar()
            if value is None:
                raise HTTPException(404, "补充任务不存在")
            if value["state"] == "error":
                for t in value["tasks"]:
                    if t["state"] == "error":
                        t.update(state="pending", error="")
                value["state"] = "pending"
                db.execute(jobs.update().where(jobs.c.id == jid).values(payload=value))
        return {"ok": True}

    async def worker():
        while True:
            try:
                with engine.connect() as db:
                    pending = [
                        r["id"] for r in db.execute(select(jobs)).mappings() if r["payload"]["state"] == "pending"
                    ]
                for jid in pending:
                    await asyncio.to_thread(process, engine, jid)
            except Exception:
                import logging

                logging.getLogger("ecmp.center").exception("补充成本重算任务失败，将恢复")
            await asyncio.sleep(3)

    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            task = asyncio.create_task(worker())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = lifespan
