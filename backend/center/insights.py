import math
from datetime import datetime
from fastapi import Depends, Query
from sqlalchemy import select, or_
from .database import snapshots
from ..attention import TITLES
from ..filter_scope import matches
from ..shop_identity import identity
from ..business_reader import BEIJING
from ..operations import decorate


def install(app, database, service, admin):
    @app.get("/api/attention", dependencies=[Depends(admin)])
    @app.get("/api/todos", dependencies=[Depends(admin)])
    def attention(
        platform: str = "",
        shop_id: str = "",
        kind: str = "",
        page: int = Query(default=1, ge=1),
        summary: bool = False,
        keyword: str = "",
        sort_by: str = "",
        sort_direction: str = "asc",
    ):
        rows = []
        sources = []
        from .freshness import configurations, describe

        policies = configurations(database)
        for shop in service.store.list():
            if (
                not matches(platform, shop.get("platform"))
                or not matches(shop_id, shop["id"])
                or shop.get("platform") != "pdd"
            ):
                continue
            with database.engine.connect() as db:
                records = {
                    r["scope"].split(":")[1]: dict(r)
                    for r in db.execute(
                        select(snapshots).where(
                            snapshots.c.node == shop["node_id"],
                            snapshots.c.shop == str(shop["local_id"]),
                            snapshots.c.scope.like("attention:%"),
                        )
                    ).mappings()
                }
            for module in TITLES:
                if kind and module != kind:
                    continue
                saved = records.get(module)
                payload = saved["payload"] if saved else {}
                freshness = describe(
                    module,
                    {**payload.get("_sync", {}).get("scope", {}), "_attention": True},
                    payload,
                    saved["updated"] if saved else 0,
                    policies.get(shop["node_id"]),
                    shop.get("platform", "pdd"),
                )
                source = {
                    **identity(shop),
                    "shop_id": shop["id"],
                    "shop_name": shop.get("name", ""),
                    "operators": shop.get("operators", []),
                    "kind": module,
                    "updated_at": saved["updated"] if saved else None,
                    **freshness,
                    "stale": not saved or freshness["overdue"] or not shop["collector_online"],
                    "error": "" if shop["collector_online"] else "节点离线",
                    "syncing": False,
                }
                source["center_received_at"] = (
                    datetime.fromtimestamp(payload["_center_received_at"], BEIJING).isoformat()
                    if payload.get("_center_received_at")
                    else None
                )
                sources.append(source)
                for original in saved["payload"].get("rows", []) if saved else []:
                    row = {**decorate(module, original, "pdd"), **source, "module_name": TITLES[module]}
                    if row.get("priority") and keyword.casefold() in json_text(row).casefold():
                        rows.append(row)
        if sort_by:
            rows.sort(key=lambda r: str(r.get(sort_by) or ""), reverse=sort_direction == "desc")
        else:
            rows.sort(key=lambda r: str(r.get("deadline") or "9999"))
        from ..priority_rules import immediate

        rows.sort(key=lambda row: not immediate(row.get("kind", row.get("module", "")), row, row.get("platform", "pdd")))
        total = len(rows)
        pages = max(1, math.ceil(total / 20))
        page = min(page, pages)
        return {
            "rows": [] if summary else rows[(page - 1) * 20 : page * 20],
            "total": total,
            "page": page,
            "page_count": pages,
            "sources": [] if summary else sources,
            "interval_seconds": 60,
            "pending": sum(not s["updated_at"] for s in sources),
            "stale": sum(s["stale"] for s in sources),
            "timezone": "Asia/Shanghai",
        }

    @app.get("/api/product/{shop_id}/{goods_id}", dependencies=[Depends(admin)])
    def product(shop_id: int, goods_id: str):
        from fastapi import HTTPException
        from ..workspace_api import PRODUCT_KINDS

        shop = next((s for s in service.store.list() if s["id"] == shop_id), None)
        if not shop:
            raise HTTPException(404, "店铺不存在")
        sections = {}
        with database.engine.connect() as db:
            for kind in PRODUCT_KINDS:
                # Only the newest snapshot of each product kind is read, never the shop's whole history.
                pattern = kind.replace("_", r"\_")
                record = (
                    db.execute(
                        select(snapshots)
                        .where(
                            snapshots.c.node == shop["node_id"],
                            snapshots.c.shop == str(shop["local_id"]),
                            or_(
                                snapshots.c.scope == kind,
                                snapshots.c.scope.like(pattern + ":%", escape="\\"),
                                snapshots.c.scope.like("archive:" + pattern + ":%", escape="\\"),
                                snapshots.c.scope == "archive:" + kind,
                            ),
                        )
                        .order_by(snapshots.c.updated.desc())
                        .limit(1)
                    )
                    .mappings()
                    .first()
                )
                if not record:
                    continue
                from .snapshot_freshness import overlay

                record = overlay(database, [record])[0]
                payload = record["payload"]
                rows = [
                    {**r, **identity(shop), "shop_id": shop_id, "shop_name": shop.get("name", "")}
                    for r in payload.get("rows", [])
                    if str(r.get("goods_id") or "") == goods_id
                ]
                sections[kind] = {
                    "rows": rows,
                    "updated_at": datetime.fromtimestamp(record["updated"], BEIJING).isoformat(),
                    "complete": payload.get("complete", False),
                    "scope": payload.get("_sync", {}).get("scope"),
                    "source_time": payload.get("metrics", {}).get("source_time"),
                }
        return {
            **identity(shop),
            "shop_id": shop_id,
            "shop_name": shop.get("name", ""),
            "goods_id": goods_id,
            "sources": [],
            "sections": {
                kind: sections.get(kind, {"rows": [], "complete": False, "updated_at": None}) for kind in PRODUCT_KINDS
            },
        }

    @app.get("/api/fleet", dependencies=[Depends(admin)])
    def fleet(platform: str = "", operator: str = ""):
        from ..business import options_for

        data = service.data("overview", options_for("overview"), platform=platform)
        shops = {s["id"]: s for s in service.store.list()}
        registry = database.registry()
        nodeids = {n["node"]: n["id"] for n in registry}
        rows = []
        accounts = set()
        totals = {}
        coverage = {}
        for metric in sorted(data["metrics"], key=lambda r: r.get("updated_at") or "", reverse=True):
            shop = shops[metric["shop_id"]]
            if operator and not any(operator.casefold() in n.casefold() for n in shop.get("operators", [])):
                continue
            account = (shop.get("platform"), shop.get("username") or (shop["node_id"], shop["id"]))
            duplicate = account in accounts
            accounts.add(account)
            row = {
                **shop,
                "shop_id": shop["id"],
                "node_id": nodeids[shop["node_id"]],
                "metrics": metric,
                "online": shop["collector_online"],
                "duplicate_account": duplicate,
                "supported": True,
                "current_day": not metric.get("day_pending"),
                "updated_at": metric.get("updated_at"),
                "stale": metric.get("stale"),
                "attention": None,
            }
            rows.append(row)
            if duplicate or not row["online"] or not row["current_day"]:
                continue
            for key, value in metric.items():
                if key in ("sales", "orders", "ad_spend", "pending_aftersales", "visitors", "views") and type(
                    value
                ) in (int, float):
                    totals[key] = totals.get(key, 0) + value
                    coverage[key] = coverage.get(key, 0) + 1
        return {
            "rows": rows,
            "totals": totals,
            "coverage": coverage,
            "unique_shops": len(accounts),
            "machines": [
                {
                    "node_id": n["id"],
                    "name": n["name"],
                    "online": any(s["node_id"] == n["node"] and s["collector_online"] for s in shops.values()),
                    "updated_at": n["seen"],
                }
                for n in registry
            ],
        }


def json_text(value):
    import json

    return json.dumps(value, ensure_ascii=False)
