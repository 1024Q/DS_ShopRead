"""Import native monthly bills; cash monthly outflow is the agreed promotion expense."""

import zipfile, re
from sqlalchemy import MetaData, Table, Column, String, JSON, Float, select, delete
from fastapi import Depends, HTTPException
from .report_view import sheet_rows, cell, number, month_dates
from ..report_export_store import MAX_BYTES

meta = MetaData()
bills = Table(
    "promotion_monthly_bills",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("shop_id", String(32)),
    Column("shop_name", String(200)),
    Column("month", String(7)),
    Column("created", Float),
    Column("headers", JSON),
    Column("rows", JSON),
    Column("summary", JSON),
)
HEADERS = ["月份", "资金类型", "本月余额", "上月余额", "本月收入", "本月支出"]


def parse(data, suffix, month):
    if month:
        month_dates(month)
    if suffix == ".xls":
        import xlrd

        book = xlrd.open_workbook(file_contents=data, on_demand=True)
        try:
            if book.nsheets != 1:
                raise ValueError("月账单包含多个工作表，请核对原文件")
            sheet = book.sheet_by_index(0)
            if sheet.nrows > 100000 or sheet.ncols > 30:
                raise ValueError("月账单行列数超限")
            data_rows = [sheet.row_values(i) for i in range(sheet.nrows)]
        finally:
            book.release_resources()
    else:
        data_rows = list(sheet_rows(data, suffix))
    iterator = iter(data_rows)
    headers = next((list(map(cell, r)) for r in iterator if any(r)), [])
    normalized = [re.sub(r"\s|[（(]元[)）]", "", h) for h in headers]
    if not all(h in normalized for h in HEADERS):
        raise ValueError("月账单表头变化，未导入；请查看原文件")
    records = []
    funds = set()
    from decimal import Decimal

    summary = {k: Decimal(0) for k in HEADERS[2:]}
    for raw in iterator:
        if not any(raw):
            continue
        if len(raw) != len(headers):
            raise ValueError("月账单行列数不一致")
        values = [cell(raw[normalized.index(h)]).strip() for h in HEADERS]
        row_month = values[0].replace("/", "-")
        month_dates(row_month)
        if month and row_month != month:
            continue
        fund = values[1]
        if fund == "全部":
            continue  # Native total rows must not double count fund rows.
        if not fund or (row_month, fund) in funds:
            raise ValueError("同月份资金类型重复，请核验原文件")
        funds.add((row_month, fund))
        amounts = [number(v) for v in values[2:]]
        if any(v is None and text not in ("", "-", "—", "--") for v, text in zip(amounts, values[2:])):
            raise ValueError("月账单金额格式无效")
        closing, opening, income, outflow = amounts
        if all(v is not None for v in amounts) and abs(closing - (opening + income - outflow)) > Decimal(".01"):
            raise ValueError("月账单余额公式不一致，请核对原文件")
        records.append([row_month, fund, *[str(v) if v is not None else None for v in amounts]])
        for name, value in zip(HEADERS[2:], amounts):
            summary[name] = summary[name] + value if summary[name] is not None and value is not None else None
    if not records:
        raise ValueError("平台下载文件中没有可导入的账单记录；平台通常每月10日生成上月账单")
    return records, {k: str(v) if v is not None else None for k, v in summary.items()}


def import_report(engine, job, path):
    meta.create_all(engine)
    with zipfile.ZipFile(path) as z:
        files = [
            i for i in z.infolist() if "/" not in i.filename and i.filename.lower().endswith((".xls", ".xlsx", ".csv"))
        ]
        if len(files) != 1 or files[0].file_size > MAX_BYTES:
            raise ValueError("未找到唯一有效月账单文件")
        records, summary = parse(z.read(files[0]), "." + files[0].filename.rsplit(".", 1)[-1].lower(), "")
    with engine.begin() as db:
        db.execute(delete(bills).where(bills.c.job_id == job["id"]))
        db.execute(
            bills.insert().values(
                job_id=job["id"],
                shop_id=str(job["shop_id"]),
                shop_name=job["shop_name"],
                month="",
                created=job["created"],
                headers=HEADERS,
                rows=records,
                summary=summary,
            )
        )
    return len(records)


def install(app, database, admin):
    meta.create_all(database.engine)

    @app.get("/api/promotion-ledger", dependencies=[Depends(admin)])
    def view(month: str = "", shop_id: int = 0):
        if month:
            try:
                month_dates(month)
            except ValueError as e:
                raise HTTPException(400, str(e))
        with database.engine.connect() as db:
            where = []
            if shop_id:
                where.append(bills.c.shop_id == str(shop_id))
            found = db.execute(select(bills).where(*where).order_by(bills.c.created.desc())).mappings()
            latest = {}
            for b in found:
                groups = {}
                for row in b["rows"]:
                    if not month or row[0] == month:
                        groups.setdefault(row[0], []).append(row)
                for period, records in groups.items():
                    latest.setdefault(
                        (b["shop_id"], period),
                        {
                            **dict(b),
                            "month": period,
                            "rows": records,
                            "summary": {
                                name: str(sum((number(r[i]) for r in records), number("0")))
                                if all(number(r[i]) is not None for r in records)
                                else None
                                for i, name in enumerate(HEADERS)
                                if i >= 2
                            },
                        },
                    )
        return {
            "bills": list(latest.values()),
            "headers": HEADERS,
            "notice": "推广消耗按现金行的本月支出计入店铺月度利润；红包、虚拟金和充值不重复扣除。",
        }
