"""Planned browser sleep preserves wanted-running intent and persisted profiles."""

import asyncio, time


def sleeping(store, sid):
    with store.connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (f"browser_sleeping:{sid}",)).fetchone()
    return bool(row and row[0] == "1")


def remember(item, value):
    item.sleeping = value
    with item.store.node_db(write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (f"browser_sleeping:{item.shop['id']}", "1" if value else "0"),
        )


async def _wake_once(item):
    started = time.monotonic()
    budget = asyncio.timeout(90)
    try:
        async with budget:
            await item.start(automatic=True)
    except BaseException as error:
        remember(item, True)
        item.state = "sleeping"
        if isinstance(error, TimeoutError):
            from .read_errors import PageReadError

            reason = "店铺休眠唤醒超过 90 秒" if budget.expired() else "店铺浏览器唤醒时页面响应超时"
            raise PageReadError(reason + "；保留断点，按计划重试") from error
        raise
    item.last_collection_activity = time.monotonic()
    item.collection_wake_seconds = min(60, max(1, time.monotonic() - started))
    if item.state != "logged_in":
        from .read_errors import LoginPending

        raise LoginPending("休眠店铺唤醒后等待登录验证；请处理验证码或扫码")


async def wake(item):
    task = getattr(item, "collection_wake_task", None)
    if not getattr(item, "sleeping", False) and (task is None or task.done()):
        return
    if item.retired or not item.wanted_running:
        return
    if task is None or task.done():
        item.recovery_status = "正在按采集名额唤醒店铺"
        # Use the existing browser recovery start limit, outside the read budget.
        task = item.collection_wake_task = asyncio.create_task(_wake_once(item))
        item.collection_wake_waiters = set()
    waiters = item.collection_wake_waiters
    waiter = asyncio.current_task()
    waiters.add(waiter)
    try:
        await asyncio.shield(task)
    finally:
        waiters.discard(waiter)
        # One cancelled substep must not abort its siblings' shared browser start.
        # Stop/pause still cancels the start once every interested step has left.
        if not waiters and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def manual_action(item, action):
    """Manual browser entry points wake first; hiding an asleep browser is a no-op."""
    if not getattr(item, "sleeping", False):
        return False
    if action == "hide":
        return True
    from .business_reader import PAGES

    if action in PAGES or action in ("show", "retry", "home", "maximize"):
        await item.start()
    return False


def near_task_hold(item, service, seconds, now):
    """Bound a warm-browser hold to the last real work, never a sliding timer."""
    from .collection import mode

    config = service.queue_config
    if not config.get("enabled", True) or config.get("home_paused") or not item.wanted_running:
        return False
    sid = item.shop["id"]
    wall = time.time()
    progress = service.progress
    grants = getattr(getattr(service, "slot_dispatcher", None), "grants", ())
    active = {key[0] for key, meta in progress.items() if meta.get("phase") == "running"} | {key[0] for key in grants}
    holds = getattr(service, "browser_sleep_holds", {})
    service.browser_sleep_holds = holds = {key: expiry for key, expiry in holds.items() if expiry > now}
    occupied = active | set(holds)
    if sid not in occupied and len(occupied) >= config.get("max_active_shops", 2):
        return False
    # A warm idle browser may never hold up a runnable peer's turn. Retrying or
    # login-blocked peers alone do not count as competing runnable work.
    if any(
        key[0] != sid
        and meta.get("phase") == "queued"
        and not any(meta.get(flag) for flag in ("blocked", "database_pressure", "browser_blocked", "clearing_queue"))
        and meta.get("retry_at", 0) <= wall
        for key, meta in progress.items()
    ) or any(key[0] != sid and key[0] not in active for key in getattr(service, "round_candidates", ())):
        return False
    waits = getattr(service, "deferred", {})
    if any(
        key[0] != sid and meta.get("next_run_at", wall + 1) <= wall and meta.get("stage") == "等待下次执行"
        for key, meta in waits.items()
    ):
        return False
    upcoming = [
        meta["next_run_at"]
        for key, meta in waits.items()
        if key[0] == sid
        and meta.get("stage") == "等待下次执行"
        and isinstance(meta.get("next_run_at"), (float, int))
        and meta["next_run_at"] >= wall - 5
        and mode(meta.get("kind"), meta.get("scope", {}), config, item.shop.get("platform", "pdd")) != "off"
    ]
    if not upcoming:
        return False
    estimate = min(60, max(1, getattr(item, "collection_wake_seconds", 20)))
    horizon = min(90, seconds + estimate)
    deadline = item.last_collection_activity + seconds + horizon
    wait = max(0, min(upcoming) - wall)
    if now >= deadline or wait > horizon or now + wait > deadline:
        return False
    item.sleep_hold_activity = item.last_collection_activity
    holds[sid] = deadline
    item.recovery_status = f"即将采集：{int(wait)} 秒后到期，暂缓休眠（最多再保持 {int(deadline - now)} 秒）"
    return True


async def maintain(item, service):
    seconds = service.queue_config.get("browser_idle_seconds", 30)
    getattr(service, "browser_sleep_holds", {}).pop(item.shop["id"], None)
    if str(getattr(item, "recovery_status", "")).startswith("即将采集："):
        item.recovery_status = ""
    if not seconds or item.retired or not item.wanted_running or getattr(item, "sleeping", False):
        return
    if (
        item.state != "logged_in"
        or getattr(item, 'direct_refund_active', False)
        or item.lock.locked()
        or getattr(item, "remote_control_active", False)
        or getattr(item, "control_until", 0) > time.time()
    ):
        return
    sid = item.shop["id"]
    dispatcher = getattr(service, "slot_dispatcher", None)
    if dispatcher and any(k[0] == sid for k in dispatcher.grants):
        return
    if any(k[0] == sid and m.get("phase") == "running" for k, m in service.progress.items()):
        return
    now = time.monotonic()
    if not hasattr(item, "last_collection_activity"):
        item.last_collection_activity = now
        return
    if now - item.last_collection_activity < seconds:
        return
    if near_task_hold(item, service, seconds, now):
        return
    async with item.lock:
        if getattr(item, "remote_control_active", False) or getattr(item, "control_until", 0) > time.time():
            return
        if dispatcher and any(k[0] == sid for k in dispatcher.grants):
            return
        if any(k[0] == sid and m.get("phase") == "running" for k, m in service.progress.items()):
            return
        item.sleeping = True  # Recovery must not race Browser.close.
        try:
            await item.stop(sleep=True)
            item.recovery_status = "采集休眠，等待下一轮名额唤醒"
        except BaseException:
            item.sleeping = False
            item.last_collection_activity = time.monotonic()
            raise
