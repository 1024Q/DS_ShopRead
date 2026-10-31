"""Versioned reconciliation snapshots. Old cost books cannot change historical profit."""

import threading, time
from collections import defaultdict
from decimal import Decimal
from sqlalchemy import MetaData, Table, Column, String, Float, JSON, Integer, select, delete
from .report_view import documents, rows, number

meta = MetaData()
bindings = Table(
    "report_cost_bindings",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("book_id", String(32), nullable=True),
    Column("income_mode", String(20)),
    Column("updated", Float),
    Column("summary", JSON),
)
results = Table(
    "report_cost_results",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("cost", JSON),
)
lock = threading.RLock()
D = Decimal


def cost_index(costs):
    index = {}
    conflicts = set()
    for sku, entry in costs.items():
        key = sku.strip().casefold()
        if key in index:
            conflicts.add(key)
        else:
            index[key] = entry
    for key in conflicts:
        index.pop(key, None)
    return index, conflicts


def surcharge(product):
    return any(word in product for word in ("补差价", "补收差价", "补拍差价"))


def calculate(records, headers, costs, mapping, income_mode="unverified"):
    def get(r, *names):
        return next((str(r["cells"][i]).strip() for i, h in enumerate(headers) if h in names), "")

    work = []
    orders = defaultdict(list)
    freights = defaultdict(list)
    index, conflicts = cost_index(costs)
    for r in records:
        sku = get(r, "商家编码-规格维度", "商家编码－规格维度")
        key = sku.casefold()
        extra = surcharge(get(r, "商品"))
        entry = None if extra else index.get(key)
        qty = number(get(r, "商品数量(件)", "商品数量"))
        amount = number(get(r, "商家实收金额(元)"))
        unit = entry["unit_cost"] if entry else None
        state = (
            "补差价收入，全部计入利润"
            if extra
            else "SKU大小写归一后存在重复成本编码"
            if key in conflicts
            else "SKU为空"
            if not sku
            else "未匹配成本"
            if unit is None
            else "数量无效"
            if qty is None or qty < 0
            else "已匹配"
        )
        w = {
            "position": r["position"],
            "sku": sku,
            "product": get(r, "商品"),
            "spec": get(r, "商品规格"),
            "order": get(r, "订单号"),
            "quantity": qty,
            "unit": unit,
            "total": unit * qty if state == "已匹配" else None,
            "amount": amount,
            "state": state,
            "shipping": None,
            "shipping_reason": "补差价商品，暂不自动计算" if extra else "未配置快递费",
        }
        if extra:
            w.update(unit=D(0), total=D(0), shipping=D(0), shipping_reason="补差价收入，不计商品成本和快递费")
        work.append(w)
        orders[w["order"] or "missing:" + str(w["position"])].append(w)
        col = mapping.get("shipping_column")
        fee = number(entry["cells"][col]) if entry and col is not None else None
        if fee is not None and qty is not None and qty >= 0:
            mode = mapping.get("shipping_mode", "piece")
            if mode == "piece":
                w.update(shipping=fee * qty, shipping_reason="按件")
            else:
                key = w["order"] if mode == "order" else get(r, "快递单号")
                if key:
                    freights[(mode, key)].append((w, fee))
                else:
                    w["shipping_reason"] = "缺少订单号" if mode == "order" else "缺少快递单号"
    ambiguous = 0
    for group in orders.values():
        if len(group) <= 1:
            continue
        if income_mode == "order":
            amounts = {r["amount"] for r in group}
            if len(amounts) != 1 or None in amounts:
                for r in group:
                    r["amount"] = None
                ambiguous += len(group)
                continue
            total = next(iter(amounts))
            remaining = total
            weights = [max(r["quantity"] or D(0), D(0)) for r in group]
            den = sum(weights)
            if not den:
                weights = [D(1)] * len(group)
                den = D(len(group))
            for i, r in enumerate(group):
                value = (total * weights[i] / den).quantize(D(".000001")) if i < len(group) - 1 else remaining
                r["amount"] = value
                remaining -= value
        elif income_mode != "row":
            # Never assume repeated order rows contain line-level revenue.
            ambiguous += len(group)
            for r in group:
                r["amount"] = None
    for (mode, key), group in freights.items():
        # A parcel/order has one estimated charge. Differing SKU tariffs use
        # the maximum once, shown explicitly, not the sum of each SKU's fee.
        amount = max(f for _, f in group)
        for i, (r, _) in enumerate(sorted(group, key=lambda x: x[0]["position"])):
            r.update(
                shipping=amount if i == 0 else D(0),
                shipping_reason="每单取最高费一次" if mode == "order" else "每包裹取最高费一次",
            )
    summary = {
        "rows": len(work),
        "matched": 0,
        "unmatched": 0,
        "ambiguous_income_rows": ambiguous,
        "revenue": D(0),
        "product_cost": D(0),
        "shipping": D(0),
        "matched_profit": D(0),
        "unmatched_revenue": D(0),
        "complete": True,
        "shipping_complete": True,
        "revenue_complete": True,
    }
    for w in work:
        if w["amount"] is None:
            summary["complete"] = False
            summary["revenue_complete"] = False
        else:
            summary["revenue"] += w["amount"]
        if w["total"] is None:
            summary["unmatched"] += 1
            summary["complete"] = False
            summary["unmatched_revenue"] += w["amount"] or D(0)
        else:
            summary["matched"] += 1
            summary["product_cost"] += w["total"]
        w["profit"] = w["amount"] - w["total"] if w["total"] is not None and w["amount"] is not None else None
        if w["profit"] is not None:
            summary["matched_profit"] += w["profit"]
        if w["shipping"] is None:
            summary["shipping_complete"] = False
        else:
            summary["shipping"] += w["shipping"]
        w["net"] = w["profit"] - w["shipping"] if w["profit"] is not None and w["shipping"] is not None else None
    summary["profit"] = summary["revenue"] - summary["product_cost"] if summary["complete"] else None
    summary["net"] = (
        summary["profit"] - summary["shipping"] if summary["complete"] and summary["shipping_complete"] else None
    )

    def serial(d):
        return {k: str(v) if isinstance(v, D) else v for k, v in d.items()}

    return [serial(w) for w in work], serial(summary)


