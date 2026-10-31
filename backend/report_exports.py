"""Node-owned manual exports. Submission intent is durable before the platform click."""

import logging
import asyncio, base64, hashlib, time
from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from .report_export_store import Ledger, ACTIVE, FIELDS, filename, pack_download
from . import report_export_adapter as adapter, update_gate


def enabled(store):
    from .report_export_settings import read

    return read(store.directory)["enabled"]


class Worker:
    def __init__(self, store, session):
        self.store = store
        self.session = session
        self.ledger = Ledger(store.directory / "report-exports")
        self.tasks = {}

    async def delete(self, jid):
        self.ledger.path(jid)
        task = self.tasks.get(jid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(self.ledger.remove, jid)

    def recover_ui_jobs(self):
        revision = "20261015-export-notification-2"
        count = 0
        for job in self.ledger.all():
            if job["state"] != "exception" or job.get("submitted_at") or job.get("ui_recovery_revision") == revision:
                continue
            field_error = "字段" in job.get("stage", "") and any(
                word in job.get("message", "")
                for word in ("核验失败", "未就绪", "字段不完整", "字段无法操作", "页面等待超时")
            )
            notice_error = "导出步骤未就绪：重置" in job.get("message", "")
            if not (field_error or notice_error):
                continue
            self.ledger.update(
                job["id"],
                state="queued",
                attempts=0,
                retry_at=0,
                ui_recovery_revision=revision,
                message="已应用页面等待修复，自动恢复未提交的导出任务",
            )
            count += 1
        return count

    @asynccontextmanager
    async def page(self, item, jid):
        async with adapter.OrdersPage(item) as page:
            from .operation_log import step

            def progress(message):
                self.ledger.update(jid, message=message, stage=message)
                step(self.store, item.shop, "报表导出", message)

            page.export_progress = progress
            try:
                if (
                    hasattr(item, "quiet_window")
                    and not getattr(item, "remote_control_active", False)
                    and getattr(item, "manual_visibility", None) is not True
                ):
                    await item.quiet_window(False)
                yield page
            except Exception:
                # Capture the actual failed export tab before its context closes.
                try:
                    capture = await asyncio.wait_for(
                        page.call("Page.captureScreenshot", {"format": "jpeg", "quality": 60}), 5
                    )
                    data = base64.b64decode(capture["data"], validate=True)
                    if len(data) > 5 * 1024 * 1024:
                        raise ValueError("截图过大")
                    (self.ledger.path(jid) / "failure.jpg").write_bytes(data)
                    self.ledger.update(jid, screenshot=True, screenshot_at=time.time())
                except Exception:
                    self.ledger.update(jid, screenshot=False, screenshot_error="浏览器连接已中断，无法截取失败页面")
                raise

    async def process(self, job):
        try:
            await self._process(job)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.ledger.update(
                job["id"],
                state="exception",
                retry_at=0,
                message="导出任务执行异常：" + (str(error) or type(error).__name__)[:240],
            )
        finally:
            current = self.ledger.get(job["id"])
            if current["state"] in ("completed", "exception"):
                from .operation_log import outcome

                outcome(self.store, current)

    async def _process(self, job):
        jid = job["id"]
        ledger = self.ledger
        try:
            item = self.session(job["shop_id"])
        except Exception:
            ledger.update(jid, state="exception", message="店铺不存在或当前不可用，请检查店铺归属后重新创建任务")
            return
        item.last_collection_activity = time.monotonic()
        if item.shop.get("platform", "pdd") != "pdd" or item.shop.get("profile_version", 0) != job["profile_version"]:
            ledger.update(jid, state="exception", message="店铺账号或平台发生变化，请重新创建任务")
            return
        if getattr(item, "sleeping", False):
            from .browser_sleep import wake

            try:
                await wake(item)
            except Exception:
                ledger.update(jid, state="exception", message="休眠店铺唤醒未完成，请检查登录状态后重试")
                return
        if item.state != "logged_in":
            ledger.update(jid, state="exception", message="店铺未登录或需要验证码，请在店铺管理完成验证后重试")
            return
        if getattr(item, "control_until", 0) > time.time() or update_gate.active():
            return
        if job.get("kind") == "promotion":
            await self.promotion(job, item)
            return
        if not job.get("submitted_at"):
            previous = max(
                (r.get("submitted_at") or 0 for r in ledger.all() if r["shop_id"] == job["shop_id"]), default=0
            )
            if time.time() - previous < 305:
                ledger.update(jid, retry_at=previous + 305, message="平台限制同店两次导出间隔至少 5 分钟")
                return
        try:
            async with item.lock:
                if update_gate.active() or getattr(item, "control_until", 0) > time.time():
                    return
                async with self.page(item, jid) as page:
                    if not job.get("submitted_at"):
                        ledger.update(jid, state="querying", message="核对全部订单及自定义时间范围")
                        await adapter.configure(page, job)
                        existing = await adapter.reports(page)
                        await adapter.ready_to_submit(page)
                        job = ledger.update(
                            jid,
                            state="submitting",
                            message="正在提交，断线后只核验结果，不重复生成",
                            submitted_at=time.time(),
                            before_ids=[str(r["id"]) for r in existing],
                        )
                        await adapter.submit(page)
                        ledger.update(
                            jid,
                            state="generating",
                            submission_confirmed=True,
                            message="平台已受理，等待生成报表",
                            retry_at=time.time() + 5,
                        )
                        return
                    await adapter.open_reports(page)
                    rows = await adapter.reports(page)
                    found = (
                        [r for r in rows if str(r.get("id")) == job.get("platform_id")]
                        if job.get("platform_id")
                        else [r for r in rows if adapter.match(r, job)]
                    )
                    if len(found) > 1:
                        raise ValueError("存在多个符合条件的报表，请人工核对，已停止自动下载")
                    if not found:
                        detail = adapter.match_diagnostics(rows, job)
                        ledger.update(jid, match_diagnostics=detail)
                        if time.time() - job["submitted_at"] > 600:
                            reason = (
                                "；".join(dict.fromkeys(reason for row in detail for reason in row["reasons"]))
                                or "平台列表暂未出现本次报表"
                            )
                            raise ValueError(
                                (
                                    "平台已受理，但未找到符合条件的报表："
                                    if job.get("submission_confirmed")
                                    else "提交结果待核验："
                                )
                                + reason
                                + "；保留原提交，请勿重复生成"
                            )
                        reason = "；".join(dict.fromkeys(reason for row in detail for reason in row["reasons"]))
                        ledger.update(
                            jid,
                            state="generating",
                            message="正在核验平台提交结果" + (" · " + reason if reason else "，等待平台列表更新"),
                            retry_at=time.time() + 15,
                        )
                        return
                    report = found[0]
                    pid = str(report["id"])
                    if report.get("status") == 0:
                        if time.time() - job["submitted_at"] > 3600:
                            raise ValueError("平台生成超过 1 小时，请检查生成报表页面；重试仅核验原报表")
                        ledger.update(
                            jid,
                            state="generating",
                            platform_id=pid,
                            message="平台生成中",
                            progress=report.get("progressRate"),
                            retry_at=time.time() + 15,
                        )
                        return
                    if report.get("status") != 1:
                        raise ValueError("平台报表生成失败：" + str(report.get("statusStr") or "未知状态"))
                    url = await adapter.post(page, adapter.URL, {"jobId": report["id"]})
                    if not isinstance(url, str):
                        raise ValueError("平台下载地址格式变化，请检查适配")
                    ledger.update(jid, state="downloading", platform_id=pid, message="正在下载报表")
            # Release the browser while transferring and unpacking files.
            folder = ledger.path(jid)
            source = folder / "original.download"
            target = folder / "report.zip"
            await adapter.download(url, source)
            ledger.update(jid, state="unpacking", message="正在解压并按店铺及时间范围命名")
            await asyncio.to_thread(pack_download, source, target, filename(job["shop_name"], job["start"], job["end"]))

            def checksum():
                with target.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()

            digest = await asyncio.to_thread(checksum)
            ledger.update(
                jid,
                state="completed",
                message="导出完成，等待中心接收",
                size=target.stat().st_size,
                sha256=digest,
                retry_at=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            current = ledger.get(jid)
            attempts = current.get("attempts", 0) + 1
            if isinstance(error, adapter.SubmissionRejected):
                ledger.update(
                    jid,
                    state="exception",
                    submitted_at=None,
                    submission_confirmed=False,
                    submission_rejected=True,
                    attempts=attempts,
                    retry_at=0,
                    message="平台拒绝生成：" + str(error)[:250],
                )
                return
            # Before submission: bounded retries. After intent: only reconcile.
            retry = attempts < 3 and (isinstance(error, adapter.PageReadError) or not isinstance(error, ValueError))
            ledger.update(
                jid,
                state=("generating" if current.get("submitted_at") else "queued") if retry else "exception",
                attempts=attempts,
                retry_at=time.time() + 60 if retry else 0,
                message=str(error)[:300] or type(error).__name__,
            )

    async def promotion(self, job, item):
        from .promotion_export import collect

        jid = job["id"]
        folder = self.ledger.path(jid)
        async with item.lock:
            if update_gate.active():
                return
            async with self.page(item, jid) as page:
                self.ledger.update(jid, state="querying", message="打开多多推广月账单")
                source = await collect(page, folder)
        self.ledger.update(jid, state="unpacking", message="整理月账单，等待中心导入")
        target = folder / "report.zip"
        await asyncio.to_thread(
            pack_download, source, target, filename(job["shop_name"], job["start"], job["end"]) + "_推广流水"
        )
        digest = await asyncio.to_thread(lambda: hashlib.sha256(target.read_bytes()).hexdigest())
        self.ledger.update(
            jid,
            state="completed",
            size=target.stat().st_size,
            sha256=digest,
            retry_at=0,
            message="推广月账单下载完成，等待中心接收",
        )

    async def run(self):
        self.recover_ui_jobs()
        while True:
            try:
                if enabled(self.store) and not update_gate.active():
                    from .report_export_settings import read

                    days = read(self.store.directory)["retention_days"]
                    await asyncio.to_thread(self.ledger.cleanup, max(1, min(30, int(days))))
                    due = [j for j in self.ledger.all() if j["state"] in ACTIVE and j.get("retry_at", 0) <= time.time()]
                    for job in reversed(due):
                        if not (self.ledger.path(job["id"]) / "state.json").exists():
                            continue
                        task = asyncio.create_task(self.process(job))
                        self.tasks[job["id"]] = task
                        try:
                            await task
                        except asyncio.CancelledError:
                            if asyncio.current_task().cancelling():
                                raise
                        finally:
                            self.tasks.pop(job["id"], None)
                        if update_gate.active():
                            break
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).warning("报表导出队列本轮处理失败，3 秒后重试", exc_info=True)
            await asyncio.sleep(3)


def router(worker, authorize):
    api = APIRouter(prefix="/api/report-exports", dependencies=[Depends(authorize)])

    @api.post("")
    def create(body: dict):
        shop = next(
            (s for s in worker.store.list() if s["id"] == body.get("shop_id") and s.get("platform", "pdd") == "pdd"),
            None,
        )
        if not shop:
            raise HTTPException(400, "该节点没有选中的拼多多店铺")
        try:
            return worker.ledger.create(
                shop, body["start"], body["end"], body["id"], body.get("fields"), body.get("kind", "orders")
            )
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))

    @api.put("/settings")
    def settings(body: dict):
        days = body.get("retention_days")
        if type(days) != int or not 1 <= days <= 30:
            raise HTTPException(400, "保留天数应为 1 至 30")
        path = worker.store.directory / "report-export.json"
        from .report_export_settings import read
        from .collector_update import write_json

        cfg = read(worker.store.directory)
        cfg["retention_days"] = days
        write_json(path, cfg)
        return {"retention_days": days}

    @api.get("/settings")
    def capabilities():
        return {"fields": FIELDS, "kinds": ["orders", "promotion", "promotion_all"]}

    @api.delete("/{jid}")
    async def delete(jid: str):
        try:
            await worker.delete(jid)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        return {"ok": True, "id": jid}

    @api.get("/{jid}")
    def get(jid: str):
        try:
            return {**worker.ledger.get(jid), "file_bytes": worker.ledger.file_bytes(jid)}
        except OSError, ValueError:
            raise HTTPException(404, "导出任务不存在")

    @api.post("/{jid}/retry")
    def retry(jid: str, body: dict | None = None):
        job = get(jid)
        if job["state"] != "exception":
            raise HTTPException(409, "只有异常任务可重试")
        if body and body.get("restart_submission") is True:
            return worker.ledger.update(
                jid,
                state="queued",
                previous_submission={"submitted_at": job.get("submitted_at"), "platform_id": job.get("platform_id")},
                submitted_at=None,
                submission_confirmed=False,
                platform_id=None,
                before_ids=[],
                attempts=0,
                retry_at=0,
                message="已人工确认平台未生成，等待重新查询及提交",
            )
        return worker.ledger.update(
            jid,
            state="generating" if job.get("submitted_at") else "queued",
            attempts=0,
            retry_at=0,
            message="等待核验原报表" if job.get("submitted_at") else "等待重新执行",
        )

    @api.get("/{jid}/screenshot")
    def screenshot(jid: str):
        from fastapi.responses import FileResponse

        get(jid)
        path = worker.ledger.path(jid) / "failure.jpg"
        if not path.is_file():
            raise HTTPException(404, "此任务没有可用的失败截图")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @api.get("/{jid}/chunk")
    def chunk(jid: str, offset: int = Query(0, ge=0)):
        job = get(jid)
        if job["state"] != "completed":
            raise HTTPException(409, "报表尚未完成")
        with (worker.ledger.path(jid) / "report.zip").open("rb") as f:
            f.seek(offset)
            data = f.read(1024 * 1024)
        return Response(data, media_type="application/octet-stream")

    return api
