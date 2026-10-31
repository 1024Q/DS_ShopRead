"""Bounded report queries and durable streamed table exports."""

import asyncio, contextlib, csv, json, time, uuid, re
from pathlib import Path
from decimal import Decimal
from sqlalchemy import MetaData, Table, Column, String, JSON, Float, select, func, cast, Numeric, tuple_, delete
from fastapi import Depends, HTTPException, Body
from fastapi.responses import FileResponse
from .report_view import documents, rows, month_dates, selected_documents, available_report_ids
from .reconciliation import results, bindings
from .cost_tables import books, entries

meta = MetaData()
exports = Table(
    "center_table_exports",
    meta,
    Column("id", String(32), primary_key=True),
    Column("created", Float),
    Column("payload", JSON),
)
ROOT = Path(__file__).resolve().parents[2] / ".data/center/table-exports"
COST = [
    ("state", "成本匹配", "text"),
    ("sku", "核算SKU", "text"),
    ("amount", "核算实收金额", "number"),
    ("unit", "单位成本", "number"),
    ("total", "商品成本", "number"),
    ("shipping", "快递费", "number"),
    ("profit", "商品毛利", "number"),
    ("net", "扣运费后毛利", "number"),
]


def report_plan(db, p):
    month = p.get("month", "")
    shop = int(p.get("shop_id") or 0)
    if month:
        month_dates(month)
    valid = available_report_ids(db)
    docs = selected_documents(db, valid, month, shop)
    headers = docs[0]["headers"] if docs else []
    if any(d["headers"] != headers for d in docs):
        raise ValueError("报表列结构不同，请按店铺筛选")
    columns = [dict(id="shop_name", label="店铺名称", type="text")]
    expressions = {"shop_name": documents.c.shop_name}
    for i, h in enumerate(headers):
        numeric = h == "商家实收金额(元)" or h in ("商品数量", "商品数量(件)")
        key = "raw_" + str(i)
        columns.append(dict(id=key, label=h, type="number" if numeric else "text"))
        expressions[key] = (
            rows.c.amount
            if h == "商家实收金额(元)"
            else rows.c.quantity
            if h in ("商品数量", "商品数量(件)")
            else rows.c.cells[i].as_string()
        )
    for key, label, kind in COST:
        k = "cost_" + key
        columns.append(dict(id=k, label=label, type=kind))
        expr = results.c.cost[key].as_string()
        expressions[k] = cast(expr, Numeric(24, 6)) if kind == "number" else expr
    from .profit_tax import reports as tax_reports, allocations as allocation

    for key, label, expr, kind in [
        ("tax_base", "分摊计税基数", allocation.c.base, "number"),
        ("tax_rate", "税点(%)", cast(tax_reports.c.summary["rate"].as_string(), Numeric(12, 6)), "number"),
        ("tax_estimated", "分摊预估税费", allocation.c.estimated, "number"),
        ("tax_deduction", "分摊新增扣税", allocation.c.deduction, "number"),
        ("after_tax_margin", "税后商品毛利（扣运费）", expressions["cost_net"] - allocation.c.deduction, "number"),
        ("tax_version", "税费版本", tax_reports.c.version_id, "text"),
        ("tax_status", "税费口径", tax_reports.c.summary["status"].as_string(), "text"),
    ]:
        columns.append(dict(id=key, label=label, type=kind))
        expressions[key] = expr
    for key, label in [("actual", "分摊实际税费"), ("included", "分摊已包含税费")]:
        columns.append(dict(id="tax_" + key, label=label, type="number"))
        expressions["tax_" + key] = allocation.c[key]
    for key, label, expr in [
        ("period", "报表月份", documents.c.start),
        ("book", "成本版本", bindings.c.book_id),
        ("book_name", "成本表名称", bindings.c.summary["book_name"].as_string()),
    ]:
        columns.append(dict(id=key, label=label, type="text"))
        expressions[key] = expr
    joined = (
        rows.join(documents, rows.c.job_id == documents.c.id)
        .outerjoin(results, (results.c.job_id == rows.c.job_id) & (results.c.position == rows.c.position))
        .outerjoin(bindings, bindings.c.job_id == rows.c.job_id)
    )
    joined = joined.outerjoin(tax_reports, tax_reports.c.job_id == rows.c.job_id).outerjoin(
        allocation, (allocation.c.job_id == rows.c.job_id) & (allocation.c.position == rows.c.position)
    )
    where = [rows.c.job_id.in_([d["id"] for d in docs])]
    if p.get("keyword"):
        where.append(rows.c.search.contains(str(p["keyword"])[:200], autoescape=True))
    if p.get("status"):
        where.append(rows.c.status == p["status"])
    for k, v in p.get("filters", {}).items():
        if v and k in expressions:
            where.append(cast(expressions[k], String).contains(str(v)[:200], autoescape=True))
    order = expressions.get(p.get("sort"), documents.c.shop_name)
    order = (order.desc() if p.get("direction") == "desc" else order.asc()).nulls_last()
    statement = (
        select(rows.c.job_id, rows.c.position, *[expr.label(key) for key, expr in expressions.items()])
        .select_from(joined)
        .where(*where)
        .order_by(order, rows.c.job_id, rows.c.position)
    )
    return columns, statement, joined, where, expressions


