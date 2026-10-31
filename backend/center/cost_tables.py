"""User-uploaded SKU cost books, owned by the center and activated atomically."""

import base64, csv, hashlib, io, time, uuid, zipfile
from xml.etree import ElementTree as ET
from pathlib import Path
from decimal import Decimal
from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, PrivateAttr, ValidationError
from sqlalchemy import MetaData, Table, Column, String, Integer, Float, JSON, Text, Numeric, select, delete, func
from .report_view import sheet_rows, cell, number

meta = MetaData()
books = Table(
    "cost_books",
    meta,
    Column("id", String(32), primary_key=True),
    Column("name", Text),
    Column("created", Float),
    Column("headers", JSON),
    Column("mapping", JSON),
    Column("count", Integer),
)
entries = Table(
    "cost_entries",
    meta,
    Column("book_id", String(32), primary_key=True),
    Column("sku_hash", String(64), primary_key=True),
    Column("sku", Text),
    Column("unit_cost", Numeric(24, 6)),
    Column("cells", JSON),
    Column("position", Integer),
)
catalog = Table(
    "cost_catalog", meta, Column("id", Integer, primary_key=True), Column("active_id", String(32), nullable=True)
)
LIMIT = 10 * 1024 * 1024


class Upload(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=14 * 1024 * 1024)
    _file_data: bytes | None = PrivateAttr(default=None)
    sku_column: int | None = None
    cost_column: int | None = None
    shipping_column: int | None = None
    shipping_mode: str = "piece"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def read_upload(request: Request):
    from ..file_upload import read_body, multipart

    content_type = request.headers.get("content-type", "application/json")
    raw = await read_body(
        request, LIMIT + 65536 if content_type.startswith("multipart/form-data") else 14 * 1024 * 1024 + 65536
    )
    try:
        if content_type.startswith("multipart/form-data"):
            filename, data, fields = multipart(content_type, raw, LIMIT)
            if set(fields) - {"sku_column", "cost_column", "shipping_column", "shipping_mode"}:
                raise ValueError("上传字段无效")
            body = Upload(filename=filename, **fields)
            body._file_data = data
            return body
        return Upload.model_validate_json(raw)
    except ValueError, ValidationError:
        raise HTTPException(400, "上传格式或字段无效，请重新选择文件") from None


def parse(body):
    try:
        data = body._file_data if body._file_data is not None else base64.b64decode(body.content, validate=True)
    except ValueError:
        raise ValueError("上传文件编码无效") from None
    if not data or len(data) > LIMIT:
        raise ValueError("文件不能为空，且不能超过10MB")
    suffix = Path(body.filename).suffix.lower()
    try:
        if suffix == ".xlsx":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024:
                    raise ValueError("Excel解压大小不能超过50MB")
        iterator = iter(sheet_rows(data, suffix))
        headers = next((list(map(cell, r)) for r in iterator if any(r)), None)
        if not headers or len(headers) > 100:
            raise ValueError("未找到表头，或列数超过100列")
        records = []
        for line, raw in enumerate(iterator, 2):
            if not any(raw):
                continue
            values = [cell(v).strip() for v in raw]
            if len(values) > len(headers) or any(len(v) > 2000 for v in values):
                raise ValueError(f"第{line}行列数或单元格长度超限")
            records.append((line, values + [""] * (len(headers) - len(values))))
            if len(records) > 50000:
                raise ValueError("单份成本表最多50000行")
    except (zipfile.BadZipFile, UnicodeError, IndexError, KeyError, ET.ParseError, csv.Error) as e:
        raise ValueError("无法读取文件，请使用有效的CSV或单工作表XLSX文件") from e
    if not records:
        raise ValueError("成本表没有数据行")

    def guess(words):
        return next((i for i, h in enumerate(headers) if h.strip().lower() in words), None)

    sku = (
        body.sku_column
        if body.sku_column is not None
        else guess({"商家编码-规格维度", "商家编码－规格维度", "sku", "sku编码", "商家编码"})
    )
    cost = (
        body.cost_column
        if body.cost_column is not None
        else guess({"单位成本", "成本单价", "成本价", "成本", "采购价", "单位成本(元)", "单位成本（元）"})
    )
    shipping = body.shipping_column
    if shipping is not None and (shipping in (sku, cost) or not 0 <= shipping < len(headers)):
        raise ValueError("快递费列必须是单独的有效列")
    if body.shipping_mode not in ("piece", "order", "package"):
        raise ValueError("请选择每件、每单或每包裹计费")
    errors = []
    result = []
    seen = set()
    if sku is None or cost is None:
        errors.append("请选择SKU编码列和单位成本列")
    elif sku == cost or not (0 <= sku < len(headers) and 0 <= cost < len(headers)):
        errors.append("SKU编码列和成本列必须是不同的有效列")
    else:
        for line, values in records:
            key = values[sku]
            amount = number(values[cost])
            reason = ""
            if not key:
                reason = "SKU编码为空"
            elif len(key) > 256:
                reason = "SKU编码超过256字符"
            elif key.casefold() in seen:
                reason = "SKU编码重复（不区分大小写）：" + key
            elif (
                amount is None
                or amount < 0
                or amount >= Decimal("1e12")
                or amount != amount.quantize(Decimal(".000001"))
            ):
                reason = "单位成本必须为非负金额，最多6位小数"
            if not reason and shipping is not None:
                fee = number(values[shipping])
                if fee is None or fee < 0 or fee >= Decimal("1e12") or fee != fee.quantize(Decimal(".000001")):
                    reason = "快递费必须为非负金额，最多6位小数"
            if reason:
                errors.append(f"第{line}行：{reason}")
            seen.add(key.casefold())
            if not reason:
                result.append(
                    {"sku_hash": digest(key), "sku": key, "unit_cost": amount, "cells": values, "position": line}
                )
    return {
        "headers": headers,
        "mapping": {
            "sku_column": sku,
            "cost_column": cost,
            "shipping_column": shipping,
            "shipping_mode": body.shipping_mode,
        },
        "total": len(records),
        "sample": [v for _, v in records[:10]],
        "errors": errors[:20],
        "error_count": len(errors),
        "valid": not errors,
    }, result


