"""Recover wanted browsers without reviving deliberately stopped/migrated shops."""

import logging
import asyncio
import time
from . import update_gate


async def report_result(item, message):
    item.collection_recovery_result = message
    item.collection_recovery_checked_at = time.time()
    if getattr(item, "store", None) is not None:
        from .async_database import run

        await run(item.store.log_event, "info", "login", "collection_health", message, item.shop["id"])


def pending_reason(item):
    if getattr(item, "sleeping", False):
        return "店铺处于采集休眠，等待下一轮名额唤醒"
    if item.retired or not getattr(item, "wanted_running", False):
        return "店铺已停止或迁移，不自动启动"
    if time.monotonic() < getattr(item, "collection_recovery_after", 0):
        return "健康检查冷却中，按任务重试计划继续"
    return "本次未启动健康检查，等待后续检查"


async def health(item):
    """Read-only probe: a failed hidden target does not imply a broken shop."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{item.port}/json/version")
            response.raise_for_status()
    except httpx.ConnectError:
        return "disconnected"
    except Exception:
        return "unknown"
    try:
        result = await asyncio.wait_for(
            item.evaluate(
                "({login:location.pathname.includes('login'),ready:!!document.querySelector('.mms-header__user-info')})"
            ),
            4,
        )
        if result and result.get("ready"):
            return "healthy"
        if result and result.get("login"):
            return "login"
    except Exception:
        logging.getLogger(__name__).debug("浏览器健康检查异常，按未知状态处理", exc_info=True)
    return "unknown"


def request(item, *wakes, repair_page=False):
    """Coalesce collection failures without holding a collection worker slot."""
    task = getattr(item, "collection_recovery_task", None)
    if task is not None and not task.done():
        return task
    if (
        getattr(item, "sleeping", False)
        or item.retired
        or not getattr(item, "wanted_running", False)
        or time.monotonic() < getattr(item, "collection_recovery_after", 0)
    ):
        return None
    item.collection_recovery_after = time.monotonic() + 60

    async def recover():
        checked = time.time()
        try:
            if update_gate.active() or getattr(item, "control_until", 0) > time.time():
                await report_result(item, "更新或人工操作中，健康检查延后；尚未确认恢复")
                return
            item.collection_recovery_stage = "检查浏览器与登录健康"
            item.collection_recovery_started = time.time()
            status = await health(item)
            if status == "healthy":
                # The failing page is already closed by its context manager.
                # Preserve other modules and their active pages.
                # A healthy home page is not proof that a timed-out merchant
                # endpoint recovered. Keep task backoff instead of retry storms.
                await report_result(item, "浏览器与登录正常；失败任务按原重试计划继续，未重启店铺")
                return
            if status == "unknown":
                await report_result(item, "暂无法确认浏览器健康，未重启店铺；保留断点与重试退避")
                return
            item.collection_recovery_exclusive = True
            item.collection_recovery_stage = "等待当前采集批次释放浏览器"
            if status == "login":
                async with asyncio.timeout(135):
                    async with item.lock:
                        if item.retired or not item.wanted_running or getattr(item, "control_until", 0) > time.time():
                            return
                        from .read_page_pool import close_idle

                        await close_idle(item)
                        item.collection_recovery_stage = "恢复店铺登录"
                        if not await asyncio.wait_for(item.recover_home(), 45):
                            await report_result(item, "登录尚未恢复；如出现验证码或扫码请人工处理")
                            return
            else:
                # Existing readers finish normally. New readers are kept out
                # by collection_recovery_task, without reserving worker slots.
                async with asyncio.timeout(90):
                    while item.lock.locked():
                        await asyncio.sleep(0.2)
                item.collection_recovery_stage = "重新连接或启动浏览器"
                if not await check(item):
                    await report_result(item, "浏览器重连尚未成功，按恢复退避继续检查")
                    return
            await restored(item, checked)
            await report_result(item, "已完成浏览器或登录恢复，保留断点继续调度")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The persisted task error/backoff remains; the browser monitor can
            # subsequently reconnect. Never spawn another recovery per module.
            item.collection_recovery_result = f"健康检查未完成（{type(error).__name__}），未确认恢复；保留重试计划"
            try:
                await report_result(item, item.collection_recovery_result)
            except Exception:
                logging.getLogger(__name__).debug("恢复结果回报失败", exc_info=True)
        finally:
            item.collection_recovery_exclusive = False
            item.collection_recovery_stage = ""
            for wake in wakes:
                wake.set()

    task = asyncio.create_task(recover())
    item.collection_recovery_task = task
    return task


def wanted(store, shop_id):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"browser_wanted:{shop_id}",)).fetchone()
    return None if row is None else row[0] == "1"


def remember(item, running):
    item.wanted_running = running
    with item.store.connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (f"browser_wanted:{item.shop['id']}", "1" if running else "0"),
        )


async def restored(item, checked_at):
    if item.retired or item.state != "logged_in":
        return
    from .async_database import run as database_work
    from .collection_recovery import browser_restored

    checked_at = max(checked_at, getattr(item, "pending_collection_recovery_at", 0))
    item.pending_collection_recovery_at = checked_at
    try:
        count = await database_work(browser_restored, item.store, item.shop["id"], checked_at)
    except Exception as error:
        # An unavailable database does not mean the now-healthy browser needs restarting.
        import logging

        logging.getLogger(__name__).warning("浏览器已恢复，采集重试状态稍后写入：%s", type(error).__name__)
        return
    item.pending_collection_recovery_at = 0
    if count:
        await database_work(
            item.store.log_event,
            "info",
            "login",
            "collection_resumed",
            f"已确认浏览器与登录恢复，{count} 项连接或登录失败任务恢复调度；分页断点保留",
            item.shop["id"],
        )


async def check(item):
    if getattr(item, "sleeping", False):
        return
    recovery = getattr(item, "collection_recovery_task", None)
    if recovery is not None and not recovery.done() and recovery is not asyncio.current_task():
        return
    if (
        item.retired
        or not item.wanted_running
        or update_gate.active()
        or item.lock.locked()
        or getattr(item, "control_until", 0) > time.time()
        or getattr(item, "remote_control_active", False)
    ):
        return
    if time.monotonic() < getattr(item, "restart_after", 0):
        return
    checked_at = time.time()
    try:
        if item.state != "stopped":
            async with item.lock:
                try:
                    await asyncio.wait_for(item.refresh(), 20)
                except Exception:
                    # A stale page socket is not proof that Chrome exited.
                    await item.close_cdp()
                    await asyncio.wait_for(item.target(), 8)
                    await asyncio.wait_for(item.recover_home(), 45)
                    await restored(item, checked_at)
                else:
                    if (
                        item.state == "logging_in"
                        and not item.auto_login_attempted
                        and time.monotonic() - item.last_recovery > 60
                    ):
                        await asyncio.wait_for(item.recover_home(), 45)
            item.restart_failures = 0
            if item.state == "logged_in":
                item.error = ""
                if getattr(item, "pending_collection_recovery_at", 0):
                    await restored(item, item.pending_collection_recovery_at)
            return item.state == "logged_in"
    except Exception:
        # start() first reuses the existing browser, launching only when needed.
        pass
    if item.retired or not item.wanted_running or update_gate.active():
        return
    item.restart_failures = getattr(item, "restart_failures", 0) + 1
    delay = min(300, 30 * 2 ** min(item.restart_failures - 1, 4))
    item.restart_after = time.monotonic() + delay
    item.recovery_status = "浏览器异常，正在自动恢复店铺"
    await asyncio.to_thread(
        item.store.log_event, "warning", "login", "auto_restart", item.recovery_status, item.shop["id"]
    )
    try:
        await asyncio.wait_for(item.start(automatic=True), 90)
        item.error = "" if item.state == "logged_in" else item.error
        item.recovery_status = (
            "店铺已恢复，自动继续采集" if item.state == "logged_in" else "等待登录完成；验证码或扫码需人工处理"
        )
        if item.state == "logged_in":
            item.restart_failures = 0
            await restored(item, checked_at)
        return item.state == "logged_in"
    except Exception as error:
        item.error = f"自动恢复失败（{type(error).__name__}），{delay} 秒后重试；请检查浏览器安装及网络"
    finally:
        item.restart_after = time.monotonic() + delay
