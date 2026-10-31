from . import recovery_log
from .filter_scope import matches
import asyncio
import hmac
import json
import os
import secrets
import sqlite3
import time
import re
import copy
from types import SimpleNamespace
from .query_cache import QueryCache
from . import update_gate
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Header, HTTPException, Request, Query, WebSocket
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from .store import Store, ROOT
from .browser import Session, chrome_path
from .platform_support import supports, source_page
from .platform_login import home as platform_home
from .business import BusinessService, create_router
from .business_reader import PAGES
from .process_status import IDENTITY as PROCESS_IDENTITY

store = Store()


def retire_legacy_credentials(store, path):
    # Import once, then move plaintext credentials aside so later starts never read them again.
    store.import_legacy(path)
    with store.connect() as db:
        imported = db.execute("SELECT 1 FROM settings WHERE key='legacy_imported'").fetchone()
    if imported and path.is_file():
        target = path.with_name(path.name + ".imported")
        index = 1
        while target.exists():
            target = path.with_name(f"{path.name}.imported.{index}")
            index += 1
        try:
            path.rename(target)
        except OSError:
            pass


if not os.getenv("PDD_DATA_DIR"):
    retire_legacy_credentials(store, ROOT / "pdd-login.local.txt")
token_path = store.directory / "api.key"
if not token_path.exists():
    token_path.write_text(secrets.token_hex(32))
    os.chmod(token_path, 0o600)
TOKEN = os.getenv("PDD_API_TOKEN") or token_path.read_text().strip()
query_cache = QueryCache()
sessions = {}


def session(shop_id):
    if shop_id not in sessions:
        record = next((s for s in store.list() if s["id"] == shop_id), None)
        if record is None:
            raise HTTPException(404, "店铺不存在")
        sessions[shop_id] = Session(record, store)
    return sessions[shop_id]


async def monitor():
    from .browser_recovery import check

    pending = {}

    async def inspect(item):
        from .browser_sleep import maintain

        await maintain(item, business)
        active = any(k[0] == item.shop["id"] and m.get("phase") == "running" for k, m in business.progress.items())
        if active or item.visible or item.state in ("logging_in", "needs_verification"):
            await check(item)

    try:
        while True:
            for item in list(sessions.values()):
                sid = item.shop["id"]
                if sid not in pending or pending[sid].done():
                    if sid in pending:
                        # Observe exceptions without allowing one shop to stop monitoring others.
                        await asyncio.gather(pending.pop(sid), return_exceptions=True)
                    pending[sid] = asyncio.create_task(inspect(item))
            await asyncio.sleep(3)
    finally:
        for task in pending.values():
            task.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)


@asynccontextmanager
async def lifespan(app):
    from . import runtime_state
    from .collector_lease import ownership

    with ownership(store):
        for record in store.list():
            item = session(record["id"])
            if getattr(item, "sleeping", False):
                continue
            try:
                await item.reclaim_read_pages()
                await item.target()
                if item.wanted_running is None:
                    from .browser_recovery import remember

                    remember(item, True)
                item.state = "logging_in"
                if hasattr(item, "launch_visibility"):
                    await item.quiet_window(item.launch_visibility())
                elif item.manual_visibility is not None:
                    await item.window(item.manual_visibility)
                await item.refresh()
            except Exception:
                if item.wanted_running:
                    from .browser_sleep import remember as remember_sleep

                    remember_sleep(item, True)
                    item.state = "sleeping"
                else:
                    item.state = "stopped"
        from . import log_spool

        logs = asyncio.create_task(log_spool.run(store))
        runtime = asyncio.create_task(runtime_state.run(store, business, sessions))
        task = asyncio.create_task(monitor())
        scheduled = asyncio.create_task(scheduler.run())
        from .direct_refunds import DirectRefunds

        business.direct_refunds = DirectRefunds(business)
        direct_refunds_task = asyncio.create_task(business.direct_refunds.run())
        maintained = asyncio.create_task(maintenance.run())
        from .invoice_upload import UploadWorker

        invoice_worker = asyncio.create_task(UploadWorker(store, session).run())
        report_task = asyncio.create_task(report_worker.run())
        yield
        direct_refunds_task.cancel()
        await asyncio.gather(direct_refunds_task, return_exceptions=True)
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
        invoice_worker.cancel()
        await asyncio.gather(invoice_worker, return_exceptions=True)
        runtime.cancel()
        await asyncio.gather(runtime, return_exceptions=True)
        scheduled.cancel()
        maintained.cancel()
        await asyncio.gather(maintained, return_exceptions=True)
        await asyncio.gather(scheduled, return_exceptions=True)
        task.cancel()
        await business.close()
        from .read_page_pool import close_idle

        await asyncio.gather(*(close_idle(item) for item in sessions.values()), return_exceptions=True)
        logs.cancel()
        await asyncio.gather(logs, return_exceptions=True)
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="PDD Shop Browser API", lifespan=lifespan)


