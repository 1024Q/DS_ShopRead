"""Center-only report import and bounded SQL queries; IDs remain strings."""

import csv, io, logging, re, time, uuid, zipfile
from decimal import Decimal, InvalidOperation
from datetime import datetime
from .. import safe_xml
from sqlalchemy import (
    Table,
    Column,
    String,
    Integer,
    Float,
    JSON,
    Text,
    Numeric,
    MetaData,
    Index,
    select,
    delete,
    func,
    inspect,
)
from ..report_export_store import MAX_BYTES

meta = MetaData()
documents = Table(
    "report_documents",
    meta,
    Column("id", String(32), primary_key=True),
    Column("shop_id", Integer, index=True),
    Column("shop_name", Text),
    Column("start", String(19)),
    Column("end", String(19)),
    Column("created", Float),
    Column("headers", JSON),
    Column("row_count", Integer),
    Column("ready", Integer, default=0),
)
rows = Table(
    "report_document_rows",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("cells", JSON),
    Column("order_number", Text),
    Column("status", Text),
    Column("amount", Numeric(24, 6)),
    Column("quantity", Numeric(24, 6)),
    Column("search", Text),
)
Index("ix_report_order", rows.c.job_id, rows.c.order_number)


def month_dates(month):
    if not isinstance(month, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("请选择有效月份")
    y, m = map(int, month.split("-"))
    end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return f"{month}-01 00:00:00", end.strftime("%Y-%m-%d %H:%M:%S")


def number(text):
    try:
        value = Decimal(str(text).replace(",", "").replace("¥", "").replace("￥", "").strip())
        return value if value.is_finite() and abs(value) < Decimal("1e18") else None
    except InvalidOperation:
        return None


def cell(value):
    text = "" if value is None else str(value)
    # CSV exports sometimes protect long identifiers with Excel's text formula syntax.
    if text.startswith('="') and text.endswith('"'):
        text = text[2:-1].replace('""', '"')
    return text.rstrip("\t")


def sheet_rows(data, suffix):
    if suffix == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        yield from csv.reader(io.StringIO(text, newline=""), dialect)
    elif suffix == ".xlsx":
        ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        with zipfile.ZipFile(io.BytesIO(data)) as book:
            if sum(i.file_size for i in book.infolist()) > MAX_BYTES:
                raise ValueError("Excel解压大小超限")
            strings = []
            if "xl/sharedStrings.xml" in book.namelist():
                with book.open("xl/sharedStrings.xml") as f:
                    for _, e in safe_xml.iterparse(f, events=("end",)):
                        if e.tag == ns + "si":
                            strings.append("".join(e.itertext()))
                            e.clear()
            sheets = sorted(n for n in book.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
            if len(sheets) != 1:
                raise ValueError("Excel包含多个工作表，请核对后使用单表CSV导出")
            with book.open(sheets[0]) as f:
                for _, e in safe_xml.iterparse(f, events=("end",)):
                    if e.tag != ns + "row":
                        continue
                    values = []
                    for c in e.findall(ns + "c"):
                        letters = re.match("[A-Z]+", c.get("r", "A")).group()
                        index = 0
                        for letter in letters:
                            index = index * 26 + ord(letter) - 64
                        if index > 200:
                            raise ValueError("报表列数超过200")
                        while len(values) < index:
                            values.append("")
                        value = c.findtext(ns + "v", "")
                        if c.get("t") == "s":
                            value = strings[int(value)]
                        elif c.get("t") == "inlineStr":
                            value = "".join(c.find(ns + "is").itertext())
                        values[index - 1] = value
                    yield values
                    e.clear()
    else:
        raise ValueError("旧版XLS格式暂不支持预览，请下载原文件并转换为CSV/XLSX")


def import_report(engine, job, path):
    meta.create_all(engine)
    # Validate and load into an invisible version. The existing report remains
    # readable until one short transaction swaps the completed version in.
    stage = uuid.uuid4().hex
    with engine.begin() as db:
        db.execute(documents.insert().values(id=stage, ready=0, created=time.time()))
    try:
        return _stage_report(engine, job, path, stage)
    finally:
        try:
            with engine.begin() as db:
                db.execute(delete(rows).where(rows.c.job_id == stage))
                db.execute(delete(documents).where(documents.c.id == stage))
        except Exception:
            logging.getLogger("ecmp.center").exception("报表暂存清理延后，下轮自动恢复：%s", stage)


def _invalidate_accounting(db, jid):
    """Keep pinned versions, but never pair new source rows with old amounts."""
    from .reconciliation import bindings, results
    from .profit_tax import reports, allocations

    tables = set(inspect(db).get_table_names())
    if bindings.name in tables:
        db.execute(bindings.update().where(bindings.c.job_id == jid).values(summary=None, updated=time.time()))
    if results.name in tables:
        db.execute(delete(results).where(results.c.job_id == jid))
    if reports.name in tables:
        db.execute(reports.update().where(reports.c.job_id == jid).values(signature="", summary={}))
    if allocations.name in tables:
        db.execute(delete(allocations).where(allocations.c.job_id == jid))


def _stage_report(engine, job, path, stage):
    headers = None
    count = 0
    batch = []
    with zipfile.ZipFile(path) as archive:
        members = [
            i
            for i in archive.infolist()
            if "/" not in i.filename and i.filename.lower().endswith((".csv", ".xlsx", ".xls"))
        ]
        if not members or sum(i.file_size for i in members) > MAX_BYTES:
            raise ValueError("未找到有效报表或解压大小超限")
        for member in members:
            iterator = iter(sheet_rows(archive.read(member), "." + member.filename.rsplit(".", 1)[1].lower()))
            current = next((list(map(cell, r)) for r in iterator if any(r)), None)
            if not current or len(current) > 200 or "订单号" not in current:
                raise ValueError("未识别到订单号表头，原文件已保留")
            if headers is None:
                headers = current
            elif headers != current:
                raise ValueError("分片报表列不一致，原文件已保留")

            def get(values, name):
                return values[headers.index(name)] if name in headers else ""

            for raw in iterator:
                if not any(raw):
                    continue
                values = list(map(cell, raw))
                if len(values) > len(headers):
                    raise ValueError("报表行列数量不一致")
                values += [""] * (len(headers) - len(values))
                count += 1
                batch.append(
                    {
                        "job_id": stage,
                        "position": count,
                        "cells": values,
                        "order_number": get(values, "订单号"),
                        "status": get(values, "订单状态"),
                        "amount": number(get(values, "商家实收金额(元)")),
                        "quantity": number(get(values, "商品数量(件)") or get(values, "商品数量")),
                        "search": " ".join(values),
                    }
                )
                if len(batch) >= 1000:
                    with engine.begin() as db:
                        db.execute(rows.insert(), batch)
                    batch = []
        with engine.begin() as db:
            if batch:
                db.execute(rows.insert(), batch)
            existing = db.execute(select(documents.c.id).where(documents.c.id == job["id"]).with_for_update()).first()
            db.execute(delete(rows).where(rows.c.job_id == job["id"]))
            db.execute(rows.update().where(rows.c.job_id == stage).values(job_id=job["id"]))
            value = dict(
                shop_id=job["shop_id"],
                shop_name=job["shop_name"],
                start=job["start"],
                end=job["end"],
                created=job["created"],
                headers=headers,
                row_count=count,
                ready=1,
            )
            if existing:
                db.execute(documents.update().where(documents.c.id == job["id"]).values(**value))
                _invalidate_accounting(db, job["id"])
            else:
                db.execute(documents.insert().values(id=job["id"], **value))
    return count


def selected_documents(db, jobs=None, month="", shop_id=0, history=False):
    where = [documents.c.ready == 1]
    if jobs is not None:
        where.append(documents.c.id.in_(jobs))
    if shop_id:
        where.append(documents.c.shop_id == shop_id)
    if month:
        start, end = month_dates(month)
        where.extend((documents.c.start == start, documents.c.end == end))
    latest = {}
    result = []
    for doc in db.execute(
        select(documents).where(*where).order_by(documents.c.created.desc(), documents.c.id.desc())
    ).mappings():
        if history:
            result.append(dict(doc))
        else:
            latest.setdefault((doc["shop_id"], doc["start"], doc["end"]), dict(doc))
    return result if history else list(latest.values())


def available_report_ids(db):
    """Use the same visibility rules for detail, current costs and table export."""
    from .database import settings

    if not inspect(db).has_table(settings.name):
        return None
    records = list(db.execute(select(settings.c.key, settings.c.value).where(settings.c.key.like("report-export%"))))
    deleted = {
        key.removeprefix("report-export-delete:") for key, _ in records if key.startswith("report-export-delete:")
    }
    return [
        value["id"]
        for key, value in records
        if key.startswith("report-export:")
        and value.get("id") not in deleted
        and value.get("import_state") == "ready"
        and value.get("kind", "orders") == "orders"
        and value.get("state") != "deleting"
    ]


def cleanup_staging(engine):
    with engine.begin() as db:
        stale = list(
            db.execute(
                select(documents.c.id).where(documents.c.ready == 0, documents.c.created < time.time() - 86400)
            ).scalars()
        )
        if stale:
            db.execute(delete(rows).where(rows.c.job_id.in_(stale)))
            db.execute(delete(documents).where(documents.c.id.in_(stale), documents.c.ready == 0))


def query(
    engine,
    jobs,
    month="",
    shop_id=0,
    keyword="",
    status="",
    sort_by=-1,
    direction="asc",
    page=1,
    page_size=50,
    metadata_only=False,
):
    with engine.connect() as db:
        docs = selected_documents(db, jobs, month, shop_id)
        ids = [d["id"] for d in docs]
        headers = docs[0]["headers"] if docs else []
        if any(d["headers"] != headers for d in docs):
            raise ValueError("报表列结构不一致，请分别选择店铺查看")
        where = [rows.c.job_id.in_(ids)]
        if keyword:
            where.append(rows.c.search.contains(keyword, autoescape=True))
        if status:
            where.append(rows.c.status == status)
        joined = rows.join(documents, rows.c.job_id == documents.c.id)
        total = db.execute(select(func.count()).select_from(joined).where(*where)).scalar()
        order = documents.c.shop_name
        if 0 <= sort_by < len(headers):
            label = headers[sort_by]
            order = (
                rows.c.amount
                if label == "商家实收金额(元)"
                else rows.c.quantity
                if label in ("商品数量", "商品数量(件)")
                else rows.c.cells[sort_by].as_string()
            )
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        result = (
            []
            if metadata_only
            else db.execute(
                select(documents.c.shop_name, rows.c.cells, rows.c.position, rows.c.job_id)
                .select_from(joined)
                .where(*where)
                .order_by(order.desc() if direction == "desc" else order.asc(), rows.c.job_id, rows.c.position)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).mappings()
        )
        states = db.execute(select(rows.c.status).where(rows.c.job_id.in_(ids)).distinct()).scalars().all()
        return {
            "headers": headers,
            "rows": [dict(r) for r in result],
            "total": total,
            "page": page,
            "pages": pages,
            "statuses": states,
            "reports": [{k: d[k] for k in ("id", "shop_name", "created", "row_count", "start", "end")} for d in docs],
        }
