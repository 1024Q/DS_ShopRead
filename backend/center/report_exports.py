"""Fleet export dispatch, PostgreSQL job metadata and durable center downloads."""

import asyncio, hashlib, json, logging, time
from pathlib import Path
from sqlalchemy import select, delete
from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse
from .database import settings
from .tunnel import content
from ..report_export_store import Ledger, ACTIVE, FIELDS, dates, filename, MAX_BYTES
from . import report_view

ROOT = Path(__file__).resolve().parents[2] / ".data/center"


class NodeReportError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CenterLedger(Ledger):
    def __init__(self, database, root):
        super().__init__(root)
        self.database = database

    def get(self, jid):
        self.path(jid)
        with self.database.engine.connect() as db:
            row = db.execute(select(settings.c.value).where(settings.c.key == "report-export:" + jid)).scalar()
        if row is None:
            raise FileNotFoundError(jid)
        return row

    def all(self):
        with self.database.engine.connect() as db:
            rows = db.execute(select(settings.c.value).where(settings.c.key.like("report-export:%"))).scalars().all()
        return sorted(rows, key=lambda r: r["created"], reverse=True)

    def save(self, row):
        key = "report-export:" + row["id"]
        with self.database.engine.begin() as db:
            found = db.execute(select(settings.c.key).where(settings.c.key == key)).first()
            if found:
                db.execute(settings.update().where(settings.c.key == key).values(value=row))
            else:
                db.execute(settings.insert().values(key=key, value=row))
        return row

    def cleanup(self, days=7):
        for row in self.all():
            if (
                row.get("import_state") != "ready"
                and row["state"] not in ACTIVE
                and row["updated"] < time.time() - days * 86400
            ):
                self.remove(row["id"])

    def remove(self, jid):
        super().remove(jid)
        with self.database.engine.begin() as db:
            from .promotion_ledger import bills

            db.execute(delete(bills).where(bills.c.job_id == jid))
            from .reconciliation import bindings, results

            db.execute(delete(results).where(results.c.job_id == jid))
            db.execute(delete(bindings).where(bindings.c.job_id == jid))
            from .profit_tax import reports as tax_reports, allocations

            db.execute(delete(allocations).where(allocations.c.job_id == jid))
            db.execute(delete(tax_reports).where(tax_reports.c.job_id == jid))
            db.execute(delete(report_view.rows).where(report_view.rows.c.job_id == jid))
            db.execute(delete(report_view.documents).where(report_view.documents.c.id == jid))
            db.execute(delete(settings).where(settings.c.key == "report-export:" + jid))


def export_config():
    from ..report_export_settings import read

    return read(ROOT)


