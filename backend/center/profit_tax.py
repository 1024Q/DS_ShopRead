"""Versioned, center-only tax estimates. A report never silently changes tax policy."""

import hashlib, json, time, uuid
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import MetaData, Table, Column, String, Integer, Float, JSON, Numeric, select, delete, cast, tuple_
from sqlalchemy.exc import IntegrityError
from fastapi import Depends, HTTPException, Body
from .report_view import documents, month_dates, number, selected_documents
from .reconciliation import bindings

meta = MetaData()
versions = Table(
    "profit_tax_versions",
    meta,
    Column("id", String(32), primary_key=True),
    Column("shop_id", Integer),
    Column("month", String(7)),
    Column("created", Float),
    Column("config", JSON),
)
policies = Table(
    "profit_tax_policies",
    meta,
    Column("shop_id", Integer, primary_key=True),
    Column("month", String(7), primary_key=True),
    Column("revision", Integer),
    Column("version_id", String(32)),
)
reports = Table(
    "report_tax_bindings",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("version_id", String(32), nullable=True),
    Column("signature", String(64)),
    Column("summary", JSON),
)
allocations = Table(
    "report_tax_allocations",
    meta,
    Column("job_id", String(32), primary_key=True),
    Column("position", Integer, primary_key=True),
    *[Column(k, Numeric(24, 2)) for k in ("base", "estimated", "actual", "included", "deduction")],
)


def allocate(db, jid, summary):
    from .reconciliation import results

    db.execute(delete(allocations).where(allocations.c.job_id == jid))
    if summary.get("rate") is None:
        return
    records = list(
        db.execute(
            select(results.c.position, cast(results.c.cost["amount"].as_string(), Numeric(24, 6)))
            .where(results.c.job_id == jid)
            .order_by(results.c.position)
        )
    )
    weights = [max(amount or Decimal(0), Decimal(0)) for _, amount in records]
    den = sum(weights)
    cumulative = Decimal(0)
    previous = {}
    batch = []
    for (position, _), weight in zip(records, weights):
        cumulative += weight
        value = dict(job_id=jid, position=position)
        for key in ("base", "estimated", "actual", "included", "deduction"):
            total = number(summary.get(key))
            rounded = (
                (total * cumulative / den).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
                if total is not None and den
                else Decimal(0)
                if total == 0
                else None
            )
            value[key] = rounded - previous.get(key, Decimal(0)) if rounded is not None else None
            previous[key] = rounded
        batch.append(value)
        if len(batch) >= 1000:
            db.execute(allocations.insert(), batch)
            batch = []
    if batch:
        db.execute(allocations.insert(), batch)


def clean(value, default=False):
    if not isinstance(value, dict):
        raise ValueError("税费配置无效")
    rate = number(value.get("rate"))
    if rate is None or not 0 <= rate <= 100 or rate.as_tuple().exponent < -6:
        raise ValueError("税点必须为0至100的百分比，最多6位小数")
    base = value.get("base", "net_receipts")
    if base not in ("receipts", "net_receipts"):
        raise ValueError("计税基数无效")
    actual = value.get("actual")
    actual = None if actual in ("", None) else number(actual)
    if value.get("actual") not in ("", None) and (actual is None or actual < 0 or actual.as_tuple().exponent < -2):
        raise ValueError("实际税费必须为非负金额，最多2位小数")
    included = number(value.get("included", "0"))
    if included is None or included < 0 or included.as_tuple().exponent < -2:
        raise ValueError("已扣税费必须为非负金额，最多2位小数")
    location = value.get("included_in", "revenue")
    if location not in ("revenue", "fees"):
        raise ValueError("请选择已扣税费的位置")
    reference = str(value.get("reference", "")).strip()
    evidence = str(value.get("evidence", "")).strip()
    if len(reference) > 200 or len(evidence) > 3000:
        raise ValueError("凭据编号或核对依据过长")
    if (actual is not None or included > 0) and (not reference or not evidence):
        raise ValueError("录入实际或已扣税费时，需要凭据编号和核对依据")
    if default and (actual is not None or included):
        raise ValueError("统一默认仅设置税点和基数，实际税费请按店铺填写")
    return dict(
        rate=str(rate),
        base=base,
        actual=str(actual) if actual is not None else None,
        included=str(included),
        included_in=location,
        reference=reference,
        evidence=evidence,
    )