def snapshot(
    engine, jid, recalculate=False, income_mode=None, refresh_rules=False, book_override=None, expected_book=None
):
    from .cost_tables import books, entries, catalog

    if not recalculate and not refresh_rules:
        with engine.connect() as db:
            old = db.execute(select(bindings).where(bindings.c.job_id == jid)).mappings().first()
            if (
                old
                and old["summary"] is not None
                and (old["book_id"] or not db.execute(select(catalog.c.active_id).where(catalog.c.id == 1)).scalar())
            ):
                return dict(old)
    with lock, engine.begin() as db:
        # Serialize both cost activation and snapshot binding across processes.
        db.execute(select(catalog.c.id).where(catalog.c.id == 1).with_for_update()).first()
        doc = (
            db.execute(select(documents).where(documents.c.id == jid, documents.c.ready == 1).with_for_update())
            .mappings()
            .first()
        )
        if not doc:
            raise ValueError("报表尚未导入")
        old = db.execute(select(bindings).where(bindings.c.job_id == jid)).mappings().first()
        if book_override and old and old["book_id"] == book_override:
            return dict(old)
        if book_override and (not old or old["book_id"] != expected_book):
            raise ValueError("报表成本版本已变化，请核对后重新提交补充成本")
        active = db.execute(select(catalog.c.active_id).where(catalog.c.id == 1)).scalar()
        if (
            old
            and old["summary"] is not None
            and not recalculate
            and not refresh_rules
            and (old["book_id"] or not active)
        ):
            return dict(old)
        bid = book_override or (active if recalculate or not old or not old["book_id"] else old["book_id"])
        book = db.execute(select(books).where(books.c.id == bid)).mappings().first() if bid else None
        mode = income_mode or (old["income_mode"] if old else "unverified")
        if mode not in ("unverified", "row", "order"):
            raise ValueError("收入口径无效")
        costs = (
            {r["sku"]: dict(r) for r in db.execute(select(entries).where(entries.c.book_id == bid)).mappings()}
            if book
            else {}
        )
        records = [
            dict(r)
            for r in db.execute(
                select(rows.c.position, rows.c.cells).where(rows.c.job_id == jid).order_by(rows.c.position)
            ).mappings()
        ]
        from .cost_supplements import sku_links

        links = {
            (r["product"], r["spec"]): r["sku"]
            for r in db.execute(select(sku_links).where(sku_links.c.shop_id == doc["shop_id"])).mappings()
        }
        headers = doc["headers"]
        si = next((i for i, h in enumerate(headers) if h in ("商家编码-规格维度", "商家编码－规格维度")), None)
        manual = {}
        if si is not None and "商品" in headers and "商品规格" in headers:
            for record in records:
                cells = record["cells"]
                key = (str(cells[headers.index("商品")]).strip(), str(cells[headers.index("商品规格")]).strip())
                if not str(cells[si] or "").strip() and key in links:
                    record["cells"] = list(cells)
                    record["cells"][si] = links[key]
                    manual[record["position"]] = links[key]
        values, summary = calculate(records, headers, costs, book["mapping"] if book else {}, mode)
        for value in values:
            if value["position"] in manual:
                value["manual_sku"] = manual[value["position"]]
                value["state"] += "（手动SKU：" + manual[value["position"]] + "）"
        summary.update(
            book_name=book["name"] if book else None,
            shop_name=doc["shop_name"],
            shop_id=doc["shop_id"],
            start=doc["start"],
            end=doc["end"],
        )
        db.execute(delete(results).where(results.c.job_id == jid))
        for i in range(0, len(values), 1000):
            db.execute(
                results.insert(), [{"job_id": jid, "position": w["position"], "cost": w} for w in values[i : i + 1000]]
            )
        data = {"job_id": jid, "book_id": bid, "income_mode": mode, "updated": time.time(), "summary": summary}
        if old:
            db.execute(bindings.update().where(bindings.c.job_id == jid).values(**data))
        else:
            db.execute(bindings.insert().values(**data))
        return data