def serialize(row):
    value = {k: str(v) if isinstance(v, Decimal) else v for k, v in dict(row).items()}
    value["id"] = value.pop("job_id") + ":" + str(value.pop("position"))
    return value


def query(engine, p):
    from .profit_tax import sync

    sync(engine, month=p.get("month", ""), shop_id=int(p.get("shop_id") or 0))
    with engine.connect() as db:
        cols, stmt, joined, where, expr = report_plan(db, p)
        total = db.execute(select(func.count()).select_from(joined).where(*where)).scalar()
        size = max(10, min(200, int(p.get("page_size", 50))))
        pages = max(1, (total + size - 1) // size)
        page = max(1, min(pages, int(p.get("page", 1))))
        sums = {}
        numeric = [c for c in cols if c["type"] == "number"]
        if numeric:
            values = db.execute(
                select(*[v for c in numeric for v in (func.sum(expr[c["id"]]), func.count(expr[c["id"]]))])
                .select_from(joined)
                .where(*where)
            ).one()
            sums = {
                c["id"]: dict(
                    value=str(values[i * 2]) if values[i * 2] is not None else None, missing=total - values[i * 2 + 1]
                )
                for i, c in enumerate(numeric)
            }
        return dict(
            columns=cols,
            rows=[serialize(r) for r in db.execute(stmt.offset((page - 1) * size).limit(size)).mappings()],
            total=total,
            page=page,
            pages=pages,
            summary=sums,
        )


def run_export(engine, jid, root):
    with engine.connect() as db:
        body = db.execute(select(exports.c.payload).where(exports.c.id == jid)).scalar()

    def update(**patch):
        body.update(patch)
        with engine.begin() as db:
            db.execute(exports.update().where(exports.c.id == jid).values(payload=body))

    try:
        if body.get("source") == "orders":
            from .profit_tax import sync

            scope = body.get("query") or {}
            sync(engine, month=scope.get("month", ""), shop_id=int(scope.get("shop_id") or 0))
        update(state="running", written=0)
        root.mkdir(parents=True, exist_ok=True)
        path = root / (jid + "." + body["format"])
        temp = root / (jid + ".tmp")
        with engine.connect() as db:
            if engine.dialect.name == "postgresql":
                db = db.execution_options(isolation_level="REPEATABLE READ")
            if body.get("source") == "orders":
                columns, stmt, _, _, _ = report_plan(db, body["query"])
                selected = body.get("selection")
                if selected:
                    pairs = []
                    for value in selected.get("ids", []):
                        job, pos = value.split(":")
                        pairs.append((job, int(pos)))
                    pred = tuple_(rows.c.job_id, rows.c.position).in_(pairs)
                    stmt = stmt.where(~pred if selected.get("all") else pred)
                iterator = (serialize(r) for r in db.execution_options(stream_results=True).execute(stmt).mappings())
            else:
                columns = body["columns"]
                iterator = iter(body["rows"])
            wanted = body.get("column_ids")
            columns = [c for key in wanted for c in columns if c["id"] == key] if wanted else columns

            def values():
                yield [c["label"] for c in columns]
                for n, r in enumerate(iterator, 1):
                    yield [
                        float(r[c["id"]])
                        if c.get("type") == "number" and r.get(c["id"]) not in (None, "")
                        else r.get(c["id"])
                        for c in columns
                    ]
                    if n % 1000 == 0:
                        update(written=n)
                update(written=n if "n" in locals() else 0)

            if body["format"] == "csv":
                with temp.open("w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.writer(f)
                    for i, row in enumerate(values()):
                        extra = (
                            ["导出时间", "筛选条件"]
                            if i == 0
                            else [
                                time.strftime("%Y-%m-%d %H:%M:%S"),
                                json.dumps(body.get("query", {}), ensure_ascii=False),
                            ]
                        )
                        writer.writerow(
                            [
                                "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v
                                for v in row + extra
                            ]
                        )
            else:
                from ..xlsx_export import workbook
                from ..export_jobs import target
                import itertools

                # Split at Excel's worksheet limit without retaining rows in memory.
                stream = iter(values())
                header = next(stream)
                chunks = []
                # Count upper bound is only for sheet allocation; current datasets <= 50 million rows.
                count = (
                    db.execute(select(func.count()).select_from(stmt.order_by(None).subquery())).scalar()
                    if body.get("source") == "orders"
                    else len(body["rows"])
                )
                for i in range(max(1, (count + 999999) // 1000000)):
                    chunks.append(("数据" + str(i + 1), itertools.chain([header], itertools.islice(stream, 1000000))))
                chunks.append(
                    (
                        "导出说明",
                        [
                            ["项目", "内容"],
                            ["导出时间", time.strftime("%Y-%m-%d %H:%M:%S")],
                            ["来源", body.get("title", "")],
                            ["筛选条件", json.dumps(body.get("query", {}), ensure_ascii=False)],
                            ["说明", "空值表示缺失，非0；利润需结合核对状态，成本版本见数据列。"],
                        ],
                    )
                )
                token = target.set(str(temp))
                try:
                    workbook(chunks)
                finally:
                    target.reset(token)
                update(written=count)
        temp.replace(path)
        update(state="completed", error="", rows=[], size=path.stat().st_size)
    except Exception as e:
        update(state="error", error=str(e)[:400])


def install(app, database, admin):
    engine = database.engine
    meta.create_all(engine)
    from .profit_tax import meta as tax_meta
    from .profit import meta as profit_meta

    tax_meta.create_all(engine)
    profit_meta.create_all(engine)

    @app.post("/api/table-tools/orders", dependencies=[Depends(admin)])
    def orders(body: dict = Body(...)):
        try:
            return query(engine, body)
        except ValueError, TypeError:
            raise HTTPException(400, "筛选或分页条件无效")

    @app.get("/api/table-tools/cost/{bid}", dependencies=[Depends(admin)])
    def cost(bid: str):
        with engine.connect() as db:
            book = db.execute(select(books).where(books.c.id == bid)).mappings().first()
            if not book:
                raise HTTPException(404, "成本表不存在")
            return {
                **dict(book),
                "rows": [
                    dict(id=r["sku_hash"], **{"raw_" + str(i): v for i, v in enumerate(r["cells"])})
                    for r in db.execute(
                        select(entries).where(entries.c.book_id == bid).order_by(entries.c.position)
                    ).mappings()
                ],
            }

    @app.post("/api/table-tools/exports", dependencies=[Depends(admin)])
    def start(body: dict = Body(...)):
        if body.get("format") not in ("csv", "xlsx"):
            raise HTTPException(400, "导出格式无效")
        if body.get("source") != "orders":
            if (
                not isinstance(body.get("rows"), list)
                or len(body["rows"]) > 50000
                or not isinstance(body.get("columns"), list)
                or not 1 <= len(body["columns"]) <= 220
            ):
                raise HTTPException(400, "导出数据超限")
        if len(json.dumps(body)) > 30 * 1024 * 1024:
            raise HTTPException(400, "导出内容过大")
        with engine.begin() as db:
            if sum(r["state"] in ("pending", "running") for r in db.execute(select(exports.c.payload)).scalars()) >= 3:
                raise HTTPException(409, "已有3个导出任务，请等待完成")
            jid = uuid.uuid4().hex
            db.execute(
                exports.insert().values(id=jid, created=time.time(), payload={**body, "state": "pending", "written": 0})
            )
        return {"id": jid}

    @app.get("/api/table-tools/exports", dependencies=[Depends(admin)])
    def listing():
        with engine.connect() as db:
            return [
                {
                    k: v
                    for k, v in {"id": r["id"], "created": r["created"], **r["payload"]}.items()
                    if k in ("id", "created", "state", "written", "error", "title", "format", "size")
                }
                for r in db.execute(select(exports).order_by(exports.c.created.desc()).limit(20)).mappings()
            ]

    @app.get("/api/table-tools/exports/{jid}/download", dependencies=[Depends(admin)])
    def download(jid: str):
        with engine.connect() as db:
            body = db.execute(select(exports.c.payload).where(exports.c.id == jid)).scalar()
        if not body or body["state"] != "completed":
            raise HTTPException(404, "文件尚未生成")
        return FileResponse(
            ROOT / (jid + "." + body["format"]), filename=(body.get("title", "数据")[:60] + "." + body["format"])
        )

    async def worker():
        while True:
            try:
                with engine.begin() as db:
                    expired = list(
                        db.execute(select(exports).where(exports.c.created < time.time() - 7 * 86400)).mappings()
                    )
                    for old in expired:
                        if old["payload"]["state"] not in ("completed", "error") or not re.fullmatch(
                            "[a-f0-9]{32}", old["id"]
                        ):
                            continue
                        for suffix in ("csv", "xlsx", "tmp"):
                            (ROOT / (old["id"] + "." + suffix)).unlink(missing_ok=True)
                        db.execute(delete(exports).where(exports.c.id == old["id"]))
                with engine.connect() as db:
                    pending = [
                        r["id"]
                        for r in db.execute(select(exports)).mappings()
                        if r["payload"]["state"] in ("pending", "running")
                    ]
                for jid in pending:
                    await asyncio.to_thread(run_export, engine, jid, ROOT)
            except Exception:
                import logging

                logging.getLogger("ecmp.center").exception("表格导出失败")
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
