"""Monthly operating profit with pinned source fingerprints and explicit review."""

import hashlib, json, time
from decimal import Decimal
from sqlalchemy import MetaData, Table, Column, Integer, String, JSON, select
from sqlalchemy.exc import IntegrityError
from fastapi import Depends, HTTPException, Body
from .report_view import month_dates, number, selected_documents, available_report_ids
from .reconciliation import bindings
from .promotion_ledger import bills

meta = MetaData()
reviews = Table(
    "monthly_profit_reviews",
    meta,
    Column("shop_id", Integer, primary_key=True),
    Column("month", String(7), primary_key=True),
    Column("revision", Integer),
    Column("draft", JSON),
    Column("checked", JSON),
)
KINDS = {"income_add": 1, "refund": -1, "cost_add": -1, "cost_return": 1, "platform_fee": -1, "other_fee": -1}
CHECKS = ("scope", "refunds", "fees", "promotion", "shipping")


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def clean(value):
    if not isinstance(value, dict):
        raise ValueError("核对内容无效")
    lines = value.get("lines", [])
    if not isinstance(lines, list) or len(lines) > 500:
        raise ValueError("最多保存500条核对项")
    result = []
    seen = set()
    for line in lines:
        if not isinstance(line, dict):
            raise ValueError("核对项无效")
        kind = line.get("kind")
        amount = number(line.get("amount"))
        ref = str(line.get("reference", "")).strip()
        note = str(line.get("note", "")).strip()
        if kind not in KINDS or amount is None or amount < 0 or amount.as_tuple().exponent < -6:
            raise ValueError("核对金额必须为非负数，最多6位小数")
        if not ref or len(ref) > 200 or len(note) > 1000:
            raise ValueError("每条核对项需要凭据编号（最多200字）")
        key = ("fee" if kind in ("platform_fee", "other_fee") else kind, ref.casefold())
        if key in seen:
            raise ValueError("同类凭据重复，不能重复计入")
        seen.add(key)
        result.append(
            dict(kind=kind, amount=str(amount), reference=ref, note=note, included=line.get("included") is True)
        )
    shipping = value.get("actual_shipping")
    if shipping in ("", None):
        shipping = None
    else:
        shipping = number(shipping)
        if shipping is None or shipping < 0 or shipping.as_tuple().exponent < -6:
            raise ValueError("实际快递费无效")
        shipping = str(shipping)
    note = str(value.get("evidence", "")).strip()
    if len(note) > 3000:
        raise ValueError("核对依据最多3000字")
    flags = value.get("checks", {})
    if not isinstance(flags, dict):
        raise ValueError("核对状态无效")
    return dict(lines=result, actual_shipping=shipping, evidence=note, checks={k: flags.get(k) is True for k in CHECKS})


def calculate(source, draft):
    s = source.get("accounting") or {}
    summary = s.get("summary") or {}
    promotion = source.get("promotion")
    issues = []
    if not source.get("report"):
        issues.append("缺少本月完整订单报表")
    if not s:
        issues.append("成本快照尚未建立")
    if summary.get("unmatched", 0):
        issues.append(f"{summary['unmatched']} 行商品未匹配成本或数量无效")
    if s and not summary.get("revenue_complete"):
        issues.append("实收金额口径待核验")
    cash = [r for r in (promotion or {}).get("rows", []) if r[1] == "现金"]
    spend = number(cash[0][5]) if len(cash) == 1 else None
    if spend is None:
        issues.append("缺少现金本月支出，不能按零计算")
    shipping = number(draft.get("actual_shipping"))
    if shipping is None and summary.get("shipping_complete"):
        shipping = number(summary.get("shipping"))
    if shipping is None:
        issues.append("快递费不完整")
    adjustment = sum((number(r["amount"]) * KINDS[r["kind"]] for r in draft["lines"] if not r["included"]), Decimal(0))
    ready = bool(source.get("report") and summary.get("complete") and spend is not None and shipping is not None)
    value = (
        number(summary.get("revenue")) - number(summary.get("product_cost")) - shipping - spend + adjustment
        if ready
        else None
    )
    return dict(
        revenue=summary.get("revenue") if summary.get("revenue_complete") else None,
        product_cost=summary.get("product_cost") if not summary.get("unmatched") and s else None,
        shipping=str(shipping) if shipping is not None else None,
        shipping_actual=draft.get("actual_shipping") is not None,
        promotion=str(spend) if spend is not None else None,
        adjustment=str(adjustment),
        estimated=str(value) if value is not None else None,
        issues=issues,
        unmatched=summary.get("unmatched", 0),
        book_name=summary.get("book_name"),
        book_id=s.get("book_id"),
    )


def sources(db, month, shop_id=0):
    start, end = month_dates(month)
    result = {}
    docs = selected_documents(db, available_report_ids(db), month, shop_id)
    for doc in docs:
        sid = doc["shop_id"]
        if sid not in result:
            result[sid] = {"shop_id": sid, "shop_name": doc["shop_name"], "month": month, "report": dict(doc)}
    ids = [v["report"]["id"] for v in result.values()]
    snapshots = {
        s["job_id"]: dict(s) for s in db.execute(select(bindings).where(bindings.c.job_id.in_(ids))).mappings()
    }
    from .profit_tax import reports as tax_reports

    taxes = {
        s["job_id"]: dict(s) for s in db.execute(select(tax_reports).where(tax_reports.c.job_id.in_(ids))).mappings()
    }
    for v in result.values():
        v["accounting"] = snapshots.get(v["report"]["id"])
        v["tax"] = taxes.get(v["report"]["id"])
    for bill in db.execute(
        select(bills)
        .where(*([bills.c.shop_id == str(shop_id)] if shop_id else []))
        .order_by(bills.c.created.desc(), bills.c.job_id.desc())
    ).mappings():
        records = [r for r in bill["rows"] if r[0] == month]
        if not records:
            continue
        sid = int(bill["shop_id"])
        v = result.setdefault(sid, {"shop_id": sid, "shop_name": bill["shop_name"], "month": month})
        v.setdefault("promotion", dict(job_id=bill["job_id"], created=bill["created"], rows=records))
    return result


