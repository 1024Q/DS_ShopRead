from .filter_scope import matches
from .shop_identity import identity

"""Read-only cross-module workspace and explicit tax review annotations."""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from .business import options_for
from .business_reader import BEIJING
from . import goods_data, tax_review
from .platform_support import supports

TODO_KINDS = ("aftersales", "evidence", "tickets", "intercepts", "packages", "invoices")
PRODUCT_KINDS = ("products", "goodsdata", "reviews", "aftersales", "quality", "navigator")


def product_snapshot(store, shop_id, goods_id):
    shop = next((s for s in store.list() if s["id"] == shop_id), None)
    if not shop:
        raise ValueError("店铺不存在")
    with store.connect() as db:
        caches = db.execute(
            "SELECT cache_key,payload,updated FROM (SELECT shop_id,cache_key,payload,updated FROM business_cache UNION ALL SELECT shop_id,cache_key,payload,updated FROM daily_archive) WHERE shop_id=? ORDER BY updated DESC",
            (shop_id,),
        ).fetchall()
    sections = {}
    # Latest query per module, never combine overlapping date ranges or shops.
    for cache in caches:
        kind = cache["cache_key"].split(":")[0]
        if kind not in PRODUCT_KINDS or kind in sections:
            continue
        payload = json.loads(cache["payload"])
        from .focus_records import current_rows

        payload = {
            **payload,
            "rows": current_rows(store, shop_id, kind, payload, cache["updated"], shop.get("platform", "pdd")),
        }
        rows = [
            {**r, **identity(shop), "shop_id": shop_id, "shop_name": shop["name"]}
            for r in payload.get("rows", [])
            if str(r.get("goods_id") or "") == goods_id
        ]
        sections[kind] = {
            "rows": rows,
            "updated_at": datetime.fromtimestamp(cache["updated"], BEIJING).isoformat(),
            "complete": payload.get("complete", False),
            "scope": payload.get("_sync", {}).get("scope"),
            "source_time": payload.get("metrics", {}).get("source_time"),
        }
    return {
        **identity(shop),
        "shop_id": shop_id,
        "shop_name": shop["name"],
        "goods_id": goods_id,
        "sections": {
            kind: sections.get(kind, {"rows": [], "complete": False, "updated_at": None}) for kind in PRODUCT_KINDS
        },
    }


class ReviewInput(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    note: str = Field(default="", max_length=2000)
    confirmed: bool = False
    overwrite: bool = False
    action: str = Field(default="", pattern=r"^(confirm_entity|overwrite_amounts)?$")
    baseline_revision: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


def create_router(service, store, authorize):
    router = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @router.post("/invoices/refresh-pending", status_code=202)
    async def refresh_pending_invoices(shop_id: int | None = None):
        options = {**options_for("invoices", status="待开票"), "_invoice_details_refresh": True}
        return service.request_sync("invoices", options, shop_id, True, platform="pdd")

    @router.post("/todos/sync", status_code=202)
    async def sync_todos(
        shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
        force: bool = False,
    ):
        from .attention import scopes

        tasks = []
        for shop in store.list():
            if not matches(shop_id, shop["id"]):
                continue
            tasks.extend(service.request_sync(kind, options, shop["id"], force) for kind, options in scopes(shop))
        return {"tasks": tasks}

    @router.post("/product/{shop_id}/{goods_id}/sync", status_code=202)
    async def sync_product(shop_id: int, goods_id: str, force: bool = False):
        if not goods_id.isdigit() or len(goods_id) > 40:
            raise HTTPException(400, "商品 ID 无效")
        try:
            shop = next((s for s in store.list() if s["id"] == shop_id), None)
            if not shop:
                raise ValueError("店铺不存在")
            return {
                "tasks": [
                    service.request_sync(
                        kind,
                        goods_data.options_for("realtime") if kind == "goodsdata" else options_for(kind),
                        shop_id,
                        force,
                    )
                    for kind in PRODUCT_KINDS
                    if supports(shop.get("platform", "pdd"), kind)
                ]
            }
        except ValueError as error:
            raise HTTPException(400, str(error))

    @router.get("/todos")
    def todos(
        shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
        refresh: bool = False,
        force: bool = False,
        platform: str = "",
        kind: str = "",
        keyword: str = Query(default="", max_length=200),
        page: int = Query(default=1, ge=1),
    ):
        from .attention import read

        return {**read(service, platform, shop_id, kind, page, keyword=keyword), "timezone": "Asia/Shanghai"}

    @router.get("/product/{shop_id}/{goods_id}")
    async def product(shop_id: int, goods_id: str, refresh: bool = False):
        if not goods_id.isdigit() or len(goods_id) > 40:
            raise HTTPException(400, "商品 ID 无效")
        try:
            result = product_snapshot(store, shop_id, goods_id)
            result["sources"] = []
            # Poll only jobs started here; opening details does not enqueue all modules.
            for kind in PRODUCT_KINDS:
                options = goods_data.options_for("realtime") if kind == "goodsdata" else options_for(kind)
                record = next(s for s in store.list() if s["id"] == shop_id)
                key = (shop_id, service.key(kind, options, record))
                if key in service.jobs:
                    result["sources"].extend(
                        {**s, "kind": kind}
                        for s in service.data(kind, options, shop_id, refresh, refresh, foreground=False)["sources"]
                    )
            return result
        except ValueError as error:
            raise HTTPException(404, str(error))

    @router.get("/tax-review/{shop_id}/{period}")
    async def read_review(shop_id: int, period: str):
        try:
            return tax_review.read(store, shop_id, period)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @router.post("/tax-review/{shop_id}/{period}")
    async def save_review(shop_id: int, period: str, body: ReviewInput):
        try:
            return tax_review.save(
                store,
                shop_id,
                period,
                body.revision,
                body.note,
                body.confirmed,
                body.overwrite,
                body.action,
                body.baseline_revision,
            )
        except RuntimeError as error:
            raise HTTPException(409, str(error))
        except ValueError as error:
            raise HTTPException(404, str(error))

    return router