def token_matches(value):
    # Compare bytes: str comparison raises TypeError for non-ASCII header values.
    return hmac.compare_digest(str(value or "").encode("utf-8", "surrogatepass"), ("Bearer " + TOKEN).encode("utf-8"))


def authorize(authorization: str = Header(default="")):
    if not token_matches(authorization):
        raise HTTPException(401, "API 密钥错误")


@app.middleware("http")
async def record_api_errors(request, call_next):
    path = request.url.path
    if (
        update_gate.active()
        and not path.startswith("/api/system/update-")
        and ((request.method != "GET" and not path.endswith("/input")) or path.endswith("/screenshot"))
    ):
        return Response(
            '{"detail":"正在为升级暂停新任务，当前操作完成后自动恢复"}', status_code=503, media_type="application/json"
        )
    eligible = (
        request.method == "GET"
        and (request.url.path.startswith("/api/data/") or request.url.path == "/api/todos")
        and request.query_params.get("export", "false").lower() not in ("true", "1", "yes", "on")
        and token_matches(request.headers.get("authorization", ""))
    )
    from .data_versions import revision as data_revision
    from datetime import datetime
    from .business_reader import BEIJING

    key = (
        str(store.path),
        datetime.now(BEIJING).date().isoformat(),
        request.url.path,
        tuple(sorted(request.query_params.multi_items())),
    )
    revision = data_revision(store, request.url.path, request.query_params) if eligible else None
    if eligible:
        saved = query_cache.get(key, revision)
        if saved:
            return Response(
                saved[0], media_type=saved[1], headers={"Cache-Control": "no-store", "X-Query-Cache": "hit"}
            )
    try:
        response = await call_next(request)
    except Exception as error:
        store.log_event(
            "error", "api", "unhandled_error", f"{request.method} {request.url.path} · {type(error).__name__}"
        )
        raise
    if response.status_code >= 400 and request.url.path.startswith("/api/") and request.url.path != "/api/logs":
        match = re.search(r"/shops/(\d+)", request.url.path)
        store.log_event(
            "error" if response.status_code >= 500 else "warning",
            "api",
            "request_error",
            f"{request.method} {request.url.path} · HTTP {response.status_code}",
            int(match[1]) if match else None,
        )
    if (
        request.method != "GET"
        and response.status_code < 400
        and not request.url.path.endswith(("/sync", "/input"))
        and request.url.path != "/api/logs"
    ):
        store.invalidate_responses()
    if eligible and response.status_code == 200:
        body = b"".join([part async for part in response.body_iterator])
        if revision == data_revision(store, request.url.path, request.query_params):
            query_cache.put(key, revision, body, response.headers.get("content-type", "application/json"))
        return Response(
            body,
            status_code=200,
            headers={**dict(response.headers), "Cache-Control": "no-store", "X-Query-Cache": "miss"},
        )
    return response


