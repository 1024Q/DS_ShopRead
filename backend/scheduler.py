from . import recovery_log
from .async_database import run as database_work, read as database_read
from .platform_support import supports
import asyncio
import json
import time
from . import collection, reconciliation, tax_info, transactions, goods_data, promotions
from . import collection_plan
from . import update_gate


class Scheduler:
    def __init__(self, service):
        self.service = service
        self.last_keepalive = {}
        self.last_tick = None
        self.due_after = {}
        self.candidates = []
        self.demand_waits = {}
        self.started = time.time()
        self.maintenance = {}
        self.maintenance_gate = asyncio.Semaphore(2)
        self.pending = []
        self.refill_lock = asyncio.Lock()
        self.waiting = {}
        self.scan_waiting = None
        self.scan_scopes = None

    def scopes(self):
        from .business import KINDS, options_for

        for kind in sorted(KINDS):
            if kind == "orders":
                from .order_plan import live

                for options in live():
                    yield kind, options
                continue
            options = (
                promotions.options_for()
                if kind in promotions.KINDS
                else transactions.options_for("realtime")
                if kind == "transactions"
                else goods_data.options_for("realtime")
                if kind == "goodsdata"
                else tax_info.options_for()
                if kind == "tax"
                else reconciliation.options_for(kind)
                if kind in reconciliation.KINDS
                else options_for(kind)
            )
            yield kind, options

    async def tick(self):
        if update_gate.active():
            return
        from .collection_recovery import watchdog

        await watchdog(self.service)
        service = self.service
        config = await database_work(collection.settings, service.store)
        service.configure_queue(config)
        self.last_tick = time.time()
        service.runtime_limits = (
            {}
            if config.get("frequency_version")
            else service.load_controller.update(
                config, service.completions, sum(m.get("phase") == "running" for m in service.progress.values())
            )
        )
        service.wake.set()
        self.candidates = []
        self.scan_waiting = {}
        self.scan_scopes = list(self.scopes())

        def priorities():
            with service.store.connect() as db:
                return {
                    r["shop_id"]: r["until_time"]
                    for r in db.execute(
                        "SELECT shop_id,until_time FROM shop_priority WHERE until_time>?", (time.time(),)
                    )
                }

        service.shop_priorities = await database_read(priorities)
        await self.inspect_shops(await database_read(service.store.list), config)

        pending = {(s["id"], service.key(k, o, s)) for s, _, k, o, _ in self.candidates}
        self.demand_waits = {key: value for key, value in self.demand_waits.items() if key in pending}
        # Publish a complete discovery snapshot. The feeder keeps replenishing
        # from the previous snapshot while the next shop scan is in progress.
        async with self.refill_lock:
            self.pending = list(self.candidates)
            self.waiting = self.scan_waiting
            self.scan_waiting = None
            self.scan_scopes = None
        service.schedule_wake.set()

    async def refill(self):
        service = self.service
        async with self.refill_lock:
            if update_gate.active():return
            # Discovery only describes durable plans; it never allocates slots.
            previous = service.deferred
            service.due_plans.observe(self.waiting)
            self.waiting = {}
            candidates = [c for c in self.pending if not c[1].retired
                          and (c[0]['id'], service.key(c[2], c[3], c[0])) not in service.jobs]
            candidates = service.demands.coalesce(candidates, time.time())
            service.round_candidates = {(s['id'], service.key(k,o,s)) for s,_,k,o,_ in candidates}
            for offset in range(0, len(candidates), 16):
                prepared = await database_work(service.prepare_scheduled, candidates[offset:offset+16], service.queue_config)
                if update_gate.active():return
                with service.due_plans.registration_batch():
                    for shop,item,kind,plan,state in prepared:
                        service.admit(shop,item,kind,plan,True,bool(state.get('resolution_due')),False,
                                      **({'urgent':True} if state.get('urgent_due') else {}))
                        key = shop['id'], plan[1]
                        entry = service.due_plans.entries.get(key)
                        if entry and key not in service.jobs:
                            meta = entry[1]
                            meta['waiting_since'] = previous.get(key,{}).get('waiting_since', state.get('waiting_since',meta['queued_at']))
            self.pending = []
            service.due_plans.request_dispatch()

    async def feed(self):
        while True:
            self.service.schedule_wake.clear()
            try:
                await self.refill()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                recovery_log.suppressed("队列补位调度异常", warning=True)
                try:
                    await database_work(
                        self.service.store.log_event,
                        "error",
                        "system",
                        "scheduler_refill_error",
                        f"队列补位暂未完成（{type(error).__name__}），保留需求并自动重试",
                    )
                except Exception:
                    recovery_log.suppressed("scheduler.py:118 恢复步骤未完成", warning=True)
            try:
                await asyncio.wait_for(self.service.schedule_wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def inspect_shops(self, shops, config):
        # Independent shop databases may be inspected concurrently. Keep this
        # bounded; collection itself still uses the existing admission limits.
        gate = asyncio.Semaphore(4)

        async def inspect_shop(shop):
            async with gate:
                try:
                    await self.tick_shop(shop, config)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    recovery_log.suppressed("店铺调度异常", warning=True)
                    await database_work(
                        self.service.store.log_event,
                        "error",
                        "system",
                        "scheduler_shop_error",
                        f"店铺调度异常（{type(error).__name__}），下个周期重试；其他店铺继续",
                        shop["id"],
                    )

        async with asyncio.TaskGroup() as group:
            for shop in shops:
                group.create_task(inspect_shop(shop))

    async def tick_shop(self, shop, config):
        if update_gate.active():
            return
        service = self.service
        waiting = self.scan_waiting if self.scan_waiting is not None else self.waiting
        item = service.session_getter(shop["id"])
        self.last_keepalive.setdefault(shop["id"], time.time())
        if item.retired or item.state == "stopped":
            return
        if config["enabled"] and item.state in ("logged_in", "sleeping"):
            from .attention import scopes as attention_scopes
            from .focus_resolution import scopes as resolution_scopes

            followups = (
                await database_work(lambda: list(resolution_scopes(service.store, shop["id"])))
                if shop.get("platform", "pdd") == "pdd"
                else []
            )
            resolution_keys = {service.key(k, o, shop) for k, o in followups}
            scopes = (
                list(attention_scopes(shop))
                + followups
                + (self.scan_scopes if self.scan_scopes is not None else list(self.scopes()))
            )
            if shop.get("platform") in ("taobao", "jd", "xiaohongshu"):
                from datetime import date

                current = tax_info.options_for()["date_from"]
                today = date.fromisoformat(current)
                quarter = (today.month - 1) // 3
                period = f"{today.year if quarter else today.year - 1}-Q{quarter or 4}"
                scopes = [(k, tax_info.options_for(period) if k == "tax" else o) for k, o in scopes]
            if supports(shop.get("platform", "pdd"), "goodsdata"):
                scopes.append(("goodsdata", goods_data.options_for("yesterday")))
            if supports(shop.get("platform", "pdd"), "transactions"):
                scopes.append(("transactions", transactions.options_for("yesterday")))
                if shop.get("platform", "pdd") == "pdd":
                    # Days missed while offline: the 30-day report's daily trend fills them once per day.
                    from datetime import datetime
                    from .business_reader import BEIJING
                    from .payment_daily import missing_days

                    def gaps():
                        with service.store.connect() as db:
                            return missing_days(db, shop["id"], datetime.now(BEIJING).date())

                    if await database_read(gaps):
                        scopes.append(("transactions", transactions.options_for("month")))
            if supports(shop.get("platform", "pdd"), "ad_reports"):
                # Warm historical report ranges once daily, independently of UI visits.
                ranges = ("yesterday", "week", "month")
                scopes.extend(("ad_reports", promotions.options_for(span)) for span in ranges)
            if shop.get("platform", "pdd") == "pdd":
                from .business import options_for

                scopes.extend(
                    [
                        ("aftersales", options_for("aftersales", status="待商家处理")),
                        ("aftersales", options_for("aftersales", status="待处理即将逾期")),
                        ("tickets", options_for("tickets", status="待处理")),
                    ]
                )
            if shop.get("platform", "pdd") == "pdd":
                from .order_plan import background

                scopes.extend(("orders", o) for o in await database_work(lambda: list(background(service.store, shop))))
            entries = [
                (
                    k,
                    o,
                    "重点检查"
                    if o.get("_attention")
                    else "固定订阅"
                    if o.get("status") or o.get("trade_range", "realtime") != "realtime"
                    else "常规采集",
                )
                for k, o in scopes
            ]
            fixed = {service.key(k, o, shop) for k, o in scopes}
            from .refund_priority import pending as pending_refunds

            refund_cases = await database_work(pending_refunds, service.store, shop["id"]) if shop.get("platform", "pdd") == "pdd" else []
            urgent_refund = bool(refund_cases) and not getattr(service.store, 'direct_refunds', None)
            from .refund_priority import needs_run

            refund_due = urgent_refund and await database_work(needs_run, service.store, shop["id"], refund_cases)
            from .task_lifecycle import reconcile

            active = {
                key
                for (sid, key), m in service.progress.items()
                if sid == shop["id"] and (m.get("phase") == "running" or m.get("force"))
            }
            retained = await database_work(reconcile, service.store, shop, fixed, service.key, active)
            from .list_completion import pending as completion_pending

            recent = [
                r
                for r in retained
                if completion_pending(r)
                or r.get("continuation")
                or r.get("failures")
                or (
                    not r.get("last_success")
                    and (
                        r.get("demand_until", 0) > time.time()
                        or json.loads(r["options"]).get("_order_lane") == "history"
                    )
                )
            ]
            # UI queries are one-shot demands, never persistent subscriptions.
            for r in recent:
                options = json.loads(r["options"])
                if options.get("_attention") and (r["kind"], options) not in scopes:
                    continue
                if r["kind"] == "orders":
                    from .order_plan import view

                    if view(options):
                        continue
                if not completion_pending(r) and collection_plan.obsolete_realtime(r["kind"], options):
                    continue
                if r["cache_key"] != service.key(r["kind"], options, shop):
                    continue
                entries.append((r["kind"], options, "查询需求"))
            scopes = await database_work(service.demands.replace, shop, entries)
            eligible = {service.key(k, o, shop) for k, o in scopes}

            def update_states():
                with service.store.shop_db(shop["id"], write=True) as db:
                    states = {
                        r["cache_key"]: dict(r)
                        for r in db.execute("SELECT * FROM collection_tasks WHERE shop_id=?", (shop["id"],))
                    }
                    updates = []
                    for key, record in states.items():
                        old = record["schedule_state"]
                        state = "periodic" if key in fixed else "pending" if key in eligible else "archived"
                        if old != state:
                            updates.append((state, shop["id"], key))
                    if updates:
                        db.executemany(
                            "UPDATE collection_tasks SET schedule_state=? WHERE shop_id=? AND cache_key=?", updates
                        )
                return states

            states = await database_work(update_states)
            from .list_completion import publish_live_statuses

            try:
                await database_work(publish_live_statuses, service, shop, states)
            except Exception:
                import logging

                logging.getLogger(__name__).warning("一次性补齐进度暂未上传，采集与断点继续保留", exc_info=True)
            # Rebuild stable parents from their durable step ledger on every scan.
            from .master_tasks import remember

            for old in list(getattr(service, "master_steps", {})):
                if old[0] == shop["id"] and old[1] not in eligible and old not in service.jobs:
                    service.master_steps.pop(old, None)
            for kind, options in scopes:
                if (
                    supports(shop.get("platform", "pdd"), kind)
                    and collection.mode(kind, options, config, shop.get("platform", "pdd")) != "off"
                ):
                    step_key = service.key(kind, options, shop)
                    remember(
                        service,
                        shop["id"],
                        step_key,
                        kind,
                        options,
                        {**states.get(step_key, {}), "platform": shop.get("platform", "pdd")},
                    )
            if time.time() - self.started >= 30:
                from .collection_recovery import release_orphans

                active_keys = {key for (sid, key), job in service.jobs.items() if sid == shop["id"] and not job.done()}
                repaired = await database_work(release_orphans, service.store, shop["id"], active_keys, time.time())
                if repaired:
                    for key, state in states.items():
                        if key not in active_keys and state.get("last_attempt", 0) < time.time() - 30:
                            state["lease_until"] = 0
                    await database_work(
                        service.store.log_event,
                        "warning",
                        "system",
                        "orphan_collection_recovered",
                        f"自动释放 {repaired} 项已无运行任务的采集占位，恢复正常调度",
                        shop["id"],
                    )
            for key, meta in list(service.progress.items()):
                if (
                    key[0] == shop["id"]
                    and meta["phase"] == "queued"
                    and key[1] not in eligible
                    and not meta.get("force")
                ):
                    job = service.jobs.get(key)
                    if job is not None:
                        job.cancel()
            # Retire only obsolete, non-manual plans; retained failure/continuation
            # scopes are already part of eligible and keep their checkpoints.
            for key, (_, meta) in list(getattr(getattr(service, 'due_plans', None), 'entries', {}).items()):
                if key[0] == shop['id'] and key[1] not in eligible and not meta.get('force'):
                    service.due_plans.entries.pop(key, None)
                    service.deferred.pop(key, None)
            seen = set()
            for kind, options in scopes:
                if not supports(shop.get("platform", "pdd"), kind):
                    continue
                key = service.key(kind, options, shop)
                if key in seen or collection.mode(kind, options, config, shop.get("platform", "pdd")) == "off":
                    continue
                seen.add(key)
                task = states.get(key, {})
                from .queue_control import held_until

                held = held_until(service, (shop["id"], key))
                if held:
                    waiting[(shop["id"], key)] = {
                        **task, "platform": shop.get("platform", "pdd"),
                        "kind": kind,
                        "scope": options,
                        "stage": "已清空排队，等待下次执行",
                        "next_run_at": held,
                    }
                    continue
                if (shop["id"], key) in service.jobs:
                    queued = service.progress.get((shop["id"], key), {})
                    if queued.get("phase") == "queued":
                        queued["urgent_refund"] = bool(urgent_refund and kind == "aftersales" and options.get("_attention"))
                        queued["urgent_due"] = bool(refund_due and queued["urgent_refund"])
                        if queued["urgent_due"]:
                            queued.pop("shared_result_ready", None)
                    from .demand_refresh import refresh

                    refresh(service.progress.get((shop["id"], key), {}), options, time.time(), config)
                    continue
                if task.get("error_kind") == "login" and item.state == "logged_in":

                    def resume_login():
                        with service.store.write_connection() as db:
                            db.execute(
                                "UPDATE collection_tasks SET blocked=0,retry_at=0,error_kind='' WHERE shop_id=? AND cache_key=?",
                                (shop["id"], key),
                            )

                    await database_work(resume_login)
                    task = {**task, "blocked": 0, "retry_at": 0}
                if (
                    task.get("blocked")
                    or task.get("retry_at", 0) > time.time()
                    or task.get("lease_until", 0) > time.time()
                ):
                    waiting[(shop["id"], key)] = {
                        **task, "platform": shop.get("platform", "pdd"),
                        "kind": kind,
                        "scope": options,
                        "stage": "需要人工处理"
                        if task.get("blocked")
                        else "重试冷却"
                        if task.get("retry_at", 0) > time.time()
                        else "等待已有任务释放",
                        "retry_at": task.get("retry_at", 0) if not task.get("blocked") else 0,
                        "reason": task.get("last_error", ""),
                    }
                    continue
                cadence_due = not task.get("last_success") or collection.due(
                    kind,
                    options,
                    task["last_success"],
                    config,
                    platform=shop.get("platform", "pdd"),
                    adaptive_seconds=task.get("adaptive_seconds", 0),
                )
                resolution_due = key in resolution_keys
                urgent_due = bool(refund_due and kind == "aftersales" and options.get("_attention"))
                if not urgent_due and not resolution_due and not cadence_due and not task.get("failures") and not task.get("continuation"):
                    from .frequency_policy import next_run

                    if config.get("frequency_version"):
                        waiting[(shop["id"], key)] = {
                        **task, "platform": shop.get("platform", "pdd"),
                            "kind": kind,
                            "scope": options,
                            "stage": "等待下次执行",
                            "next_run_at": next_run(
                                kind, options, config, shop.get("platform", "pdd"), task["last_success"], time.time()
                            ),
                        }
                    continue
                if (
                    not resolution_due
                    and not options.get("_attention")
                    and key in fixed
                    and not task.get("failures")
                    and not task.get("continuation")
                    and cadence_due
                ):
                    # Spread initial admission; completion times then naturally spread
                    # recurring realtime work without adding another minute per cycle.
                    generation = (
                        task.get("last_success", 0)
                        if collection.mode(kind, options, config, shop.get("platform", "pdd")) == "daily"
                        else 0
                    )
                    bucket = self.due_after.setdefault(shop["id"], {})
                    token = (key, generation)
                    for old in list(bucket):
                        if old[0] == key and old != token:
                            bucket.pop(old, None)
                    deadline = bucket.setdefault(
                        token,
                        time.time() + collection_plan.stagger(shop["id"], kind, config.get("stagger_seconds", 60)),
                    )
                    if time.time() < deadline:
                        continue
                since = self.demand_waits.setdefault((shop["id"], key), time.time())
                self.candidates.append(
                    (
                        shop,
                        item,
                        kind,
                        options,
                        {
                            **task,
                            "waiting_since": since,
                            "resolution_due": resolution_due,
                            "urgent_refund": bool(urgent_refund and kind == "aftersales" and options.get("_attention")),
                            "urgent_due": urgent_due,
                            "priority_until": service.shop_priorities.get(shop["id"], 0),
                        },
                    )
                )
            self.due_after[shop["id"]] = {
                k: v
                for k, v in self.due_after.get(shop["id"], {}).items()
                if k[0] in eligible and time.time() - v < 86400
            }
        if (
            not update_gate.active()
            and config["keepalive_enabled"]
            and not item.lock.locked()
            and not item.visible
            and getattr(item, "control_until", 0) < time.time()
            and time.time() - self.last_keepalive[shop["id"]] >= config["keepalive_seconds"]
        ):
            if item.state not in ("logged_in", "logging_in"):
                return
            if shop["id"] in self.maintenance:
                return
            self.last_keepalive[shop["id"]] = time.time()
            task = asyncio.create_task(self.maintain(shop, item))
            self.maintenance[shop["id"]] = task
            task.add_done_callback(lambda done: self.maintenance.pop(shop["id"], None))

    async def maintain(self, shop, item):
        service = self.service
        async with self.maintenance_gate:
            if (
                update_gate.active()
                or item.retired
                or item.visible
                or item.lock.locked()
                or getattr(item, "control_until", 0) > time.time()
            ):
                return
            try:
                async with item.lock:
                    if item.state == "logged_in":
                        await item.command("Page.reload", {"ignoreCache": False})
                        await asyncio.sleep(1)
                        await item.refresh()
                    if item.state == "logging_in":
                        await item.recover_home()
                await database_work(
                    service.store.log_event, "info", "login", "keepalive", "定时登录维护完成", shop["id"]
                )
            except Exception:
                try:
                    await database_work(
                        service.store.log_event,
                        "warning",
                        "login",
                        "keepalive_failed",
                        "登录维护暂未完成，将在下个周期检查",
                        shop["id"],
                    )
                except Exception:
                    recovery_log.suppressed("scheduler.py:293 恢复步骤未完成", warning=True)

    async def run(self):
        feeder = asyncio.create_task(self.feed())
        try:
            await self.loop()
        finally:
            tasks = [feeder, *self.maintenance.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                recovery_log.suppressed("后台调度异常", warning=True)
                try:
                    await database_work(
                        self.service.store.log_event,
                        "error",
                        "system",
                        "scheduler_error",
                        f"后台调度异常（{type(error).__name__}），下个周期重试",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    recovery_log.suppressed("scheduler.py:310 恢复步骤未完成", warning=True)
            await asyncio.sleep(10)
