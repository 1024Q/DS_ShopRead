import asyncio, json
from fastapi import APIRouter, Depends, HTTPException, Query
from . import operations_insights as insights


def router(service, authorize, maintenance):
    api = APIRouter(dependencies=[Depends(authorize)])

    @api.get("/api/system/health")
    async def health():
        return await asyncio.to_thread(insights.health, service)

    @api.get("/api/home/history")
    async def history(
        days: int = Query(7, ge=7, le=30),
        platform: str = "",
        shop_id: int | None = None,
        comparison: str = Query("comparable", pattern="^(comparable|strict)$"),
    ):
        return await asyncio.to_thread(insights.history, service.store, days, platform, shop_id, comparison)

    @api.get("/api/attention/history")
    async def history_changes(
        before: int = Query(0, ge=0),
        change: str = Query("", pattern="^(|new|changed|resolved)$"),
        shop_id: str | None = Query(None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
        platform: str = "",
        kind: str = "",
        keyword: str = Query("", max_length=100),
        page: int = Query(1, ge=1),
        sort_by: str = "",
        sort_direction: str = Query("desc", pattern="^(asc|desc)$"),
    ):
        return await asyncio.to_thread(
            insights.notification_history,
            service.store,
            before,
            change,
            shop_id,
            platform,
            kind,
            keyword,
            page,
            sort_by,
            sort_direction,
        )

    @api.get("/api/attention/changes")
    async def changes(after: int | None = Query(None, ge=0)):
        return await asyncio.to_thread(insights.notifications, service.store, after)

    @api.post("/api/system/priority")
    async def priority(shop_id: int, minutes: int = Query(15, ge=0, le=15)):
        raise HTTPException(410, "采集优先级已取消，任务按排队期限自动排序")

    @api.post("/api/system/retry-scope")
    async def retry(shop_id: int, cache_key: str):
        # Resolve exact saved options server-side. Never silently widen dates or filters.
        from .collection_plan import obsolete_realtime

        shop = next((s for s in service.store.list() if s["id"] == shop_id), None)
        with service.store.connect() as db:
            row = db.execute(
                "SELECT * FROM collection_tasks WHERE shop_id=? AND cache_key=?", (shop_id, cache_key)
            ).fetchone()
        if not shop or not row:
            raise HTTPException(404, "采集范围已不存在")
        item = service.session_getter(shop_id)
        if item.state != "logged_in":
            raise HTTPException(409, "请先启动店铺并完成登录验证")
        if (shop_id, cache_key) in service.jobs:
            return {"skipped": True, "reason": "已在队列中"}
        if not row["last_error"]:
            return {"skipped": True, "reason": "已恢复成功，跳过重复采集"}
        options = json.loads(row["options"])
        if obsolete_realtime(row["kind"], options) or service.key(row["kind"], options, shop) != cache_key:
            raise HTTPException(409, "旧范围已失效，请选择指定日期采集")
        await service.enqueue_async(shop, item, row["kind"], options, force=True)
        queued = (shop_id, cache_key) in service.jobs
        if not queued:
            raise HTTPException(409, "店铺正在远程控制或不可采集，请稍后再试")
        return {"queued": True}

    @api.post("/api/system/backup-verify")
    async def verify(name: str):
        try:
            return await maintenance.verify(name)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        except Exception:
            raise HTTPException(500, "备份校验失败，请检查文件及密钥") from None

    return api