def calculate(summary, draft, config):
    empty = dict(
        base=None,
        rate=None,
        estimated=None,
        actual=None,
        included=None,
        deduction=None,
        status="未设置",
        issues=["尚未设置税费，税后利润暂不计算"],
    )
    if not config:
        return empty
    revenue = number(summary.get("revenue")) if summary.get("revenue_complete") else None
    lines = draft.get("lines", [])
    issues = []
    # Only explicitly unreflected refunds reduce the base, never subtract included refunds again.
    refunds = sum((number(x["amount"]) for x in lines if x["kind"] == "refund" and not x["included"]), Decimal(0))
    income = sum((number(x["amount"]) for x in lines if x["kind"] == "income_add" and not x["included"]), Decimal(0))
    base = (
        max(Decimal(0), revenue + income - (refunds if config["base"] == "net_receipts" else 0))
        if revenue is not None
        else None
    )
    estimate = (
        (base * Decimal(config["rate"]) / 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if base is not None
        else None
    )
    actual = number(config.get("actual"))
    included = Decimal(config["included"])
    total = actual if actual is not None else estimate
    if base is None:
        issues.append("实收口径待核验，无法预估税费")
    fee = sum(
        (
            number(x["amount"])
            for x in lines
            if x["kind"] in ("platform_fee", "other_fee")
            and not x["included"]
            and x["reference"].casefold() == config["reference"].casefold()
        ),
        Decimal(0),
    )
    if included and config["included_in"] == "fees" and fee < included:
        issues.append("已扣税费在费用核对中没有足额同凭据记录，请先核对")
    if fee and config["included_in"] != "fees":
        issues.append("同凭据已计入费用，请将已扣税费位置设为费用核对，避免重复扣税")
    if fee and not included:
        issues.append("同凭据已计入费用，请填写其中已扣税额，避免重复扣税")
    if total is not None and included > total:
        issues.append("已扣税费超过本次税费，请核对退税或实际税额")
    deduction = total - included if total is not None and not issues else None
    return dict(
        base=str(base) if base is not None else None,
        rate=config["rate"],
        base_mode=config["base"],
        estimated=str(estimate) if estimate is not None else None,
        actual=config["actual"],
        included=str(included),
        deduction=str(deduction) if deduction is not None else None,
        status="待核对" if issues else "实际税费" if actual is not None else "预估税费",
        issues=issues,
    )


def resolve(db, sid, month):
    for shop in (sid, 0):
        version = db.execute(
            select(policies.c.version_id).where(policies.c.shop_id == shop, policies.c.month == month)
        ).scalar()
        if version:
            return version
    return None


def _signature(vid, accounting, review):
    summary = (accounting or {}).get("summary") or {}
    draft = (review or {}).get("draft") or {}
    return hashlib.sha256(
        json.dumps(
            [vid, (accounting or {}).get("updated"), summary, draft], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()


def sync(engine, job_ids=None, *, month="", shop_id=0):
    """Refresh amounts against pinned policy; only explicit apply switches versions."""
    from .profit import reviews

    with engine.connect() as db:
        docs = selected_documents(db, job_ids, month, shop_id, history=job_ids is not None)
        docs = [d for d in docs if d["start"] and (d["start"], d["end"]) == month_dates(d["start"][:7])]
        if not docs:
            return
        ids = [d["id"] for d in docs]
        pairs = {(d["shop_id"], d["start"][:7]) for d in docs}
        old_by_id = {r["job_id"]: r for r in db.execute(select(reports).where(reports.c.job_id.in_(ids))).mappings()}
        accounting_by_id = {
            r["job_id"]: r for r in db.execute(select(bindings).where(bindings.c.job_id.in_(ids))).mappings()
        }
        review_by_scope = {
            (r["shop_id"], r["month"]): r
            for r in db.execute(select(reviews).where(tuple_(reviews.c.shop_id, reviews.c.month).in_(pairs))).mappings()
        }
        policy_by_scope = {
            (r["shop_id"], r["month"]): r["version_id"]
            for r in db.execute(
                select(policies).where(
                    tuple_(policies.c.shop_id, policies.c.month).in_(pairs | {(0, m) for _, m in pairs})
                )
            ).mappings()
        }
        changed = []
        for doc in docs:
            old = old_by_id.get(doc["id"])
            scope = (doc["shop_id"], doc["start"][:7])
            vid = old["version_id"] if old else policy_by_scope.get(scope, policy_by_scope.get((0, scope[1])))
            signature = _signature(vid, accounting_by_id.get(doc["id"]), review_by_scope.get(scope))
            if not old or old["signature"] != signature:
                changed.append(doc["id"])
    # Unchanged page reads do not acquire row locks or write. Re-read after the
    # per-document lock so a concurrent cost/tax change cannot be overwritten.
    for jid in sorted(changed):
        with engine.begin() as db:
            doc = (
                db.execute(select(documents).where(documents.c.id == jid, documents.c.ready == 1).with_for_update())
                .mappings()
                .first()
            )
            if not doc:
                continue
            period = doc["start"][:7]
            old = db.execute(select(reports).where(reports.c.job_id == doc["id"])).mappings().first()
            vid = old["version_id"] if old else resolve(db, doc["shop_id"], period)
            config = db.execute(select(versions.c.config).where(versions.c.id == vid)).scalar() if vid else None
            accounting = db.execute(select(bindings).where(bindings.c.job_id == doc["id"])).mappings().first()
            review = (
                db.execute(select(reviews).where(reviews.c.shop_id == doc["shop_id"], reviews.c.month == period))
                .mappings()
                .first()
            )
            summary = (accounting or {}).get("summary") or {}
            draft = (review or {}).get("draft") or {}
            signature = _signature(vid, accounting, review)
            if old and old["signature"] == signature:
                continue
            data = dict(version_id=vid, signature=signature, summary=calculate(summary, draft, config))
            allocate(db, doc["id"], data["summary"])
            if old:
                db.execute(reports.update().where(reports.c.job_id == doc["id"]).values(**data))
            else:
                db.execute(reports.insert().values(job_id=doc["id"], **data))


def install(app, database, admin):
    engine = database.engine
    meta.create_all(engine)

    @app.get("/api/profit-tax", dependencies=[Depends(admin)])
    def listing(month: str):
        try:
            month_dates(month)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with engine.connect() as db:
            items = [
                dict(r)
                for r in db.execute(
                    select(
                        policies.c.shop_id, policies.c.revision, versions.c.id, versions.c.created, versions.c.config
                    )
                    .join(versions, policies.c.version_id == versions.c.id)
                    .where(policies.c.month == month)
                ).mappings()
            ]
            shops = {s["id"]: s["name"] for s in database.shops()} if hasattr(database, "shops") else {}
            for row in db.execute(select(documents.c.shop_id, documents.c.shop_name)).mappings():
                shops.setdefault(row["shop_id"], row["shop_name"])
            history = [
                dict(r)
                for r in db.execute(
                    select(versions).where(versions.c.month == month).order_by(versions.c.created.desc())
                ).mappings()
            ]
        return dict(items=items, shops=[dict(id=k, name=v) for k, v in shops.items()], history=history)

    @app.put("/api/profit-tax/{shop_id}/{month}", dependencies=[Depends(admin)])
    def save(shop_id: int, month: str, body: dict = Body(...)):
        try:
            month_dates(month)
            if shop_id < 0 or type(body.get("revision")) is not int:
                raise ValueError("配置版本或店铺无效")
            config = clean(body.get("config"), shop_id == 0)
            with engine.begin() as db:
                where = (policies.c.shop_id == shop_id, policies.c.month == month)
                old = db.execute(select(policies).where(*where).with_for_update()).mappings().first()
                revision = old["revision"] if old else 0
                if revision != body["revision"]:
                    raise HTTPException(409, "配置已变化，请刷新后重试")
                previous = resolve(db, shop_id, month)
                vid = uuid.uuid4().hex
                db.execute(
                    versions.insert().values(id=vid, shop_id=shop_id, month=month, created=time.time(), config=config)
                )
                values = dict(version_id=vid, revision=revision + 1)
                if old:
                    db.execute(policies.update().where(*where).values(**values))
                else:
                    db.execute(policies.insert().values(shop_id=shop_id, month=month, **values))
                # Existing documents must be pinned before changing a default, even if never viewed.
                start, end = month_dates(month)
                docs = list(
                    db.execute(
                        select(documents)
                        .where(
                            documents.c.start == start,
                            documents.c.end == end,
                            *([documents.c.shop_id == shop_id] if shop_id else []),
                        )
                        .order_by(documents.c.id)
                        .with_for_update()
                    ).mappings()
                )
                affected = 0
                for doc in docs:
                    bound = db.execute(select(reports).where(reports.c.job_id == doc["id"])).mappings().first()
                    applicable = (
                        shop_id != 0
                        or not db.execute(
                            select(policies.c.version_id).where(
                                policies.c.shop_id == doc["shop_id"], policies.c.month == month
                            )
                        ).scalar()
                    )
                    if body.get("apply_existing") is True and applicable:
                        value = dict(version_id=vid, signature="", summary={})
                        affected += 1
                    elif bound:
                        continue
                    else:
                        prior = previous if applicable else resolve(db, doc["shop_id"], month)
                        value = dict(version_id=prior, signature="", summary={})
                    if bound:
                        db.execute(reports.update().where(reports.c.job_id == doc["id"]).values(**value))
                    else:
                        db.execute(reports.insert().values(job_id=doc["id"], **value))
            sync(engine, [d["id"] for d in docs])
            return dict(id=vid, revision=revision + 1, affected=affected)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        except IntegrityError:
            raise HTTPException(409, "配置被同时修改，请刷新")