def enrich_reports(engine, result):
    from .profit_tax import sync, reports as tax_reports

    sync(engine, [d["id"] for d in result["reports"]])
    with engine.connect() as db:
        snapshots = {
            r["job_id"]: dict(r)
            for r in db.execute(
                select(bindings).where(bindings.c.job_id.in_([d["id"] for d in result["reports"]]))
            ).mappings()
        }
        taxes = {
            r["job_id"]: dict(r)
            for r in db.execute(select(tax_reports).where(tax_reports.c.job_id.in_(snapshots))).mappings()
        }
        for jid, snapshot in snapshots.items():
            snapshot["tax"] = taxes.get(jid)
    result["accounting_pending"] = [
        r["shop_name"] for r in result["reports"] if not (snapshots.get(r["id"]) or {}).get("summary")
    ]
    result["accounting"] = [s for s in snapshots.values() if s.get("summary")]
    result["cost_book"] = {"name": "按报表锁定的成本版本"}
    if not result["rows"]:
        return result
    with engine.connect() as db:
        for jid in snapshots:
            positions = [r["position"] for r in result["rows"] if r["job_id"] == jid]
            costs = dict(
                db.execute(
                    select(results.c.position, results.c.cost).where(
                        results.c.job_id == jid, results.c.position.in_(positions)
                    )
                ).all()
            )
            for r in result["rows"]:
                if r["job_id"] == jid:
                    r["cost"] = costs.get(r["position"])
    return result


def unmatched(engine, jids, page=1):
    from sqlalchemy import func

    where = [results.c.job_id.in_(jids), results.c.cost["total"].as_string().is_(None)]
    with engine.connect() as db:
        total = db.execute(select(func.count()).select_from(results).where(*where)).scalar()
        pages = max(1, (total + 49) // 50)
        page = min(page, pages)
        data = (
            db.execute(
                select(results.c.job_id, results.c.cost)
                .where(*where)
                .order_by(results.c.job_id, results.c.position)
                .offset((page - 1) * 50)
                .limit(50)
            )
            .mappings()
            .all()
        )
        return {"rows": [dict(r) for r in data], "total": total, "page": page, "pages": pages}