@app.middleware("http")
async def measure_queries(request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.method == "GET" and request.url.path.startswith("/api/data/") and response.status_code == 200:
        from .request_metrics import record

        elapsed = (time.perf_counter() - started) * 1000
        record(request.url.path, elapsed, response.headers.get("X-Query-Cache"))
        response.headers["Server-Timing"] = f"database;dur={elapsed:.1f}"
    return response


@app.get("/api/system/performance", dependencies=[Depends(authorize)])
def performance_status():
    from .request_metrics import snapshot

    return snapshot()


@app.get("/api/attention", dependencies=[Depends(authorize)])
def attention_data(
    platform: str = "",
    shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
    kind: str = "",
    page: int = Query(default=1, ge=1),
    summary: bool = False,
    keyword: str = Query(default="", max_length=200),
    sort_by: str = "",
    sort_direction: str = Query("asc", pattern="^(asc|desc)$"),
):
    from .attention import read

    return read(business, platform, shop_id, kind, page, summary, keyword, sort_by, sort_direction)


@app.post("/api/attention/sync", status_code=202, dependencies=[Depends(authorize)])
async def attention_sync(
    platform: str = "",
    shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
    kind: str = "",
):
    from .attention import scopes

    count = 0
    for record in await asyncio.to_thread(store.list):
        if not matches(platform, record.get("platform", "pdd")) or not matches(shop_id, record["id"]):
            continue
        for module, options in scopes(record):
            if kind and module != kind:
                continue
            await business.enqueue_async(record, session(record["id"]), module, options, refresh=True, force=True)
            count += 1
    return {"accepted": count}


@app.get("/api/events", dependencies=[Depends(authorize)])
async def database_events(request: Request):
    from .update_events import events

    return StreamingResponse(
        events(store, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/logs", dependencies=[Depends(authorize)])
def logs(
    level: str = Query(default="", pattern="^(|info|warning|error)$"),
    shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
    keyword: str = Query(default="", max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
    module: str = "",
    date_from: str = "",
    date_to: str = "",
    before: float = 0,
    before_id: int = 0,
    after: float = 0,
):
    try:
        rows = store.read_logs(level, shop_id, keyword, limit + 1, module, date_from, date_to, before, before_id, after)
    except ValueError:
        raise HTTPException(400, "日志日期无效")
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "rows": rows,
        "has_more": more,
        "next": {"before": rows[-1]["last_seen"], "before_id": rows[-1]["id"]} if more and rows else None,
        "timezone": "Asia/Shanghai",
    }


class ClientLog(BaseModel):
    message: str = Field(max_length=500)


@app.post("/api/logs", dependencies=[Depends(authorize)])
def client_log(value: ClientLog):
    store.log_event("error", "frontend", "client_error", value.message)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True, "runtime": PROCESS_IDENTITY, "server_time": time.time()}


@app.post("/api/system/update-prepare", dependencies=[Depends(authorize)])
async def update_prepare():
    # Establish admission control BEFORE checking running work. Otherwise a busy
    # scheduler can replace each finishing task and starve the updater forever.
    # Never pause while someone is remotely operating a shop browser: report instead.
    items = list(sessions.values())
    blockers = update_gate.blockers(business, items)
    if blockers.get("remote_controls"):
        raise HTTPException(
            409,
            {
                "phase": "remote_control",
                "message": "有店铺正在远程控制，暂不进入升级",
                "blockers": blockers,
                "retry_after": 5,
            },
        )
    update_gate.renew()
    blockers = update_gate.blockers(business, items)
    if any(blockers.values()):
        raise HTTPException(
            409,
            {
                "phase": "draining",
                "message": "已暂停派发新任务，正在等待当前采集、上传或远程控制结束",
                "blockers": blockers,
                "retry_after": 2,
            },
        )
    return {"ready": True, "expires_in": 30}


@app.get("/api/system/update-blockers", dependencies=[Depends(authorize)])
async def update_blockers():
    return {"blockers": update_gate.blockers(business, list(sessions.values()))}


@app.post("/api/system/update-cancel", dependencies=[Depends(authorize)])
async def update_cancel():
    update_gate.release()
    business.wake.set()
    return {"resumed": True}


class ShopInput(BaseModel):
    username: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=1024)
    name: str = Field(default="", max_length=100)
    browser: str = "system"
    platform: str = "pdd"

    operator: str = Field(default="", max_length=10000)
    login_method: str = "password"
    login_url: str = Field(default="", max_length=2000)


class ShopEdit(ShopInput):
    password: str = Field(default="", max_length=1024)


class ShopImportInput(BaseModel):
    content: str = Field(max_length=1048576)
    browser: str = "system"
    commit: bool = False
    platform: str = "pdd"


class OperatorInput(BaseModel):
    name: str = Field(default="", max_length=100)
    names: list[str] | None = Field(default=None, max_length=100)
    previous: str | None = Field(default=None, max_length=100)


class AssignOperator(BaseModel):
    shop_ids: list[int] = Field(min_length=1, max_length=5000)
    operator: str = Field(default="", max_length=10000)
    operators: list[str] | None = Field(default=None, max_length=100)
    mode: str = "replace"


def refresh_shop_metadata():
    for record in store.list():
        if record["id"] in sessions:
            sessions[record["id"]].shop = record


@app.get("/api/operators", dependencies=[Depends(authorize)])
def operators():
    return store.operators()


@app.post("/api/operators", dependencies=[Depends(authorize)])
def save_operator(data: OperatorInput):
    try:
        if data.names is not None:
            store.save_operators(data.names)
        else:
            store.save_operator(data.name, data.previous)
    except ValueError as error:
        raise HTTPException(400, str(error))
    refresh_shop_metadata()
    return store.operators()


@app.post("/api/shops/operators", dependencies=[Depends(authorize)])
def assign_operators(data: AssignOperator):
    try:
        store.assign_operator(data.shop_ids, data.operators if data.operators is not None else data.operator, data.mode)
    except ValueError as error:
        raise HTTPException(400, str(error))
    refresh_shop_metadata()
    return {"ok": True}


@app.post("/api/shops/import", dependencies=[Depends(authorize)])
def batch_import(data: ShopImportInput):
    from .shop_import import import_shops

    try:
        result = import_shops(store, data.content, data.browser, data.commit, data.platform)
        if result["imported"]:
            store.log_event(
                "info",
                "shops",
                "batch_import",
                f"批量导入完成：新增 {result['imported']} 家，跳过 {result['skipped']} 家",
            )
        return result
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.get("/api/shops", dependencies=[Depends(authorize)])
def shops():
    return [session(s["id"]).public() for s in store.list()]


@app.get("/api/browsers", dependencies=[Depends(authorize)])
def browsers():
    result = []
    for kind in ("system", "bundled"):
        try:
            chrome_path(kind)
            available = True
        except ValueError:
            available = False
        result.append({"id": kind, "available": available})
    return result


@app.get("/api/shops/{shop_id}/refund-actions", dependencies=[Depends(authorize)])
def refund_actions(
    shop_id: int,
    before: float = 0,
    limit: int = Query(default=100, ge=1, le=200),
    state: str = Query(default="", max_length=20),
    before_case_id: str = Query(default="", max_length=100),
):
    session(shop_id)  # Validate existence; never wake a browser for audit reads.
    from .auto_refunds import records

    return records(store, shop_id, limit, before, state, before_case_id)


@app.post("/api/shops", dependencies=[Depends(authorize)])
def add(data: ShopInput):
    try:
        shop_id = store.add(**data.model_dump())
        return session(shop_id).public()
    except (ValueError, sqlite3.IntegrityError) as error:
        raise HTTPException(400, "该账号已存在" if isinstance(error, sqlite3.IntegrityError) else str(error))


@app.put("/api/shops/{shop_id}", dependencies=[Depends(authorize)])
async def edit(shop_id: int, data: ShopEdit):
    item = session(shop_id)
    await business.cancel_shop(shop_id)
    async with item.lock:
        if item.retired:
            raise HTTPException(409, "店铺配置已变更，请刷新后重试")
        try:
            old = await asyncio.to_thread(store.credentials, shop_id)
            store.validate(
                data.username,
                data.password or old["password"],
                data.browser,
                data.name,
                data.platform,
                data.operator,
                data.login_method,
                data.login_url,
            )
            if any(
                row["id"] != shop_id
                and row.get("platform", "pdd") == data.platform
                and row["username"].casefold() == data.username.strip().casefold()
                for row in await asyncio.to_thread(store.list)
            ):
                raise ValueError("该账号已存在")
            restart = (
                data.username.strip() != old["username"]
                or (bool(data.password) and data.password != old["password"])
                or data.browser != old["browser"]
                or data.platform != old.get("platform", "pdd")
                or data.login_url != old.get("login_url", "")
            )
            if restart:
                try:
                    await item.stop()
                except Exception:
                    raise HTTPException(503, "无法关闭店铺浏览器，修改未保存，请稍后重试") from None
            await asyncio.to_thread(store.update, shop_id, **data.model_dump())
            record = next(row for row in await asyncio.to_thread(store.list) if row["id"] == shop_id)
            if restart:
                item.retired = True
                sessions[shop_id] = Session(record, store)
            else:
                item.shop = record
                if old.get("login_method") != record.get("login_method"):
                    item.qr_requested = record.get("login_method") == "qr"
                    item.auto_login_attempted = False
            return sessions[shop_id].public()
        except HTTPException:
            raise
        except (ValueError, sqlite3.IntegrityError) as error:
            raise HTTPException(400, "该账号已存在" if isinstance(error, sqlite3.IntegrityError) else str(error))
        except Exception as error:
            message = f"{type(error).__name__}: {error}"[:200]
            try:
                await asyncio.to_thread(
                    store.log_event, "error", "shops", "edit_failed", "修改店铺失败 · " + message, shop_id
                )
            except Exception:
                recovery_log.suppressed("main.py:371 恢复步骤未完成", warning=False)
            raise HTTPException(500, "修改店铺失败：" + message) from None


@app.delete("/api/shops/{shop_id}", dependencies=[Depends(authorize)])
async def delete(shop_id: int):
    item = session(shop_id)
    await business.cancel_shop(shop_id)
    async with item.lock:
        if item.retired:
            raise HTTPException(409, "店铺配置已变更，请刷新后重试")
        try:
            await item.stop()
        except Exception:
            raise HTTPException(503, "无法关闭店铺浏览器，店铺未删除，请稍后重试") from None
        try:
            await asyncio.to_thread(store.delete, shop_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        except Exception as error:
            message = f"{type(error).__name__}: {error}"[:200]
            try:
                await asyncio.to_thread(
                    store.log_event, "error", "shops", "delete_failed", "删除店铺失败 · " + message, shop_id
                )
            except Exception:
                recovery_log.suppressed("main.py:388 恢复步骤未完成", warning=False)
            raise HTTPException(500, "删除店铺失败：" + message) from None
        item.retired = True
        sessions.pop(shop_id, None)
        return {"ok": True, "id": shop_id}


@app.post("/api/shops/{shop_id}/transfer-credentials", dependencies=[Depends(authorize)])
async def transfer_credentials(shop_id: int):
    # Private node tunnel operation; center rejects direct browser proxy access.
    try:
        record = await asyncio.to_thread(store.credentials, shop_id)
    except KeyError, ValueError:
        raise HTTPException(404, "原节点未找到店铺配置")
    if not record.get("password"):
        raise HTTPException(409, "原节点未保存密码，请选择扫码登录或先在原店铺补充密码")
    return Response(
        json.dumps({"password": record["password"]}),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/shops/{shop_id}/{action}", dependencies=[Depends(authorize)])
async def action(shop_id: int, action: str, request: Request):
    item = session(shop_id)
    if action in PAGES and not supports(item.shop.get("platform", "pdd"), action):
        raise HTTPException(400, "该平台支持官方登录及浏览器控制，业务数据采集尚未接入")
    if action in ("debug-on", "debug-off"):
        # Per-shop debug mode: show the browser during collection. Never cancels work.
        try:
            async with item.lock:
                await item.set_debug_mode(action == "debug-on")
        except ValueError as error:
            raise HTTPException(400, str(error))
        return item.public()
    if action in ("stop", "browser", "show", "hide", "qr-login") or action in PAGES:
        await business.cancel_shop(shop_id)
    try:
        from .browser_sleep import manual_action

        if await manual_action(item, action):
            return item.public()
        if action == "start":
            await item.start()
        elif action == "qr-login":
            item.qr_requested = True
            await item.start()
        elif action in ("show", "hide"):
            async with item.lock:
                await item.window(action == "show")
                item.remember_visibility(action == "show")
        elif action == "retry":
            async with item.lock:
                item.qr_requested = item.shop.get("login_method") == "qr"
                submitted = await item.fill_login()
                if submitted and item.state not in ("needs_verification", "logged_in"):
                    item.state = "logging_in"
        elif action == "home":
            async with item.lock:
                await item.command("Page.navigate", {"url": platform_home(item.shop)})
        elif action in PAGES:
            async with item.lock:
                await item.command("Page.navigate", {"url": source_page(item.shop.get("platform", "pdd"), action)})
                await item.window(True, activate=False)
                item.remember_visibility(True)
        elif action == "maximize":
            async with item.lock:
                await item.maximize()
        elif action == "stop":
            async with item.lock:
                await item.stop()
        elif action == "browser":
            async with item.lock:
                if item.retired:
                    raise ValueError("店铺配置已变更，请刷新后重试")
                if item.state != "stopped":
                    raise ValueError("请先停止该店铺浏览器，再切换浏览器类型")
                kind = (await request.json()).get("browser")
                chrome_path(kind)
                store.browser(shop_id, kind)
                item.shop["browser"] = kind
                item.retired = True
                item = sessions[shop_id] = Session(item.shop.copy(), store)
        else:
            raise HTTPException(404, "操作不存在")
        return item.public()
    except ValueError as error:
        raise HTTPException(400, str(error))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "浏览器操作失败，请确认浏览器已启动")


@app.get("/api/shops/{shop_id}/screenshot", dependencies=[Depends(authorize)])
async def screenshot(shop_id: int):
    try:
        item = session(shop_id)
        first = getattr(item, "control_until", 0) <= time.time()
        item.control_until = time.time() + 10
        if first:
            await business.cancel_shop(shop_id)
        return Response(await item.screenshot(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except Exception:
        raise HTTPException(503, "登录画面暂未就绪")


@app.websocket("/api/shops/{shop_id}/control")
async def browser_control(ws: WebSocket, shop_id: int, node_id: int | None = None):
    from .remote_control import serve

    if not token_matches(ws.headers.get("authorization", "")):
        await ws.close(code=4401)
        return
    if update_gate.active():
        await ws.accept()
        await ws.close(code=1013)
        return
    try:
        item = session(shop_id)
    except HTTPException:
        await ws.accept()
        await ws.close(code=4404)
        return
    await serve(ws, item, business)


class Input(BaseModel):
    type: str
    x: float = Field(default=0, ge=0, le=10000)
    y: float = Field(default=0, ge=0, le=10000)
    text: str = Field(default="", max_length=1024)
    key: str = Field(default="", max_length=20)
    delta_y: float = Field(default=0, ge=-2000, le=2000)
    delta_x: float = Field(default=0, ge=-2000, le=2000)


@app.post("/api/shops/{shop_id}/input", dependencies=[Depends(authorize)])
async def input_event(shop_id: int, event: Input):
    item = session(shop_id)
    item.control_until = time.time() + 30
    try:
        async with item.lock:
            if event.type == "text":
                await item.command("Input.insertText", {"text": event.text})
            elif event.type == "enter":
                for kind in ("keyDown", "keyUp"):
                    await item.command(
                        "Input.dispatchKeyEvent",
                        {"type": kind, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
                    )
            elif event.type == "wheel":
                await item.command(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": event.x,
                        "y": event.y,
                        "deltaX": event.delta_x,
                        "deltaY": event.delta_y,
                    },
                )
            elif event.type == "key":
                keys = {
                    "Backspace": 8,
                    "Tab": 9,
                    "Escape": 27,
                    "Delete": 46,
                    "ArrowLeft": 37,
                    "ArrowUp": 38,
                    "ArrowRight": 39,
                    "ArrowDown": 40,
                    "Home": 36,
                    "End": 35,
                }
                if event.key not in keys:
                    raise HTTPException(400, "按键不支持")
                for kind in ("keyDown", "keyUp"):
                    await item.command(
                        "Input.dispatchKeyEvent",
                        {"type": kind, "key": event.key, "windowsVirtualKeyCode": keys[event.key]},
                    )
            elif event.type in ("mousePressed", "mouseMoved", "mouseReleased"):
                await item.command(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event.type,
                        "x": event.x,
                        "y": event.y,
                        "button": "left" if event.type != "mouseMoved" else "none",
                        "buttons": 0 if event.type == "mouseReleased" else 1,
                        "clickCount": 1 if event.type != "mouseMoved" else 0,
                    },
                )
            else:
                raise HTTPException(400, "输入类型不支持")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "浏览器输入失败")


# Explicit input route must precede the generic action route.
input_route = next(r for r in app.router.routes if getattr(r, "path", "") == "/api/shops/{shop_id}/input")
app.router.routes.remove(input_route)
action_index = next(
    i for i, r in enumerate(app.router.routes) if getattr(r, "path", "") == "/api/shops/{shop_id}/{action}"
)
app.router.routes.insert(action_index, input_route)

business = BusinessService(store, session)
from .report_exports import Worker as ReportWorker, router as report_router

report_worker = ReportWorker(store, session)
app.include_router(report_router(report_worker, authorize))
from .scheduler import Scheduler

scheduler = Scheduler(business)
from .database_maintenance import Maintenance

maintenance = Maintenance(store)
from .insights_api import router as insights_router

app.include_router(insights_router(business, authorize, maintenance))


@app.get("/api/system/settings", dependencies=[Depends(authorize)])
async def system_settings(include_step_metrics: bool = False, metrics_platform: str = ""):
    from .collection import settings, REALTIME, effective
    from .platforms import PLATFORMS

    config = await asyncio.to_thread(settings, store)
    metrics = {}
    if include_step_metrics:
        if metrics_platform and metrics_platform not in PLATFORMS:
            raise HTTPException(400, "采集平台无效")
        from .task_step_metrics import snapshot

        metrics = await asyncio.to_thread(snapshot, store, metrics_platform)
    return {
        **metrics,
        "effective": effective(config),
        "collection": config,
        "platforms": PLATFORMS,
        "realtime_modules": sorted(REALTIME),
        "scheduler_running": True,
        "last_tick": scheduler.last_tick,
    }


@app.put("/api/system/settings", dependencies=[Depends(authorize)])
async def save_system_settings(request: Request):
    from .collection import save_settings

    try:
        config = await asyncio.to_thread(save_settings, store, await request.json())
        business.configure_queue(config)
        from .collection import effective

        return {"collection": config, "effective": effective(config)}
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except TypeError, KeyError:
        raise HTTPException(400, "采集设置无效，请检查时间、间隔和模块选项") from None


@app.post("/api/system/clear-queue", dependencies=[Depends(authorize)])
async def clear_collection_queue():
    from .queue_control import clear

    return await clear(business)


@app.get("/api/system/home-collection", dependencies=[Depends(authorize)])
async def home_collection_state():
    from .collection import settings

    config = await asyncio.to_thread(settings, store)
    return {"paused": config.get("home_paused", False), "confirmed": True}


@app.put("/api/system/home-collection", dependencies=[Depends(authorize)])
async def home_collection_change(request: Request):
    value = await request.json()
    if not isinstance(value, dict) or set(value) != {"paused"} or type(value["paused"]) is not bool:
        raise HTTPException(400, "暂停状态应为布尔值")
    from .home_collection_control import change

    return await change(business, value["paused"])


@app.put("/api/system/concurrency", dependencies=[Depends(authorize)])
async def save_concurrency(request: Request):
    from .collection import save_settings
    from .collection_queue import CONCURRENCY_FIELDS, queue_status

    try:
        value = await request.json()
        if not isinstance(value, dict) or not value or set(value) - set(CONCURRENCY_FIELDS):
            raise ValueError("并发修改项无效")
        config = await asyncio.to_thread(save_settings, store, value)
        business.configure_queue(config)
        await asyncio.to_thread(
            store.log_event,
            "info",
            "system",
            "collection_concurrency",
            f"采集并发设置已保存：节点 {config['max_concurrent']}，默认单店 {config['shop_concurrent']}，单店覆盖 {len(config.get('shop_limits', {}))} 项",
        )
        return {"collection": config, "queues": queue_status(business.progress, config)}
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    except TypeError, KeyError:
        raise HTTPException(400, "并发设置无效") from None


status_tasks = {}


def frozen_status():
    frozen = copy.copy(business)
    for name in ("progress", "errors", "jobs"):
        source = getattr(business, name)
        setattr(frozen, name, {k: ({**v, 'timing_seconds': dict(v.get('timing_seconds', {})),
                                  'timing_current': dict(v['timing_current']) if v.get('timing_current') else None,
                                  'reuse_counts': dict(v.get('reuse_counts', {}))}
                                 if name == "progress" else v) for k, v in source.items()})
    frozen.waiting_login = set(business.waiting_login)
    frozen.completions = list(business.completions)
    states = {k: SimpleNamespace(state=v.state) for k, v in sessions.items()}
    frozen.session_getter = lambda key: states.get(key, SimpleNamespace(state="stopped"))
    frozen.deferred = {k: dict(v) for k, v in business.deferred.items()}
    frozen.dispatch_snapshot = business.due_plans.status()
    return frozen


async def read_status(full):
    # Copy live dictionaries on their owning event loop before the worker reads
    # them. A single in-flight snapshot per kind avoids concurrent metadata scans.
    entry = status_tasks.get(full)
    if not entry or (entry[1].done() and time.monotonic() - entry[0] > 2):
        frozen = frozen_status()
        from .system_status import snapshot, summary, progress_snapshot

        task = asyncio.create_task(
            asyncio.to_thread(progress_snapshot if full == "progress" else snapshot if full else summary, frozen)
        )
        entry = (time.monotonic(), task)
        status_tasks[full] = entry
    try:
        result = dict(await asyncio.shield(entry[1]))
    except Exception:
        if status_tasks.get(full) is entry:
            status_tasks.pop(full, None)
        raise
    if "collection" not in result:
        from .collection import settings

        result["collection"] = await asyncio.to_thread(settings, store)
    result["last_tick"] = scheduler.last_tick
    return result


@app.get("/api/system/status", dependencies=[Depends(authorize)])
async def system_status():
    return await read_status(True)


@app.get("/api/system/progress", dependencies=[Depends(authorize)])
async def system_progress(tasks: bool = False, page: int = Query(1, ge=1)):
    result = await read_status("progress")
    result.pop("sources", None)
    d = dict(result.get("diagnostics", {}))
    items = d.pop("tasks", [])
    d["task_count"] = len(items)
    d["page_count"] = max(1, (len(items) + 19) // 20)
    if tasks:
        d["tasks"] = items[(page - 1) * 20 : page * 20]
    result["diagnostics"] = d
    return result


@app.get("/api/system/sources", dependencies=[Depends(authorize)])
async def system_sources(
    shop_id: str = "",
    kind: str = "",
    cadence: str = "",
    state: str = "",
    keyword: str = "",
    sort_by: str = "",
    sort_direction: str = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    retry: bool = False,
    details: bool = False,
    snapshot: bool = False,
):
    from .source_pages import source_page

    return source_page(
        await read_status(True),
        shop_id,
        kind,
        cadence,
        state,
        keyword,
        sort_by,
        sort_direction,
        page,
        page_size,
        retry,
        details,
        snapshot,
    )


@app.get("/api/system/summary", dependencies=[Depends(authorize)])
async def system_summary(task_group: str = "", page: int = Query(1, ge=1)):
    if task_group:
        if task_group in ("running", "queued", "deferred"):
            from .collector_task_list import listing

            return await asyncio.to_thread(listing, frozen_status(), task_group, page, {}, {}, [])
        from .system_status import summary

        return await asyncio.to_thread(summary, frozen_status(), task_group, page)
    result = await read_status(False)
    result["shops"] = {
        "total": len(sessions),
        "logged_in": sum(s.state == "logged_in" for s in sessions.values()),
        "sleeping": sum(s.state == "sleeping" for s in sessions.values()),
    }
    return result


@app.get("/api/home/progress", dependencies=[Depends(authorize)])
async def home_progress(
    platform: str = "", shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$")
):
    from .home_progress import read

    return await asyncio.to_thread(read, business, platform, shop_id)


@app.get("/api/system/maintenance", dependencies=[Depends(authorize)])
async def maintenance_status():
    return await asyncio.to_thread(maintenance.listing)


@app.put("/api/system/maintenance", dependencies=[Depends(authorize)])
async def maintenance_settings(request: Request):
    from .database_maintenance import save

    try:
        return {"settings": save(store, await request.json())}
    except ValueError, TypeError:
        raise HTTPException(400, "备份时间或保留预览设置无效") from None


@app.post("/api/system/backup", dependencies=[Depends(authorize)])
async def backup_database():
    try:
        return await maintenance.backup()
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    except Exception:
        raise HTTPException(500, "备份失败，请查看系统日志") from None


app.include_router(create_router(business, authorize))
app.include_router(business.export_jobs.router(authorize))
from .workspace_api import create_router as create_workspace_router

app.include_router(create_workspace_router(business, store, authorize))
from .invoice_import_api import create_router as create_invoice_import_router

app.include_router(create_invoice_import_router(store, authorize))


class WindowBatchInput(BaseModel):
    action: str = Field(pattern="^(show|hide|tile)$")


window_batch_lock = asyncio.Lock()


async def operate_all_windows(action):
    from contextlib import AsyncExitStack
    from .window_layout import native_windows

    if window_batch_lock.locked():
        raise HTTPException(409, "已有窗口操作正在执行，请稍后重试")
    async with window_batch_lock:
        active = [session(r["id"]) for r in store.list() if platform_home(r)]
        active = [
            item
            for item in active
            if item.state not in ("stopped", "sleeping") and not item.headless and not item.retired
        ]
        if not active:
            raise HTTPException(400, "没有已启动的桌面浏览器窗口；请先启动店铺浏览器")
        previous = {item.shop["id"]: item.control_until for item in active}
        for item in active:
            item.control_until = max(item.control_until, time.time() + 45)

        async def run():
            await asyncio.gather(*(business.cancel_shop(item.shop["id"]) for item in active))
            async with AsyncExitStack() as stack:
                for item in sorted(active, key=lambda x: x.shop["id"]):
                    await stack.enter_async_context(item.lock)
                current = [item for item in active if not item.retired and item.state != "stopped"]
                records = [{"id": item.shop["id"], "port": item.port, "profile": str(item.profile)} for item in current]
                result = await asyncio.to_thread(native_windows, records, action)
                by_id = {item.shop["id"]: item for item in current}
                for row in result["results"]:
                    if row.get("windows") and "visible" in row:
                        item = by_id[row["shop_id"]]
                        item.visible = row["visible"]
                        item.remember_visibility(action != "hide")
                    if row["ok"]:
                        item = by_id[row["shop_id"]]
                        item.visible = action != "hide"
                        item.remember_visibility(item.visible)
                    else:
                        store.log_event("warning", "shops", "window_action_failed", row["error"], row["shop_id"])
                count = sum(row["ok"] for row in result["results"])
                return {**result, "ok": count == len(records), "count": count, "total": len(records), "action": action}

        try:
            return await asyncio.wait_for(run(), timeout=40)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        except Exception:
            raise HTTPException(503, "批量窗口操作未完成，请检查浏览器后重试") from None
        finally:
            for item in active:
                item.control_until = previous[item.shop["id"]]


@app.post("/api/shops/windows", dependencies=[Depends(authorize)])
async def all_windows(value: WindowBatchInput):
    return await operate_all_windows(value.action)


@app.post("/api/shops/tile", dependencies=[Depends(authorize)])
async def tile():
    return await operate_all_windows("tile")