def install(app, database, admin):
    engine = database.engine
    meta.create_all(engine)
    from .reconciliation import meta as accounting_meta

    accounting_meta.create_all(engine)
    from .cost_supplements import install as install_supplements

    install_supplements(app, database, admin)
    from sqlalchemy.exc import IntegrityError

    try:
        with engine.begin() as db:
            if not db.execute(select(catalog.c.id).where(catalog.c.id == 1)).first():
                db.execute(catalog.insert().values(id=1, active_id=None))
    except IntegrityError:
        pass

    @app.get("/api/cost-tables", dependencies=[Depends(admin)])
    def listing():
        with engine.connect() as db:
            return {
                "active_id": db.execute(select(catalog.c.active_id).where(catalog.c.id == 1)).scalar(),
                "books": [dict(r) for r in db.execute(select(books).order_by(books.c.created.desc())).mappings()],
            }

    @app.post("/api/cost-tables/preview", dependencies=[Depends(admin)])
    def preview(body: Upload = Depends(read_upload)):
        try:
            return parse(body)[0]
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/cost-tables", dependencies=[Depends(admin)])
    def save(body: Upload = Depends(read_upload)):
        try:
            preview, values = parse(body)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not preview["valid"]:
            raise HTTPException(400, "；".join(preview["errors"]))
        bid = uuid.uuid4().hex
        with engine.begin() as db:
            db.execute(select(catalog.c.id).where(catalog.c.id == 1).with_for_update()).first()
            db.execute(
                books.insert().values(
                    id=bid,
                    name=body.filename,
                    created=time.time(),
                    headers=preview["headers"],
                    mapping=preview["mapping"],
                    count=len(values),
                )
            )
            for offset in range(0, len(values), 1000):
                db.execute(entries.insert(), [dict(book_id=bid, **r) for r in values[offset : offset + 1000]])
            db.execute(catalog.update().where(catalog.c.id == 1).values(active_id=bid))
        return {"id": bid, "count": len(values)}

    @app.get("/api/cost-tables/{bid}", dependencies=[Depends(admin)])
    def view(bid: str, keyword: str = Query("", max_length=256), page: int = Query(1, ge=1)):
        with engine.connect() as db:
            book = db.execute(select(books).where(books.c.id == bid)).mappings().first()
            if not book:
                raise HTTPException(404, "成本表不存在")
            where = [entries.c.book_id == bid]
            if keyword:
                where.append(entries.c.sku.contains(keyword, autoescape=True))
            total = db.execute(select(func.count()).select_from(entries).where(*where)).scalar()
            pages = max(1, (total + 49) // 50)
            page = min(page, pages)
            data = (
                db.execute(
                    select(entries.c.cells).where(*where).order_by(entries.c.position).offset((page - 1) * 50).limit(50)
                )
                .scalars()
                .all()
            )
            return {**dict(book), "rows": data, "total": total, "page": page, "pages": pages}

    @app.post("/api/cost-tables/{bid}/activate", dependencies=[Depends(admin)])
    def activate(bid: str):
        with engine.begin() as db:
            db.execute(select(catalog.c.id).where(catalog.c.id == 1).with_for_update()).first()
            if not db.execute(select(books.c.id).where(books.c.id == bid)).first():
                raise HTTPException(404, "成本表不存在")
            db.execute(catalog.update().where(catalog.c.id == 1).values(active_id=bid))
        return {"ok": True}

    @app.delete("/api/cost-tables/{bid}", dependencies=[Depends(admin)])
    def remove(bid: str):
        with engine.begin() as db:
            db.execute(select(catalog.c.id).where(catalog.c.id == 1).with_for_update()).first()
            from .reconciliation import bindings

            if db.execute(select(bindings.c.job_id).where(bindings.c.book_id == bid).limit(1)).first():
                raise HTTPException(409, "此成本版本已绑定历史报表，不能删除；可启用新版本")
            from .cost_supplements import jobs as supplement_jobs

            pending = db.execute(select(supplement_jobs.c.payload)).scalars()
            if any(bid in (t.get("book_id"), t.get("expected_book")) for job in pending for t in job["tasks"]):
                raise HTTPException(409, "此成本版本用于补充核算记录，不能删除")
            db.execute(catalog.update().where(catalog.c.active_id == bid).values(active_id=None))
            db.execute(delete(entries).where(entries.c.book_id == bid))
            db.execute(delete(books).where(books.c.id == bid))
        return {"ok": True}


def enrich(engine, result):
    if result.get("reports"):
        from .reconciliation import enrich_reports

        return enrich_reports(engine, result)
    with engine.connect() as db:
        active = db.execute(select(catalog.c.active_id).where(catalog.c.id == 1)).scalar()
        book = db.execute(select(books).where(books.c.id == active)).mappings().first() if active else None
        result["cost_book"] = {"id": book["id"], "name": book["name"]} if book else None
        headers = result["headers"]
        idx = next((i for i, h in enumerate(headers) if h in ("商家编码-规格维度", "商家编码－规格维度")), None)

        def value(row, names):
            return next((row["cells"][i] for i, h in enumerate(headers) if h in names), "")

        keys = {str(r["cells"][idx]).strip() for r in result["rows"]} if idx is not None else set()
        from .reconciliation import cost_index, surcharge

        costs, conflicts = (
            cost_index(
                {
                    r.sku: {"unit_cost": r.unit_cost}
                    for r in db.execute(select(entries.c.sku, entries.c.unit_cost).where(entries.c.book_id == active))
                }
            )
            if book and keys
            else ({}, set())
        )
        for row in result["rows"]:
            sku = str(row["cells"][idx]).strip() if idx is not None else ""
            entry = costs.get(sku.casefold())
            extra = surcharge(value(row, ("商品",)))
            unit = entry["unit_cost"] if entry and not extra else None
            quantity = number(value(row, ("商品数量(件)", "商品数量")))
            amount = number(value(row, ("商家实收金额(元)",)))
            state = (
                "补差价收入，全部计入利润"
                if extra
                else "SKU大小写归一后存在重复成本编码"
                if sku.casefold() in conflicts
                else "未启用成本表"
                if not book
                else "报表缺少SKU列"
                if idx is None
                else "SKU为空"
                if not sku
                else "未匹配成本"
                if unit is None
                else "数量或实收金额无效"
                if quantity is None or quantity < 0 or amount is None
                else "已匹配"
            )
            if extra:
                unit = Decimal(0)
            total = Decimal(0) if extra else unit * quantity if state == "已匹配" else None
            row["cost"] = {
                "state": state,
                "unit": str(unit) if unit is not None else None,
                "total": str(total) if total is not None else None,
                "profit": str(amount - total) if total is not None else None,
            }
    return result