def install(app, database, hub, admin):
    report_view.meta.create_all(database.engine)
    from .cost_tables import meta as cost_meta

    cost_meta.create_all(database.engine)
    from .reconciliation import meta as accounting_meta

    accounting_meta.create_all(database.engine)
    from .promotion_ledger import install as install_promotions

    install_promotions(app, database, admin)
    from .profit import install as install_profit

    install_profit(app, database, admin)
    from .table_tools import install as install_table_tools

    install_table_tools(app, database, admin)
    ledger = CenterLedger(database, ROOT / "report-exports")
    locks = {}
    health = dict(running=False, consecutive_failures=0, last_success=0, last_error="", retry_at=0)

    def job_lock(jid):
        return locks.setdefault(jid, asyncio.Lock())

    def deletion_requests():
        with database.engine.connect() as db:
            return list(
                db.execute(select(settings.c.value).where(settings.c.key.like("report-export-delete:%"))).scalars()
            )

    def deletion_record(jid, value):
        key = "report-export-delete:" + jid
        with database.engine.begin() as db:
            db.execute(delete(settings).where(settings.c.key == key))
            if value is not None:
                db.execute(settings.insert().values(key=key, value=value))

    def configuration():
        cfg = export_config()
        return cfg

    def existing(jid):
        try:
            job = ledger.get(jid)
        except ValueError, OSError:
            raise HTTPException(404, "导出任务不存在")
        return job

    async def node_call(node, method, path, body=None, query=""):
        result = await hub.request(node, method, path, query, json.dumps(body).encode() if body is not None else b"")
        if result["status"] != 200:
            try:
                message = json.loads(content(result)).get("detail", "节点暂不可用")
            except ValueError:
                message = "节点暂不可用"
            raise NodeReportError(result["status"], str(message))
        return content(result)

    async def finish_delete(record):
        jid = record["id"]
        async with job_lock(jid):
            await asyncio.to_thread(ledger.remove, jid)
            if record.get("remote_job_id"):
                if record.get("node") not in hub.peers:
                    return False
                try:
                    await asyncio.wait_for(
                        node_call(record["node"], "DELETE", "report-exports/" + record["remote_job_id"]), 8
                    )
                except ValueError, HTTPException, TimeoutError:
                    return False
            deletion_record(jid, None)
            return True

    @app.get("/api/report-exports", dependencies=[Depends(admin)])
    def list_jobs(kind: str = "orders"):
        if kind not in ("orders", "promotion"):
            raise HTTPException(400, "报表类型无效")
        cfg = configuration()
        nodes = {n["node"]: n["name"] for n in database.registry()}
        shops = [
            {
                "id": s["id"],
                "name": s.get("name") or s.get("username"),
                "node_id": s["node_id"],
                "node_name": nodes.get(s["node_id"], "未知节点"),
                "online": s["node_id"] in hub.peers,
            }
            for s in database.shops()
            if s.get("platform", "pdd") == "pdd"
        ]
        deleted = {r["id"] for r in deletion_requests()}
        jobs = [
            {
                **job,
                "node_name": nodes.get(job.get("node"), "未知节点"),
                "center_file_bytes": ledger.file_bytes(job["id"]),
            }
            for job in ledger.all()
            if job["state"] != "deleting" and job["id"] not in deleted and job.get("kind", "orders") == kind
        ][:200]
        return {
            "shops": shops,
            "jobs": jobs,
            "fields": FIELDS,
            "retention_days": cfg.get("retention_days", 7),
            "online": any(s["online"] for s in shops),
            "online_nodes": sum(n in hub.peers for n in nodes),
            "total_nodes": len(nodes),
            "refreshed_at": time.time(),
            "worker": dict(health),
        }

    @app.post("/api/report-exports", dependencies=[Depends(admin)])
    def create(body: dict):
        configuration()
        try:
            kind = body.get("kind", "orders")
            if kind not in ("orders", "promotion"):
                raise ValueError("报表类型无效")
            start, end = (
                ("all", "all")
                if kind == "promotion"
                else dates(*report_view.month_dates(body["month"]))
                if body.get("month")
                else dates(body["start"], body["end"])
            )
            ids = body["shop_ids"]
            if (
                body.get("confirmed") is not True
                or not isinstance(ids, list)
                or not 1 <= len(ids) <= 50
                or any(type(i) != int for i in ids)
            ):
                raise ValueError("请选择 1 至 50 家店铺并确认条件")
            shops = {s["id"]: s for s in database.shops()}
            if any(i not in shops or shops[i].get("platform", "pdd") != "pdd" for i in ids):
                raise ValueError("请选择有效的拼多多店铺")
            if any(shops[i]["node_id"] not in hub.peers for i in ids):
                raise ValueError("所选店铺存在离线节点，请连接后再创建导出任务")
            result = []
            with ledger.lock:
                for sid in dict.fromkeys(ids):
                    saved = next(
                        (
                            j
                            for j in ledger.all()
                            if j["shop_id"] == sid
                            and j["start"] == start
                            and j["end"] == end
                            and j.get("kind", "orders") == kind
                            and (kind == "promotion" or j.get("fields") == FIELDS)
                            and j["state"] == "completed"
                            and (ledger.path(j["id"]) / "report.zip").exists()
                        ),
                        None,
                    )
                    if saved and not body.get("regenerate"):
                        result.append(saved)
                        continue
                    node = shops[sid]["node_id"]
                    row = ledger.create(shops[sid], start, end, kind=kind)
                    result.append(ledger.update(row["id"], node=node, local_shop_id=shops[sid]["local_id"]))
            return {"jobs": result}
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/report-exports/view", dependencies=[Depends(admin)])
    def view(
        month: str = "",
        shop_id: int = 0,
        keyword: str = Query("", max_length=100),
        status: str = Query("", max_length=100),
        sort_by: int = -1,
        direction: str = "asc",
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
        metadata_only: bool = False,
    ):
        configuration()
        deleted = {r["id"] for r in deletion_requests()}
        jobs = [
            j["id"]
            for j in ledger.all()
            if j.get("import_state") == "ready"
            and j.get("kind", "orders") == "orders"
            and j["state"] != "deleting"
            and j["id"] not in deleted
        ]
        from .cost_tables import enrich

        try:
            result = enrich(
                database.engine,
                report_view.query(
                    database.engine,
                    jobs,
                    month,
                    shop_id,
                    keyword,
                    status,
                    sort_by,
                    direction,
                    page,
                    page_size,
                    metadata_only,
                ),
            )
            result["accounting_errors"] = {
                j["id"]: j["accounting_error"] for j in ledger.all() if j.get("accounting_error") and j["id"] in jobs
            }
            return result
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/report-exports/unmatched", dependencies=[Depends(admin)])
    def missing_cost(month: str = "", shop_id: int = 0, page: int = Query(1, ge=1)):
        data = view(
            month=month, shop_id=shop_id, keyword="", status="", sort_by=-1, direction="asc", page=1, page_size=1
        )
        from .reconciliation import unmatched

        return unmatched(database.engine, [r["id"] for r in data["reports"]], page)

    @app.post("/api/report-exports/{jid}/recalculate", dependencies=[Depends(admin)])
    def recalculate(jid: str, body: dict):
        existing(jid)
        if body.get("confirmed") is not True:
            raise HTTPException(400, "请确认切换到当前成本版本并重新核算")
        from .reconciliation import snapshot

        try:
            result = snapshot(database.engine, jid, True, body.get("income_mode"))
            ledger.update(jid, accounting_error="")
            return result
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/report-exports/{jid}/import", dependencies=[Depends(admin)])
    def retry_import(jid: str):
        job = existing(jid)
        if job["state"] != "completed":
            raise HTTPException(409, "文件尚未接收完成")
        return ledger.update(
            jid,
            **({"reimport_pending": True} if job.get("import_state") == "ready" else {"import_state": "pending"}),
            import_error="",
        )

    @app.delete("/api/report-exports/{jid}", dependencies=[Depends(admin)])
    async def delete_report(jid: str):
        job = existing(jid)
        # The node uses the center ID even if its create response was lost.
        record = {
            "id": jid,
            "node": job.get("node"),
            "remote_job_id": (job.get("remote_job_id") or jid) if job.get("node") else None,
        }
        deletion_record(jid, record)
        ledger.update(jid, state="deleting")
        done = await finish_delete(record)
        return {"ok": True, "id": jid, "node_cleanup_pending": not done}

    @app.put("/api/report-exports/settings", dependencies=[Depends(admin)])
    async def configure(body: dict):
        cfg = configuration()
        days = body.get("retention_days")
        if type(days) != int or not 1 <= days <= 30:
            raise HTTPException(400, "保留天数应为 1 至 30")
        cfg["retention_days"] = days
        from ..collector_update import write_json

        write_json(ROOT / "report-export.json", cfg)

        async def update_node(node):
            try:
                await node_call(node, "PUT", "report-exports/settings", {"retention_days": days})
                return True
            except ValueError, HTTPException:
                return False

        results = await asyncio.gather(*(update_node(node) for node in hub.peers))
        return {
            "retention_days": days,
            "confirmed_nodes": sum(results),
            "message": "保留时间已保存；任务下发时再次同步到所属节点",
        }

    @app.post("/api/report-exports/{jid}/retry", dependencies=[Depends(admin)])
    async def retry(jid: str, body: dict | None = None):
        job = existing(jid)
        if job["state"] != "exception":
            raise HTTPException(409, "仅可重试异常任务")
        if job.get("remote_job_id"):
            try:
                remote = json.loads(await node_call(job["node"], "GET", f"report-exports/{job['remote_job_id']}"))
                if remote["state"] == "exception":
                    await node_call(
                        job["node"],
                        "POST",
                        f"report-exports/{job['remote_job_id']}/retry",
                        {"restart_submission": bool(body and body.get("restart_submission") is True)},
                    )
            except ValueError as e:
                raise HTTPException(409, str(e))
        return ledger.update(
            jid, state="queued", message="等待恢复原任务", retry_at=0, transfer_retry_at=0, transfer_attempts=0
        )

    @app.get("/api/report-exports/{jid}/screenshot", dependencies=[Depends(admin)])
    def screenshot(jid: str):
        existing(jid)
        path = ledger.path(jid) / "failure.jpg"
        if not path.is_file():
            raise HTTPException(404, "此任务暂无失败截图；旧任务需要再次执行后才能记录截图")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/report-exports/{jid}/download", dependencies=[Depends(admin)])
    def download(jid: str):
        job = existing(jid)
        path = ledger.path(jid) / "report.zip"
        if job["state"] != "completed" or not path.is_file():
            raise HTTPException(409, "中心尚未接收完成")
        return FileResponse(
            path,
            filename=filename(job["shop_name"], job["start"], job["end"]) + ".zip",
            media_type="application/zip",
            headers={"Cache-Control": "no-store"},
        )

    async def synchronize(job, cfg):
        jid = job["id"]
        node = job["node"]
        # Recheck ownership: moving a shop must not dispatch to its former node.
        shop = next((s for s in await asyncio.to_thread(database.shops) if s["id"] == job["shop_id"]), None)
        if not shop or shop["node_id"] != node or shop["local_id"] != job["local_shop_id"]:
            ledger.update(jid, state="exception", message="店铺已切换节点，停止原节点导出")
            return
        if not job.get("remote_job_id"):
            try:
                capabilities = json.loads(await node_call(node, "GET", "report-exports/settings"))
            except ValueError:
                capabilities = {}
            if (
                job.get("kind") == "promotion"
                and ("promotion_all" if job["start"] == "all" else "promotion") not in capabilities.get("kinds", [])
            ) or (
                job.get("kind", "orders") == "orders"
                and not set(job["fields"]).issubset(capabilities.get("fields", []))
            ):
                ledger.update(
                    jid,
                    message="等待所属采集节点更新推广月账单支持"
                    if job.get("kind") == "promotion"
                    else "等待所属采集节点更新导出字段支持，暂不提交旧字段报表",
                    transfer_retry_at=time.time() + 15,
                )
                return
            await node_call(node, "PUT", "report-exports/settings", {"retention_days": cfg.get("retention_days", 7)})
            raw = await node_call(
                node,
                "POST",
                "report-exports",
                {
                    "id": jid,
                    "shop_id": job["local_shop_id"],
                    "start": job["start"],
                    "end": job["end"],
                    "fields": job["fields"],
                    "kind": job.get("kind", "orders"),
                },
            )
            remote = json.loads(raw)
            job = ledger.update(jid, remote_job_id=remote["id"])
        else:
            remote = json.loads(await node_call(node, "GET", "report-exports/" + job["remote_job_id"]))
        status = remote["state"]
        if type(remote.get("file_bytes")) is int:
            ledger.update(jid, node_file_bytes=remote["file_bytes"], storage_checked_at=time.time())
        ledger.update(
            jid,
            state="downloading" if status == "completed" else status,
            message="正在接收到中心" if status == "completed" else remote["message"],
            progress=remote.get("progress"),
            platform_id=remote.get("platform_id"),
            submitted_at=remote.get("submitted_at"),
            retry_at=remote.get("retry_at", 0),
            submission_confirmed=remote.get("submission_confirmed", False),
            stage=remote.get("stage"),
            screenshot_error=remote.get("screenshot_error", ""),
            node_updated=remote.get("updated"),
        )
        if remote.get("screenshot") and (
            not job.get("screenshot") or job.get("screenshot_at") != remote.get("screenshot_at")
        ):
            data = await node_call(node, "GET", f"report-exports/{job['remote_job_id']}/screenshot")
            if not 0 < len(data) <= 5 * 1024 * 1024 or not data.startswith(b"\xff\xd8"):
                raise ValueError("失败截图格式或大小异常")
            folder = ledger.path(jid)
            folder.mkdir(exist_ok=True)
            (folder / "failure.jpg").write_bytes(data)
            ledger.update(jid, screenshot=True, screenshot_at=remote.get("screenshot_at"))
        if status != "completed":
            return
        size = remote.get("size", 0)
        if not 0 < size <= MAX_BYTES * 2:
            raise ValueError("报表文件大小异常")
        folder = ledger.path(jid)
        folder.mkdir(exist_ok=True)
        temp = folder / "download.part"
        digest = hashlib.sha256()
        with temp.open("wb") as out:
            for offset in range(0, size, 1024 * 1024):
                chunk = await node_call(
                    node, "GET", f"report-exports/{job['remote_job_id']}/chunk", query="offset=" + str(offset)
                )
                if len(chunk) != min(1024 * 1024, size - offset):
                    raise ValueError("报表传输不完整，将自动重新接收")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != remote["sha256"]:
            raise ValueError("报表校验不一致，将自动重新接收")
        temp.replace(folder / "report.zip")
        ledger.update(
            jid, state="completed", message="中心已接收，等待导入", import_state="pending", size=size, retry_at=0
        )

    async def refresh_storage(job):
        async with job_lock(job["id"]):
            try:
                current = ledger.get(job["id"])
                if current["state"] == "deleting":
                    return
                remote = json.loads(
                    await asyncio.wait_for(node_call(job["node"], "GET", "report-exports/" + job["remote_job_id"]), 6)
                )
                if remote.get("state") != current["state"] and remote.get("state") in ACTIVE | {"completed"}:
                    ledger.update(
                        job["id"], state="queued", transfer_retry_at=0, message="采集节点已恢复原导出任务，正在同步进度"
                    )
                # Storage inspection must not reset the report retention clock.
                value = remote.get("file_bytes")
                ledger.save(
                    {
                        **ledger.get(job["id"]),
                        **({"node_file_bytes": value} if type(value) is int and value >= 0 else {}),
                        "ui_recovery_revision": remote.get("ui_recovery_revision"),
                        "storage_checked_at": time.time(),
                        "storage_retry_at": 0,
                        "node_report_missing": False,
                        "storage_error": "",
                    }
                )
            except (OSError, ValueError, HTTPException, TimeoutError) as error:
                try:
                    current = ledger.get(job["id"])
                except OSError, ValueError:
                    return
                missing = isinstance(error, NodeReportError) and error.status == 404
                # Persist failed inspections too: otherwise the 2-second loop floods nodes.
                # Storage metadata must never extend retention or change imported data.
                ledger.save(
                    {
                        **current,
                        "storage_checked_at": time.time(),
                        "storage_retry_at": time.time()
                        + (86400 if missing and current["state"] == "completed" else 300),
                        "storage_error": "节点报表记录已不存在" if missing else str(error)[:250],
                        **({"node_report_missing": True, "node_file_bytes": 0} if missing else {}),
                    }
                )

    async def run():
        workers = {}
        gate = asyncio.Semaphore(4)

        async def storage(job):
            async with gate:
                await refresh_storage(job)

        async def process(job, cfg):
            async with gate, job_lock(job["id"]):
                try:
                    try:
                        current = ledger.get(job["id"])
                    except FileNotFoundError:
                        return
                    if current["state"] == "deleting":
                        return
                    if job["state"] == "completed" and (
                        job.get("reimport_pending") or job.get("import_state") not in ("ready", "error")
                    ):
                        ledger.update(
                            job["id"],
                            import_state="ready" if job.get("reimport_pending") else "importing",
                            message="正在重新导入，保留原报表" if job.get("reimport_pending") else "正在导入报表",
                        )
                        try:
                            if job.get("kind") == "promotion":
                                from .promotion_ledger import import_report

                                count = await asyncio.to_thread(
                                    import_report, database.engine, job, ledger.path(job["id"]) / "report.zip"
                                )
                            else:
                                count = await asyncio.to_thread(
                                    report_view.import_report,
                                    database.engine,
                                    job,
                                    ledger.path(job["id"]) / "report.zip",
                                )
                                from .reconciliation import snapshot

                                try:
                                    await asyncio.to_thread(snapshot, database.engine, job["id"], refresh_rules=True)
                                except Exception as e:
                                    ledger.update(job["id"], accounting_error=str(e)[:250])
                            ledger.update(
                                job["id"],
                                import_state="ready",
                                import_scope="all" if job.get("kind") == "promotion" else "selected",
                                row_count=count,
                                message=f"已导入 {count} 行，可查看数据",
                                import_error="",
                                reimport_pending=False,
                            )
                        except Exception as e:
                            retained = job.get("reimport_pending")
                            ledger.update(
                                job["id"],
                                import_state="ready" if retained else "error",
                                import_error=str(e)[:250],
                                reimport_pending=False,
                                message="重新导入失败，保留原报表" if retained else "导入异常，原文件可下载",
                            )
                        return
                    await synchronize(job, cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    message = str(getattr(error, "detail", None) or error)[:250] or type(error).__name__
                    if isinstance(error, NodeReportError) and error.status == 404 and job.get("remote_job_id"):
                        # The node no longer has this job (cleared or reinstalled): retrying
                        # every 30 s only produces 404s. Stop and ask for a new run instead.
                        ledger.update(
                            job["id"],
                            state="exception",
                            node_report_missing=True,
                            message="采集节点已没有此导出任务（可能已被清理或节点重装），请重新执行",
                            transfer_retry_at=0,
                        )
                        return
                    attempts = job.get("transfer_attempts", 0) + 1
                    ledger.update(
                        job["id"],
                        state="exception" if attempts >= 5 else job["state"],
                        transfer_attempts=attempts,
                        message="接收未完成：" + message,
                        transfer_retry_at=time.time() + 30,
                    )

        async def prepare_accounting(job):
            from .reconciliation import snapshot

            async with gate, job_lock(job["id"]):
                try:
                    await asyncio.to_thread(snapshot, database.engine, job["id"])
                except Exception as e:
                    ledger.update(job["id"], accounting_error=str(e)[:250])

        health["running"] = True
        try:
            last_cleanup = 0
            initialized = False
            while True:
                try:
                    if not initialized:
                        for saved in ledger.all():
                            if (
                                saved.get("kind") == "promotion"
                                and saved.get("import_state") == "ready"
                                and saved.get("import_scope") != "all"
                                and (ledger.path(saved["id"]) / "report.zip").is_file()
                            ):
                                ledger.update(
                                    saved["id"], import_state="pending", message="按全部日期重新导入已下载账单"
                                )
                        initialized = True
                    cfg = export_config()
                    for jid, task in list(workers.items()):
                        if task.done():
                            outcome = await asyncio.gather(task, return_exceptions=True)
                            workers.pop(jid, None)
                            if isinstance(outcome[0], Exception):
                                logging.getLogger("ecmp.center").error(
                                    "报表后台子任务失败，将按状态恢复：%s", type(outcome[0]).__name__
                                )
                    from .reconciliation import bindings

                    with database.engine.connect() as db:
                        from .cost_tables import catalog

                        has_cost = db.execute(select(catalog.c.active_id).where(catalog.c.id == 1)).scalar()
                        accounted = set(
                            db.execute(
                                select(bindings.c.job_id).where(
                                    bindings.c.summary["rows"].as_string().is_not(None),
                                    *([bindings.c.book_id.is_not(None)] if has_cost else []),
                                )
                            ).scalars()
                        )
                        imported = set(
                            db.execute(
                                select(report_view.documents.c.id).where(report_view.documents.c.ready == 1)
                            ).scalars()
                        )
                    for job in ledger.all():
                        if job["id"] in workers:
                            continue
                        if (
                            job.get("kind", "orders") == "orders"
                            and job.get("import_state") == "ready"
                            and job["state"] == "completed"
                            and job["id"] in imported
                            and job["id"] not in accounted
                            and not job.get("accounting_error")
                        ):
                            workers[job["id"]] = asyncio.create_task(prepare_accounting(job))
                            continue
                        importing = job["state"] == "completed" and (
                            job.get("reimport_pending") or job.get("import_state") not in ("ready", "error")
                        )
                        interval = (
                            15
                            if job["state"] == "exception"
                            and "字段" in job.get("message", "")
                            and not job.get("ui_recovery_revision")
                            else 300
                        )
                        if (
                            job["state"] in ("completed", "exception")
                            and not importing
                            and job.get("remote_job_id")
                            and not job.get("node_report_missing")
                            and job.get("node") in hub.peers
                            and time.time() >= job.get("storage_retry_at", 0)
                            and time.time() - job.get("storage_checked_at", 0) > interval
                        ):
                            workers[job["id"]] = asyncio.create_task(storage(job))
                            continue
                        if not importing and (
                            job["state"] not in ACTIVE or job.get("transfer_retry_at", 0) > time.time()
                        ):
                            continue
                        if not importing and job.get("node") not in hub.peers:
                            if job.get("message") != "等待所属采集节点连接":
                                ledger.update(job["id"], message="等待所属采集节点连接")
                            continue
                        workers[job["id"]] = asyncio.create_task(process(job, cfg))
                    if time.monotonic() - last_cleanup >= 30:
                        last_cleanup = time.monotonic()
                        for record in deletion_requests():
                            key = "delete:" + record["id"]
                            if key not in workers:
                                workers[key] = asyncio.create_task(finish_delete(record))
                        await asyncio.to_thread(ledger.cleanup, cfg.get("retention_days", 7))
                        await asyncio.to_thread(report_view.cleanup_staging, database.engine)
                    health.update(consecutive_failures=0, last_success=time.time(), last_error="", retry_at=0)
                    delay = 2
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failures = health["consecutive_failures"] + 1
                    delay = min(30, 2 ** min(failures, 5))
                    health.update(
                        consecutive_failures=failures, last_error=type(error).__name__, retry_at=time.time() + delay
                    )
                    logging.getLogger("ecmp.center").exception("报表后台调度异常，%s 秒后自动恢复", delay)
                await asyncio.sleep(delay)
        finally:
            health["running"] = False
            for task in workers.values():
                task.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)

    async def start():
        app.state.report_export_task = asyncio.create_task(run())

    async def stop():
        app.state.report_export_task.cancel()
        await asyncio.gather(app.state.report_export_task, return_exceptions=True)

    from .lifecycle import chain

    chain(app, start, stop)