def present(source, review=None):
    draft = (review or {}).get("draft") or clean({})
    checked = (review or {}).get("checked")
    signature = fingerprint({"source": source, "draft": draft})
    calc = calculate(source, draft)
    tax = (source.get("tax") or {}).get("summary") or {}
    deduction = number(tax.get("deduction"))
    before = number(calc["estimated"])
    calc.update(
        tax=tax,
        tax_version=(source.get("tax") or {}).get("version_id"),
        tax_estimated=tax.get("estimated"),
        tax_actual=tax.get("actual"),
        tax_deduction=tax.get("deduction"),
        after_tax=str(before - deduction) if before is not None and deduction is not None else None,
    )
    calc["issues"] += tax.get("issues", [])
    valid = bool(checked and checked["fingerprint"] == signature)
    return dict(
        shop_id=source["shop_id"],
        shop_name=source["shop_name"],
        month=source["month"],
        report_id=(source.get("report") or {}).get("id"),
        revision=(review or {}).get("revision", 0),
        draft=draft,
        fingerprint=signature,
        calculation=calc,
        checked=checked,
        status="checked" if valid else "stale" if checked else "estimated",
        checked_profit=checked["calculation"]["estimated"] if valid else None,
        checked_tax_profit=checked["calculation"].get("after_tax") if valid and tax.get("actual") is not None else None,
    )


def install(app, database, admin):
    meta.create_all(database.engine)
    from .profit_tax import install as install_tax, sync as sync_tax

    install_tax(app, database, admin)

    @app.get("/api/profit", dependencies=[Depends(admin)])
    def view(month: str, shop_id: int = 0):
        try:
            month_dates(month)
            sync_tax(database.engine, month=month, shop_id=shop_id)
            with database.engine.connect() as db:
                data = sources(db, month, shop_id)
                old = {
                    r["shop_id"]: dict(r)
                    for r in db.execute(select(reviews).where(reviews.c.month == month)).mappings()
                }
                return {"items": [present(v, old.get(sid)) for sid, v in data.items()]}
        except ValueError as e:
            raise HTTPException(400, str(e))

    def save(shop_id, month, body, confirm):
        try:
            month_dates(month)
            if type(body.get("revision")) is not int:
                raise ValueError("缺少配置版本，请刷新")
            draft = clean(body.get("draft", {})) if not confirm else None
            sync_tax(database.engine, month=month, shop_id=shop_id)
            with database.engine.begin() as db:
                where = (reviews.c.shop_id == shop_id, reviews.c.month == month)
                old = db.execute(select(reviews).where(*where).with_for_update()).mappings().first()
                revision = old["revision"] if old else 0
                if revision != body["revision"]:
                    raise HTTPException(409, "配置已被修改，请刷新后重试")
                source = sources(db, month, shop_id).get(shop_id)
                if not source:
                    raise HTTPException(404, "尚无该店铺当月报表或账单")
                checked = old["checked"] if old else None
                if confirm:
                    current = present(source, old)
                    draft = current["draft"]
                    if body.get("fingerprint") != current["fingerprint"]:
                        raise HTTPException(409, "报表、成本或账单已变化，请刷新并重新核对")
                    if (
                        current["calculation"]["estimated"] is None
                        or not all(draft["checks"].values())
                        or draft["actual_shipping"] is None
                        or not draft["evidence"]
                    ):
                        raise HTTPException(400, "请补齐成本、现金支出及实际快递费，完成全部核对并填写依据")
                    checked = dict(
                        fingerprint=current["fingerprint"],
                        calculation=current["calculation"],
                        source=source,
                        draft=draft,
                        at=time.time(),
                    )
                values = dict(revision=revision + 1, draft=draft, checked=checked)
                if old:
                    changed = db.execute(
                        reviews.update().where(*where, reviews.c.revision == revision).values(**values)
                    )
                    if changed.rowcount != 1:
                        raise HTTPException(409, "配置已被修改，请刷新")
                else:
                    db.execute(reviews.insert().values(shop_id=shop_id, month=month, **values))
            sync_tax(database.engine, month=month, shop_id=shop_id)
            with database.engine.connect() as db:
                return present(sources(db, month, shop_id)[shop_id], values)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except IntegrityError:
            raise HTTPException(409, "配置已被修改，请刷新")

    @app.put("/api/profit/{shop_id}/{month}", dependencies=[Depends(admin)])
    def draft(shop_id: int, month: str, body: dict = Body(...)):
        return save(shop_id, month, body, False)

    @app.post("/api/profit/{shop_id}/{month}/confirm", dependencies=[Depends(admin)])
    def confirm(shop_id: int, month: str, body: dict = Body(...)):
        return save(shop_id, month, body, True)
