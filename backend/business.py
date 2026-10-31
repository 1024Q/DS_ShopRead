from . import recovery_log
from .task_timing import TaskMeta
from .async_database import record_state
from .filter_scope import matches, values
from .async_database import run as database_work
from .shop_identity import identity
from .platform_support import supports
import asyncio
import hashlib
import json
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import APIRouter, Depends, HTTPException, Query
from .business_reader import BEIJING, TICKET_STATUS, collect
from .operations import KINDS as OPERATION_KINDS, STATUS as OPERATION_STATUS, decorate
from . import reconciliation, tax_info, transactions, goods_data, promotions
from .read_errors import PageReadError, LoginPending
from . import product_images, collection, attention
from . import collection_plan
from . import collection_checkpoint as checkpoints
from .collection_queue import queue_for
from . import update_gate

from .business_kinds import KINDS


def options_for(kind, date_from="", date_to="", status="", stars="", reply=""):
    if kind == "ad_logs":
        date_from = date_to = ""
    if kind == "tickets" and status == "即将逾期":
        status = "2小时内将逾期"
    today = datetime.now(BEIJING).date()
    default_days = (
        180
        if kind == "evidence" or kind == "tickets" and status == "待处理"
        else 90
        if kind in ("orders", "intercepts")
        else 3
        if kind in ("reviews", "packages")
        else 7
    )
    first = datetime.fromisoformat(date_from).date() if date_from else today - timedelta(days=default_days - 1)
    last = datetime.fromisoformat(date_to).date() if date_to else today
    if first > last or (last - first).days > (36525 if kind == "products" else 180):
        raise ValueError("商品创建日期范围无效" if kind == "products" else "日期范围必须有效且不超过 180 天")
    values = sorted({int(value) for value in stars.split(",") if value})
    if any(value not in range(1, 6) for value in values):
        raise ValueError("星级必须为 1 至 5")
    if reply not in ("", "replied", "unreplied"):
        raise ValueError("回复状态无效")
    if kind == "tickets" and status and status not in TICKET_STATUS.values():
        raise ValueError("工单状态无效")
    if kind == "orders":
        from .orders import STATUSES

        if status not in STATUSES:
            raise ValueError("订单状态无效")
        if first < today - timedelta(days=90):
            raise ValueError("订单查询仅支持近 3 个月，请调整日期")
    if kind == "products" and status not in ("", "在售中", "已下架", "已售罄"):
        raise ValueError("商品状态无效")
    if (
        kind in OPERATION_STATUS
        and kind not in ("aftersales", "packages", "invoices")
        and status
        and status not in OPERATION_STATUS[kind]
    ):
        raise ValueError("状态无效")
    return {
        "start": int(datetime.combine(first, datetime.min.time(), tzinfo=BEIJING).timestamp()),
        "end": int(datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=BEIJING).timestamp()) - 1,
        "date_from": first.isoformat(),
        "date_to": last.isoformat(),
        "status": status,
        "stars": values,
        "reply": reply,
        "explicit_dates": kind in ("aftersales", "invoices") and bool(date_from or date_to),
        **(
            {"_relative_days": default_days}
            if not date_from
            and not date_to
            and kind in ("reviews", "tickets", "packages", "evidence", "invoices", "aftersales", "intercepts")
            else {}
        ),
    }


def filter_rows(
    rows,
    kind,
    keyword="",
    status="",
    stars=(),
    reply="",
    date_from="",
    date_to="",
    min_price=None,
    max_price=None,
    low_stock=None,
    priority_only=False,
    carrier="",
    activity_type="",
    brand_reason="",
    activity_group="",
):
    matches = []
    for row in rows:
        if kind == "orders" and status == "即将逾期发货":
            from .orders import pending_shipping

            if not pending_shipping(row.get("status")):
                continue
        if kind == "aftersales" and status == "待处理即将逾期":
            from .aftersales_warning import pending_merchant

            if not pending_merchant(row.get("progress") or row.get("status")):
                continue
        if (
            keyword.casefold()
            not in " ".join(
                str(row.get(key, ""))
                for key in (
                    "operators",
                    "operator",
                    "operator_name",
                    "operation_module",
                    "id",
                    "name",
                    "code",
                    "goods_id",
                    "order_number",
                    "problem",
                    "content",
                    "reply",
                    "tracking_number",
                    "return_tracking_number",
                    "invoice_title",
                    "invoice_title_type",
                    "buyer_taxpayer_number",
                    "invoice_number",
                    "goods_spec",
                    "invoice_other_info",
                    "progress",
                    "remark",
                    "activity_name",
                    "activity_id",
                    "activity_type",
                )
            ).casefold()
        ):
            continue
        if kind == "orders" and status in ("待付款", "待发货", "待收货") and row.get("_order_status_stale"):
            continue
        if status and row.get("status") != status and row.get("attention_status") != status:
            continue
        if kind == "activities" and activity_type and row.get("activity_type") != activity_type:
            continue
        if kind == "brands" and brand_reason and brand_reason not in str(row.get("problem") or "").split("；"):
            continue
        if kind == "activities" and activity_group:
            from .activities import groups

            if activity_group not in row.get("activity_groups", groups(row)):
                continue
        if priority_only and not row.get("priority"):
            continue
        if carrier and carrier.casefold() not in row.get("carrier", "").casefold():
            continue
        if kind == "reviews":
            if stars and row.get("stars") not in stars:
                continue
            if reply and row.get("replied") != (reply == "replied"):
                continue
        if date_from and (not row.get("created_at") or row["created_at"][:10] < date_from):
            continue
        if date_to and (not row.get("created_at") or row["created_at"][:10] > date_to):
            continue
        if min_price is not None and (row.get("price_max") is None or row["price_max"] < min_price):
            continue
        if max_price is not None and (row.get("price_min") is None or row["price_min"] > max_price):
            continue
        if low_stock is not None and (row.get("stock") is None or row["stock"] > low_stock):
            continue
        matches.append(row)
    return matches


def image_catalog(store, shop_id, rows):
    # Only rows lacking a thumbnail or name consult other snapshots; enrich() ignores the catalog otherwise.
    missing = {
        str(r.get("goods_id"))
        for r in rows
        if r.get("goods_id") and (not product_images.thumbnail(r) or not r.get("name"))
    }
    return store.product_images(shop_id, missing) if missing else {}


def scheduling_states(store, pairs):
    """Publication time and ledger row for many (shop_id,cache_key) pairs in one read snapshot."""
    cached = {}
    tasks = {}
    pairs = list(dict.fromkeys(pairs))
    by_shop = {}
    for sid, key in pairs:
        by_shop.setdefault(sid, []).append(key)
    for sid, keys in by_shop.items():
        with store.connect() as db:
            db.execute("BEGIN")
            for offset in range(0, len(keys), 200):
                batch = keys[offset : offset + 200]
                marks = ",".join("?" for _ in batch)
                args = [sid, *batch]
                for table in ("daily_archive", "business_cache"):
                    for row in db.execute(
                        f"SELECT shop_id,cache_key,updated FROM {table} WHERE shop_id=? AND cache_key IN ({marks})",
                        args,
                    ):
                        cached[row["shop_id"], row["cache_key"]] = {"updated": row["updated"]}
                # Automatic homepage reads keep a separate summary snapshot;
                # its success must not look like a missing full-list cache.
                for row in db.execute(
                    f"SELECT shop_id,cache_key,updated FROM business_cache WHERE shop_id=? AND cache_key IN ({marks})",
                    [sid, *("summary:" + key for key in batch)],
                ):
                    pair = row["shop_id"], row["cache_key"][8:]
                    if row["updated"] > cached.get(pair, {}).get("updated", 0):
                        cached[pair] = {"updated": row["updated"]}
                for row in db.execute(
                    f"SELECT * FROM collection_tasks WHERE shop_id=? AND cache_key IN ({marks})", args
                ):
                    tasks[row["shop_id"], row["cache_key"]] = dict(row)
    return cached, tasks


class BusinessService:
    def __init__(self, store, session_getter):
        self.store, self.session_getter = store, session_getter
        self.jobs, self.errors = {}, {}
        self.waiting_login = set()
        self.queue_config = collection.settings(store) if store is not None else dict(collection.DEFAULTS)
        self.progress = {}
        self.master_steps = {}
        self.deferred = {}
        from .node_dispatch import NodeDispatch
        self.due_plans = NodeDispatch(self)
        self.slot_dispatcher = self.due_plans
        self.queue_caps = {}
        from .collection_demands import Demands

        self.demands = Demands(self)
        self.running_shops = set()
        self.wake = asyncio.Event()
        self.schedule_wake = asyncio.Event()
        self.focus_kind = None
        self.completions = []
        self.started_at = time.time()
        self.shop_priorities = {}
        from .machine_load import LoadController

        self.load_controller = LoadController()
        self.runtime_limits = {}

    def configure_queue(self, config):
        if config == self.queue_config:
            return
        self.queue_config = dict(config)
        # Finish active reads; discard paused queued reads without opening their browsers.
        for key, meta in list(self.progress.items()):
            from .home_only_pause import held

            paused = held(meta["kind"], meta.get("scope", {}), config, meta.get("platform", "pdd"))
            from .shop_rotation import home_paused

            home_stop = home_paused(meta["kind"], meta.get("scope", {}), config)
            if (
                home_stop
                or meta.get("phase") == "queued"
                and (
                    paused
                    or not meta.get("force")
                    and (
                        not config["enabled"]
                        or collection.mode(meta["kind"], meta.get("scope", {}), config, meta.get("platform", "pdd"))
                        == "off"
                    )
                )
            ):
                task = self.jobs.get(key)
                if task and not task.cancelling():
                    task.cancel()

                    def discard_cancelled(done, key=key, meta=meta):
                        if done.cancelled() and meta.get("phase") == "queued" and self.jobs.get(key) is done:
                            self.jobs.pop(key, None)
                            self.progress.pop(key, None)
                            if hasattr(self, "shop_rotation"):
                                self.shop_rotation.complete(key)
                            self.wake.set()
                            self.schedule_wake.set()

                    task.add_done_callback(discard_cancelled)
        self.wake.set()
        self.schedule_wake.set()
        self.due_plans.dispatch()

    @asynccontextmanager
    async def slot(self, job_key, item, kind):
        meta = self.progress.setdefault(
            job_key,
            TaskMeta(
                {
                    "kind": kind,
                    "queued_at": time.time(),
                    "phase": "queued",
                    "loaded": 0,
                    "total": None,
                    "foreground": True,
                }
            ),
        )
        from .operation_log import step

        if isinstance(meta, TaskMeta) and self.store is not None:
            from .task_catalog import title

            meta.on_stage = lambda stage: step(self.store, item.shop, title(kind, meta.get("scope", {})), stage)
        from .node_dispatch import current_execution
        dispatcher = self.due_plans
        lease = None
        acquired = False
        binding = None
        execution_token = None
        from .collection_metrics import Tracker, phase as measure_phase
        tracker = Tracker(self.store, {'id': job_key[0], **getattr(item, 'shop', {})}, job_key[1], kind,
                          meta.get('scope', {}), self.queue_config, meta)
        try:
            meta.setdefault('scope', {})
            with measure_phase('slot_wait'):
                binding = await dispatcher.enter(job_key, item, meta)
            from .task_timing import end_wait
            end_wait(meta)
            # Browser preparation completed before reservation. This read lease
            # acquisition is immediate on the owning event loop; an exclusive
            # operation cannot interleave between admission and acquisition.
            if hasattr(item.lock, 'read'):
                lease = await item.lock.read()
                item.collection_lease = lease
            else:
                await item.lock.acquire()
                acquired = True
            from .frequency_policy import group
            frequency = group(kind, meta.get('scope', {}), self.queue_config, meta.get('platform', 'pdd'))
            meta['timeout_seconds'] = frequency['timeout'] if frequency else 240
            meta['freshness_seconds'] = min(30, frequency['seconds']) if frequency else 30
            root = item.source if hasattr(item, 'source') else item
            root.read_page_idle_seconds = min(90, max(1, self.queue_config.get('browser_idle_seconds', 30) or 90))
            self.running_shops.add(job_key[0])
            meta.update(queue=queue_for(meta, self.queue_config), phase='running', started_at=time.time(), stage='准备页面')
            root.last_collection_activity = time.monotonic()
            execution_token = current_execution.set(binding)
            from .read_budget import ReadBudget

            async with ReadBudget(meta["timeout_seconds"], meta):
                meta["stage"] = "后台隐藏采集（调试日志）" if getattr(item, "debug_mode", False) else "后台静默采集"
                if (
                    not getattr(item, "remote_control_active", False)
                    and getattr(item, "manual_visibility", None) is not True
                ):
                    if hasattr(item, "quiet_window"):
                        await item.quiet_window(False)
                item.read_progress = lambda loaded, total, stage: meta.update(loaded=loaded, total=total, stage=stage)
                from .database_writer import operation as write_context

                token = write_context.set(f"{getattr(item, 'shop', {}).get('name', job_key[0])} / {kind}")
                from .shard_connection import shop_context

                shop_token = shop_context.set(job_key[0])
                try:
                    yield meta
                finally:
                    shop_context.reset(shop_token)
                    write_context.reset(token)
        finally:
            if lease:
                lease.release()
                item.collection_lease = None
            elif acquired:
                item.lock.release()
            if meta.get("phase") == "running":
                item.read_progress = None
                if meta.get("started_at"):
                    from .queue_eta import record as record_duration

                    record_duration(self, job_key[0], kind, meta.get("scope", {}), time.time() - meta["started_at"])
                if not any(
                    k != job_key and k[0] == job_key[0] and m.get("phase") == "running"
                    for k, m in self.progress.items()
                ):
                    self.running_shops.discard(job_key[0])
            if execution_token is not None:current_execution.reset(execution_token)
            if binding:
                binding.finish()
                if job_key not in self.jobs:
                    # Direct slot callers have no publication outside this context.
                    dispatcher.core.release(job_key, binding.token)
                    self.progress.pop(job_key, None)
            elif job_key not in self.jobs:
                self.progress.pop(job_key, None)
            setattr(item.source if hasattr(item, "source") else item, "last_collection_activity", time.monotonic())
            dispatcher.request_dispatch()
            import sys
            error = sys.exc_info()[1]
            from .task_timing import end_wait
            end_wait(meta)
            tracker.finish('partial' if isinstance(error, checkpoints.YieldCollection) else
                           'cancelled' if isinstance(error, asyncio.CancelledError) else
                           'failed' if error is not None else meta.get('result', 'skipped'))

    async def close(self):
        self.closing = True
        self.due_plans.entries.clear()
        await self.due_plans.close()
        if getattr(self, 'direct_refunds', None):
            await self.direct_refunds.close()
        from .queue_eta import save as save_durations

        save_durations(self, force=True)
        jobs = list(self.jobs.values())
        jobs += list(getattr(self, "recovery_tasks", set()))
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        self.jobs.clear()
        self.progress.clear()
        self.wake.set()
        if getattr(self, "slot_dispatcher", None):
            await self.slot_dispatcher.close()

    async def cancel_shop(self, shop_id):
        await self.due_plans.cancel_shop(shop_id)
        if getattr(self, 'direct_refunds', None):
            await self.direct_refunds.cancel_shop(shop_id)
        tasks = [task for (record_id, _), task in self.jobs.items() if record_id == shop_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key in [key for key in self.jobs if key[0] == shop_id]:
            self.jobs.pop(key, None)
            self.progress.pop(key, None)
        self.wake.set()

    def key(self, kind, options, record, legacy=False):
        if kind == "orders" and options.get("_status_list_snapshot"):
            from .list_completion import order_key

            return order_key(record)
        if kind == "orders" and record.get("platform", "pdd") == "pdd" and not legacy:
            from .order_plan import view, key

            if view(options):
                return key(record)
        options = collection_plan.canonical(kind, options, record.get("platform", "pdd"))
        options = (
            {k: v for k, v in options.items() if k != "_relative_days"}
            if legacy
            else collection_plan.key_scope(kind, options, record.get("platform", "pdd"))
        )
        scope = {} if kind in ("overview", "products", "navigator", "brands", "activities", "ad_logs") else options
        digest = hashlib.sha256(
            json.dumps(
                [
                    scope,
                    record.get("profile_version", 0),
                    "taobao-flow-v1"
                    if kind == "goodsdata" and record.get("platform") == "taobao"
                    else "goods-images-v2"
                    if kind == "goodsdata"
                    else "product-images-v3"
                    if kind in product_images.KINDS
                    else "details-v2"
                    if kind in reconciliation.KINDS
                    else "operations-v2"
                    if kind in OPERATION_KINDS
                    else "v1",
                ],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]
        return kind + ":" + digest

    def query_options(self, kind, options, record):
        if kind == "orders" and options.get("status") == "待发货" and not options.get("_list_completion"):
            from .list_completion import order_key, order_candidate
            from .table_index import metadata

            saved = metadata(self.store, record["id"], order_key(record))
            from .order_plan import key as view_key

            general = metadata(self.store, record["id"], view_key(record))
            if (
                saved
                and (not general or saved["updated"] >= general["updated"])
                and order_candidate(saved["payload"], options)
            ):
                return {**options, "_status_list_snapshot": True}
        canonical = collection_plan.canonical(kind, options, record.get("platform", "pdd"))
        if (
            kind == "invoices"
            and record.get("platform", "pdd") == "pdd"
            and options.get("status") == "待开票"
            and not options.get("explicit_dates")
            and not options.get("_invoice_details_refresh")
        ):
            from .table_index import metadata

            pending = {**options, "_invoice_details_refresh": True}
            latest = metadata(self.store, record["id"], self.key(kind, pending, record))
            original = metadata(self.store, record["id"], self.key(kind, canonical, record))
            if (
                latest
                and latest["payload"].get("complete")
                and (not original or latest["updated"] >= original["updated"])
            ):
                return pending
        if canonical != options:
            from .table_index import metadata

            cached = metadata(self.store, record["id"], self.key(kind, canonical, record))
            # A truncated all-values read cannot prove coverage of a narrow query.
            # Preserve the existing filtered adapter as a one-shot fallback.
            if cached and not cached["payload"].get("complete"):
                return {**options, "_filtered_read": True}
        return canonical

    def prepare_enqueue(self, record, kind, options, foreground):
        options = collection_plan.canonical(kind, options, record.get("platform", "pdd"))
        cache_key = self.key(kind, options, record)
        caches, tasks = scheduling_states(self.store, [(record["id"], cache_key)])
        cached = caches.get((record["id"], cache_key))
        if options.get("_attention"):
            state = tasks.get((record["id"], cache_key)) or {}
            cached = {"updated": state["last_success"]} if state.get("last_success") else None
        collection.register(self.store, record["id"], cache_key, kind, options, cached, requested=foreground)
        return (
            options,
            cache_key,
            cached,
            collection.settings(self.store),
            collection.state(self.store, record["id"], cache_key),
        )

    async def enqueue_async(self, record, item, kind, options, refresh=False, force=False, foreground=True):
        prepared = await database_work(self.prepare_enqueue, record, kind, options, foreground)
        return self.admit(record, item, kind, prepared, refresh, force, foreground)

    def prepare_scheduled(self, candidates, config):
        """One shop-scoped metadata batch; never hydrate business payloads."""
        plans = [
            (s, i, k, collection_plan.canonical(k, o, s.get("platform", "pdd")), t) for s, i, k, o, t in candidates
        ]
        pairs = [(s["id"], self.key(k, o, s)) for s, i, k, o, t in plans]
        caches, states = scheduling_states(self.store, pairs)
        prepared = []
        for (shop, item, kind, options, state), pair in zip(plans, pairs):
            task = states.get(pair)
            cached = caches.get(pair)
            if options.get("_attention"):
                cached = {"updated": task["last_success"]} if task and task.get("last_success") else None
            if task is None:
                collection.register(self.store, shop["id"], pair[1], kind, options, cached, requested=False)
                task = collection.state(self.store, shop["id"], pair[1])
            prepared.append((shop, item, kind, (options, pair[1], cached, config, task), state))
        return prepared

    def enqueue(self, record, item, kind, options, refresh=False, force=False, foreground=True):
        return self.admit(
            record, item, kind, self.prepare_enqueue(record, kind, options, foreground), refresh, force, foreground
        )

    def admit(self, record, item, kind, prepared, refresh, force, foreground, urgent=False):
        options, cache_key, cached, config, task = prepared
        from .home_only_pause import held

        if held(kind, options, config, record.get("platform", "pdd")):
            return cached
        from .task_catalog import removed_report

        if removed_report(kind, options):
            return cached
        if kind == "orders":
            from .order_plan import disabled_scope

            if disabled_scope(options):
                return cached
        job_key = (record["id"], cache_key)
        from .queue_control import held_until

        if not force and held_until(self, job_key):
            return cached
        self.configure_queue(config)
        urgent = bool(urgent and kind == "aftersales" and options.get("_attention") and record.get("platform", "pdd") == "pdd")
        if job_key in self.progress:
            from .demand_refresh import refresh as refresh_demand

            refresh_demand(self.progress[job_key], options, time.time(), config)
        resume = job_key in self.waiting_login
        due = (
            resume
            or force
            or (
                config["enabled"]
                and collection.mode(kind, options, config, record.get("platform", "pdd")) != "off"
                and (
                    urgent
                    or task.get("continuation")
                    or task["failures"] > 0
                    or not cached
                    or (
                        refresh
                        and collection.due(
                            kind,
                            options,
                            task["last_success"],
                            config,
                            platform=record.get("platform", "pdd"),
                            adaptive_seconds=task.get("adaptive_seconds", 0),
                        )
                    )
                )
            )
        )
        retry = resume or force or time.time() >= task["retry_at"]
        if (
            due
            and retry
            and (force or not task.get("blocked"))
            and getattr(item, "control_until", 0) < time.time()
            and item.state in ("logged_in", "sleeping")
            and not item.retired
            and job_key not in self.jobs
        ):
            meta = TaskMeta(
                {
                    "kind": kind,
                    "platform": record.get("platform", "pdd"),
                    "queued_at": time.time(),
                    "phase": "queued",
                    "loaded": 0,
                    "total": None,
                    "foreground": foreground,
                    "force": force,
                    "resume": resume,
                    "scope": options,
                    "failures": task["failures"],
                    "continuation": bool(task.get("continuation")),
                    "urgent_refund": urgent,
                    "urgent_due": urgent,
                }
            )
            meta["last_success"] = task.get("last_success", 0)
            if task.get("continuation"):
                meta["queue_turn_at"] = meta["queued_at"]
            meta.update(
                priority_until=self.shop_priorities.get(record["id"], 0),
                midnight_home=collection_plan.midnight_home(kind, options, task.get("last_success", 0)),
            )
            self.due_plans.add(job_key, item, meta)
            self.due_plans.dispatch()
            self.schedule_wake.set()
        if foreground and job_key in self.progress:
            self.progress[job_key]["foreground"] = True
        if force and job_key in self.progress:
            progress = self.progress[job_key]
            if progress["phase"] == "queued":
                progress["force"] = True
            elif not progress.get("force"):
                progress["repeat_force"] = True
        self.wake.set()
        return cached

    async def sync(self, item, kind, options, cache_key):
        from .shop_lock import CollectionSession

        item = CollectionSession(item)
        job_key = (item.shop["id"], cache_key)
        claimed = False
        outcome = None
        try:
            # Bound before admission: if the slot itself fails, the error handling
            # below still records the failure against this job's progress.
            progress = self.progress.get(job_key, {})
            async with self.slot(job_key, item, kind) as progress:
                # Read the coalesced queued scope only after admission. Retries
                # retain their original scope so pagination checkpoints survive.
                options = dict(progress.get("scope", options))
                if getattr(item, "control_until", 0) > time.time():
                    return

                def claim_task():
                    nonlocal claimed
                    if not collection.state(self.store, item.shop["id"], cache_key):
                        collection.register(self.store, item.shop["id"], cache_key, kind, options, None, False)
                    from .task_lifecycle import before_run

                    demand = self.demands.shops.get(item.shop["id"], {}).get(cache_key, {})
                    subscribed = bool(demand.get("sources", set()) - {"查询需求"})
                    if item.retired or not before_run(
                        self.store,
                        item.shop,
                        kind,
                        options,
                        cache_key,
                        self.key,
                        progress.get("force", False),
                        subscribed,
                        self.queue_config,
                        urgent=bool(progress.get("urgent_due") and progress.get("urgent_refund")),
                    ):
                        return False
                    claimed = collection.claim(
                        self.store,
                        item.shop["id"],
                        cache_key,
                        progress.get("force", False),
                        resume=progress.get("resume", False) or progress.get("deferred_force", False),
                    )
                    return claimed

                progress["stage"] = "核对采集需求与任务断点"
                claimed = await database_work(claim_task)
                if not claimed:
                    return
                from .list_completion import publish_status as publish_completion_status

                await database_work(publish_completion_status, self, item.shop, kind, options, cache_key)
                if item.retired:
                    if claimed:
                        await database_work(collection.finish, self.store, item.shop["id"], cache_key, cancelled=True)
                    return
                await database_work(
                    self.store.log_event,
                    "info",
                    kind,
                    "sync_start",
                    "开始读取" + (" · " + options["period"] if options.get("period") else ""),
                    item.shop["id"],
                )
                if item.state != "logged_in":
                    raise LoginPending("等待店铺完成重新登录或人工验证")
                self.waiting_login.discard(job_key)
                from .auto_refunds import prioritize as prioritize_refunds

                priority_checked = await prioritize_refunds(item, self.store, kind, options, progress)
                from .incremental_collection import read as collect_planned
                from .summary_publication import (
                    scheduled as summary_scheduled,
                    key as summary_key,
                    manual_details_pending,
                )

                summary_only = summary_scheduled(
                    kind, options, item.shop.get("platform", "pdd"), progress.get("force", False)
                )
                if summary_only and await database_work(manual_details_pending, self.store, item.shop["id"], cache_key):
                    summary_only = False
                checkpoint_key = summary_key(cache_key) if summary_only else cache_key
                progress["stage"] = "读取本地增量快照"
                cached = (
                    None if summary_only else await database_work(self.store.read_cache, item.shop["id"], cache_key)
                )
                if not cached and options.get("_relative_days") and not options.get("_attention"):
                    # Seed the new rolling identity from an exact legacy window, preserving its watermark.
                    for offset in (0, 1):
                        old = {
                            **options,
                            "start": options["start"] - offset * 86400,
                            "end": options["end"] - offset * 86400,
                        }
                        old.update(
                            date_from=datetime.fromtimestamp(old["start"], BEIJING).date().isoformat(),
                            date_to=datetime.fromtimestamp(old["end"], BEIJING).date().isoformat(),
                        )
                        cached = await database_work(
                            self.store.read_cache, item.shop["id"], self.key(kind, old, item.shop, legacy=True)
                        )
                        if cached and cached.get("payload", {}).get("complete"):
                            break
                        cached = None
                config = collection.settings(self.store)
                from .collection_priority import rank as priority_rank

                if (
                    getattr(self, "core_backlog", 0)
                    and priority_rank(kind, options, config, item.shop.get("platform", "pdd")) > 0
                ):
                    # Short cooperative batches while core data waits. Frequency
                    # and query scope stay unchanged; checkpoints retain every page.
                    config = {
                        **config,
                        "batch_pages": min(config["batch_pages"], 3),
                        "batch_seconds": min(config["batch_seconds"], 10),
                    }
                if options.get("_order_lane") in ("history", "daily"):
                    config = {**config, "batch_pages": 3, "batch_seconds": 6}
                if kind in ("goodsdata", "ad_products", "ad_reports") and options.get("trade_range") in (
                    "yesterday",
                    "week",
                    "month",
                    "quarter",
                ):
                    config = {
                        **config,
                        "batch_pages": min(config["batch_pages"], 3),
                        "batch_seconds": min(config["batch_seconds"], 8),
                    }
                if options.get("_attention"):
                    config = {**config, "batch_pages": 6, "batch_seconds": 8}

                async def planned_read():
                    progress["stage"] = "加载分页断点"
                    async with checkpoints.async_context(
                        self.store, item.shop["id"], checkpoint_key, config
                    ) as checkpoint:
                        if summary_only:
                            # No detail pagination exists in this scope: each retry
                            # verifies the current document, never yesterday's clock.
                            await database_work(checkpoint.clear)
                        if progress.get("force") and not progress.get("continuation"):
                            await database_work(checkpoint.clear)
                        if (
                            options.get("_relative_days")
                            and datetime.fromtimestamp(checkpoint.data["started_at"], BEIJING).date()
                            != datetime.now(BEIJING).date()
                        ):
                            await database_work(checkpoint.clear)
                        read_options = dict(options)
                        if item.shop.get("platform", "pdd") == "pdd" and kind in (
                            "packages",
                            "tickets",
                            "reviews",
                            "evidence",
                            "intercepts",
                        ):
                            read_options["end"] = min(options["end"], int(checkpoint.data["started_at"]))
                        full = bool(checkpoint.data.get("force_full") or progress.get("force", False))
                        checkpoint.data["force_full"] = full
                        if full and kind in ("transactions", "ad_reports") and options.get("trade_range") == "realtime":
                            # Persist the explicit detail request before any page can
                            # fail so a later retry does not silently become summary-only.
                            await database_work(checkpoint.save)
                        from .request_reuse import fresh
                        from .summary_publication import context as summary_context, key as summary_key

                        async def publish_summary(summary_kind, summary_options, payload):
                            if item.retired or summary_kind != kind:
                                return
                            payload["_sync"] = {"snapshot_started_at": checkpoint.data["started_at"], "scope": options}
                            # A distinct cache scope cannot advance the full-list task,
                            # erase its rows, or clear its continuation checkpoint.
                            await database_work(
                                self.store.write_cache, item.shop["id"], summary_key(cache_key), payload
                            )
                            progress["summary_published_at"] = time.time()

                        progress["stage"] = "准备采集页面"
                        from .collection_metrics import phase as measure_phase
                        with fresh(full), summary_context(publish_summary, summary_only=summary_only):
                            with measure_phase('query', '执行当前范围只读查询与分页核验'):
                                result = await collect_planned(item, kind, read_options, cached, config, full, collect)
                        result.setdefault("_sync", {})["snapshot_started_at"] = checkpoint.data["started_at"]
                        return result

                # Recovery runs once per shop after releasing this worker and
                # its read lease; unrelated shops can immediately use the slot.
                data = await planned_read()
                if not summary_only and data.get('complete') is not True:
                    raise PageReadError('查询或分页尚未完成，保留断点，不能标记采集成功')
                from .read_budget import finish as finish_read

                finish_read()
                if not item.retired:
                    from .task_timing import fields as timing_fields

                    data["_sync"] = {
                        **data.get("_sync", {}),
                        **{k: v for k, v in timing_fields(progress).items()
                           if k in ('stage_elapsed_seconds', 'current_wait_seconds', 'stage_durations', 'database_wait_seconds', 'read_seconds')},
                        "duration_seconds": round(time.time() - progress["started_at"], 1),
                        "scope": options,
                    }

                    def publish():
                        payload = data
                        if summary_only:
                            if not payload.get("summary_complete") or not payload.get("details_on_demand"):
                                raise ValueError("首页摘要未完整核验，不能标记采集成功")
                            self.store.write_cache(item.shop["id"], summary_key(cache_key), payload)
                            collection.finish(self.store, item.shop["id"], cache_key)
                            # Old detail checkpoints remain available for explicit
                            # refresh; they must not keep scheduling full pages.
                            with self.store.write_connection() as db:
                                db.execute(
                                    "UPDATE collection_tasks SET continuation=0 WHERE shop_id=? AND cache_key=?",
                                    (item.shop["id"], cache_key),
                                )
                            checkpoints.clear(self.store, item.shop["id"], checkpoint_key)
                            return
                        from .list_completion import publish as publish_completion

                        publish_completion(self, item.shop, kind, options, payload)
                        if (
                            not options.get("_attention")
                            and kind in product_images.KINDS
                            and hasattr(self.store, "product_images")
                        ):
                            payload["rows"] = product_images.enrich(
                                payload.get("rows", []),
                                image_catalog(self.store, item.shop["id"], payload.get("rows", [])),
                            )
                        if kind == "tax":
                            payload = self.store.compare_tax(item.shop["id"], payload, options)
                            changed = sum(
                                bool(row.get("db_comparison", {}).get("attention")) for row in payload["rows"]
                            )
                            if changed:
                                self.store.log_event(
                                    "warning",
                                    "tax",
                                    "database_difference",
                                    f"{changed} 个账期存在异常待核对，已采用最新成功采集金额并保留变更历史",
                                    item.shop["id"],
                                )
                        if options.get("_attention"):
                            attention.publish(self.store, item.shop["id"], kind, payload)
                        else:
                            self.store.write_cache(item.shop["id"], cache_key, payload)
                            if kind == "orders":
                                from .order_plan import publish as publish_orders

                                publish_orders(self.store, item.shop, options, payload)
                            if kind == "invoices" and options.get("_invoice_details_refresh"):
                                from .invoice_fields import backfill

                                backfill(self.store, item.shop["id"], payload)
                            attention.reconcile(self.store, item.shop["id"], kind, payload)
                            collection.archive(self.store, item.shop["id"], cache_key, kind, options, payload)
                        collection.record_change(
                            self.store,
                            item.shop["id"],
                            cache_key,
                            kind,
                            options,
                            payload,
                            config,
                            item.shop.get("platform", "pdd"),
                        )
                        collection.finish(self.store, item.shop["id"], cache_key)
                        self.demands.publish(item.shop, kind, options, cache_key, payload)
                        checkpoints.clear(self.store, item.shop["id"], cache_key)

                    progress["stage"] = "保存采集结果与上传进度"
                    await database_work(publish)
                    outcome = True
                    progress['result'] = 'success'
                    progress['delivery_scope'] = 'attention:' + kind if options.get('_attention') else summary_key(cache_key) if summary_only else cache_key
                    # Refund work runs only after the read is durable, with its own
                    # operation budget: a slow refund check never delays the data.
                    from .auto_refunds import apply as apply_auto_refunds, OPERATION_SECONDS

                    progress["stage"] = "核对小额退款（独立操作时限）"
                    refund_seconds = OPERATION_SECONDS - progress.get("refund_priority_seconds", 0)
                    refreshed = False
                    if refund_seconds > 1:
                        data, refreshed = await apply_auto_refunds(item, self.store, kind, options, data,
                                                                  seconds=refund_seconds, skip_ids=priority_checked)
                    if refreshed and not item.retired:
                        progress["stage"] = "保存退款后的售后列表"
                        try:
                            await database_work(publish)
                        except Exception as error:
                            # The initial complete read is already durable. A
                            # follow-up failure must not turn it into a failed read.
                            await database_work(self.store.log_event, "warning", kind, "refund_refresh_deferred",
                                                "原售后读取已保存，退款后列表刷新入库延后：" + type(error).__name__, item.shop["id"])
                    from .center.delivery_notify import notify

                    notify(self.store.directory, item.shop["id"])
                    self.errors.pop(job_key, None)
                    self.waiting_login.discard(job_key)
                    await database_work(
                        self.store.log_event,
                        "info",
                        kind,
                        "sync_success",
                        "首页摘要已更新，明细按需采集"
                        if summary_only
                        else f"读取完成，保存 {len(data.get('rows', []))} 条记录",
                        item.shop["id"],
                    )
        except checkpoints.YieldCollection:
            if claimed:
                saved = await record_state(collection.yield_batch, self.store, item.shop["id"], cache_key)
                if saved:
                    self.errors.pop(job_key, None)
                else:
                    self.errors[job_key] = (time.time(), "分页已保存，任务状态写入暂受数据库锁阻塞，等待重试")
            # A manually requested batch must finish even while automatic collection is paused.
            config = await database_work(collection.settings, self.store)
            if locals().get("progress", {}).get("force") and (
                not config["enabled"]
                or collection.mode(kind, options, config, item.shop.get("platform", "pdd")) == "off"
            ):
                progress["continue_manual"] = True
            await database_work(
                self.store.log_event,
                "info",
                kind,
                "sync_yield",
                "已保存分页进度，释放浏览器等待下一批",
                item.shop["id"],
            )
        except asyncio.CancelledError:
            if claimed:
                await record_state(collection.finish, self.store, item.shop["id"], cache_key, cancelled=True)
            raise
        except Exception as error:
            if outcome is True:
                # Publication and success metadata are already durable; ancillary notification must not undo them.
                self.errors.pop(job_key, None)
                import logging

                logging.getLogger(__name__).warning("采集已成功入库，后续通知或日志失败：%s", type(error).__name__)
                return
            outcome = False
            if isinstance(error, LoginPending):
                self.waiting_login.add(job_key)
            from .collection_errors import describe

            message = describe(error)
            from .collection_errors import classify

            category, blocked = classify(error)
            if category == "network" or isinstance(error, PageReadError):
                stage = locals().get("progress", {}).get("stage")
                if stage:
                    elapsed = max(
                        0, int(time.time() - locals().get("progress", {}).get("stage_started_at", time.time()))
                    )
                    message += f" · 失败步骤：{stage}（持续 {elapsed} 秒）"
            if isinstance(error, PageReadError) or category == "network":
                from .browser_recovery import request, pending_reason

                recovery = request(
                    item.source, self.wake, self.schedule_wake, repair_page=isinstance(error, PageReadError)
                )
                if recovery is not None:
                    message += " · 已安排浏览器健康检查，结果另记日志；尚未确认恢复"
                    if not hasattr(self, "recovery_tasks"):
                        self.recovery_tasks = set()
                    self.recovery_tasks.add(recovery)
                    recovery.add_done_callback(self.recovery_tasks.discard)
                else:
                    message += " · " + pending_reason(item.source)
                if not getattr(item, "debug_mode", False):
                    # Silent mode hides the window; repeated page failures may come from
                    # background rendering. Point to the per-shop comparison switch.
                    failures = getattr(self, "silent_page_failures", {})
                    self.silent_page_failures = failures
                    failures[item.shop["id"]] = failures.get(item.shop["id"], 0) + 1
                    if failures[item.shop["id"]] >= 3:
                        message += " · 本店后台静默采集已连续失败，可在店铺卡片开启调试模式显示浏览器对比"
            self.errors[job_key] = (time.time(), message)
            if claimed or locals().get("progress", {}).get("wake_failed"):
                await record_state(
                    collection.finish,
                    self.store,
                    item.shop["id"],
                    cache_key,
                    error=message,
                    error_kind=category,
                    blocked=blocked,
                    **({"retry_after": error.retry_after} if category == "shop_cooldown" else {}),
                )
            await database_work(
                self.store.log_event,
                "warning" if isinstance(error, LoginPending) or category in ('rate_limit', 'shop_cooldown') else "error",
                kind,
                "login_pending" if isinstance(error, LoginPending) else 'sync_cooldown' if category in ('rate_limit', 'shop_cooldown') else "sync_error",
                message,
                item.shop["id"],
            )
        finally:
            # Publication/retry/continuation belongs to this step only. The next
            # sibling can use the released slot without waiting for a shop scan.
            if claimed:
                try:
                    from .list_completion import publish_status as publish_completion_status

                    await database_work(publish_completion_status, self, item.shop, kind, options, cache_key)
                    state = await database_work(collection.state, self.store, item.shop["id"], cache_key)
                    if state:
                        from .master_tasks import remember

                        remember(
                            self,
                            item.shop["id"],
                            cache_key,
                            kind,
                            options,
                            {**state, "platform": item.shop.get("platform", "pdd")},
                        )
                except Exception:
                    recovery_log.suppressed("business.py:501 恢复步骤未完成", warning=False)
            if claimed and outcome is not None:
                from .operations_insights import record_run

                try:
                    await database_work(
                        record_run,
                        self.store,
                        item.shop["id"],
                        kind,
                        locals().get("progress", {}).get("started_at", time.time()),
                        outcome,
                        locals().get("category", ""),
                    )
                except Exception:
                    recovery_log.suppressed("business.py:505 恢复步骤未完成", warning=False)
                self.completions.append((time.time(), outcome))
                if outcome:
                    getattr(self, "silent_page_failures", {}).pop(item.shop["id"], None)
                self.completions = [r for r in self.completions if r[0] > time.time() - 900]
            self.deferred.pop(job_key, None)
            self.schedule_wake.set()
            self.due_plans.request_dispatch()
            pending = locals().get("progress", {})
            if (
                ((outcome is not None and pending.get("repeat_force")) or pending.get("continue_manual"))
                and not item.retired
                and item.state == "logged_in"
            ):
                self.due_plans.repeat[job_key] = (item, kind, options)

    def request_sync(self, kind, options, shop_id=None, force=False, platform=None):
        """Explicit command: register demand; collect only missing scopes or manual refresh."""
        if kind == "orders":
            from .order_plan import view, request

            if view(options):
                return request(self, options, shop_id, force, platform)
        records = [
            r
            for r in self.store.list()
            if supports(r.get("platform", "pdd"), kind)
            and matches(platform, r.get("platform", "pdd"))
            and matches(shop_id, r["id"])
        ]
        if shop_id is not None and not records:
            raise ValueError("店铺不存在或该平台采集尚未接入")
        self.focus_kind = kind
        tasks = []
        plans = [(record, self.query_options(kind, options, record)) for record in records]
        plans = [(record, read_options, self.key(kind, read_options, record)) for record, read_options in plans]
        caches, states = scheduling_states(self.store, [(record["id"], key) for record, _, key in plans])
        for record, read_options, key in plans:
            item = self.session_getter(record["id"])
            cached = caches.get((record["id"], key))
            state = states.get((record["id"], key)) or {}
            if (
                force
                or not cached
                or (record["id"], key) in self.waiting_login
                or state.get("failures", 0) > 0
                or state.get("continuation")
            ):
                self.enqueue(record, item, kind, read_options, force=force)
            else:
                collection.register(self.store, record["id"], key, kind, read_options, cached)
            tasks.append({"shop_id": record["id"], "syncing": (record["id"], key) in self.jobs or self.due_plans.pending((record["id"], key)), "cached": bool(cached)})
        self.wake.set()
        return {"tasks": tasks, "mode": "database", "force": force}

    def data(
        self,
        kind,
        options,
        shop_id=None,
        refresh=False,
        force=False,
        foreground=True,
        platform=None,
        indexed=False,
        read_only=False,
    ):
        # Legacy refresh/force query parameters are deliberately read-only.
        records = [
            r
            for r in self.store.list()
            if supports(r.get("platform", "pdd"), kind) and matches(platform, r.get("platform", "pdd"))
        ]
        if shop_id is not None:
            records = [r for r in records if matches(shop_id, r["id"])]
            if not records:
                raise ValueError("店铺不存在")
        sources, rows, metrics = [], [], []
        config = collection.settings(self.store)
        from .query_batch import read as read_batch

        keys = {r["id"]: self.key(kind, self.query_options(kind, options, r), r) for r in records}
        caches, tasks = read_batch(self.store, keys, indexed)
        from .summary_publication import KINDS as SUMMARY_KINDS, read_many as read_summaries, merge as merge_summary

        summaries = read_summaries(self.store, keys) if kind in SUMMARY_KINDS else {}
        for record in records:
            item = self.session_getter(record["id"])
            cache_key = keys[record["id"]]
            job_key = (record["id"], cache_key)
            cached = merge_summary(kind, caches.get(job_key), summaries.get(job_key))
            error = self.errors.get(job_key)
            task = tasks.get(job_key, {})
            if not error and task.get("last_error"):
                error = (task["retry_at"], task["last_error"])
            age = time.time() - cached["updated"] if cached else float("inf")
            payload = cached["payload"] if cached else {}
            if not indexed and cached and payload.get("rows"):
                from .focus_records import current_rows

                payload = {
                    **payload,
                    "rows": current_rows(
                        self.store, record["id"], kind, payload, cached["updated"], record.get("platform", "pdd")
                    ),
                }
                if kind == "ad_products" and record.get("platform", "pdd") == "pdd":
                    from .promotion_status import normalize

                    payload = {**payload, "rows": [normalize(r) for r in payload.get("rows", [])]}
            # An overnight read must not relabel yesterday's daily totals as today.
            previous_day = (
                kind == "overview"
                and cached
                and datetime.fromtimestamp(
                    payload.get("_sync", {}).get("snapshot_started_at") or cached["updated"], BEIJING
                ).date()
                < datetime.now(BEIJING).date()
            )
            if previous_day:
                daily_fields = ("sales", "orders", "ad_spend", "visitors", "views", "reviews_today")
                payload = {**payload, "metrics": {**payload.get("metrics", {}), **{key: None for key in daily_fields}}}
            if kind in product_images.KINDS and payload.get("rows"):
                payload = {
                    **payload,
                    "rows": product_images.enrich(
                        payload["rows"], image_catalog(self.store, record["id"], payload["rows"])
                    ),
                }
            if kind == "tax" and cached:
                canonical = self.store.tax_snapshot(record["id"], options)
                if canonical:
                    payload = {**payload, "rows": canonical, "database_compared": True}
            source = {
                **identity(record),
                "operators": record.get("operators", []),
                "shop_id": record["id"],
                "shop_name": record["name"] or f"店铺 {record['id']}",
                "state": item.state,
                "syncing": job_key in self.jobs or self.due_plans.pending(job_key),
                "waiting_login": job_key in self.waiting_login,
                "cache_missing": cached is None,
                "recovery_status": getattr(item, "recovery_status", ""),
                "updated_at": datetime.fromtimestamp(cached["updated"], BEIJING).isoformat() if cached else None,
                "error": error[1]
                if error
                else (None if item.state in ("logged_in", "sleeping") else "店铺未登录，请先在店铺管理中完成登录"),
                "loaded_count": payload.get("_row_count", len(payload.get("rows", []))),
                "source_total": payload.get("source_total"),
                "complete": payload.get("complete", False),
                "source_name": payload.get("source_name"),
                "scope_note": payload.get("scope_note"),
                "source_shop_id": payload.get("source_shop_id"),
                "source_unit": payload.get("source_unit", "记录"),
                "source_loaded": payload.get("source_loaded", payload.get("_row_count", len(payload.get("rows", [])))),
                "latest_summary_day": payload.get("latest_summary_day"),
                "blocked": bool(task.get("blocked")),
                "error_kind": task.get("error_kind"),
                "age_seconds": round(age) if cached else None,
                "day_pending": bool(previous_day),
                "stale": collection.due(
                    kind,
                    options,
                    cached["updated"] if cached else 0,
                    config,
                    platform=record.get("platform", "pdd"),
                    adaptive_seconds=task.get("adaptive_seconds", 0),
                )
                or bool(error)
                or item.state not in ("logged_in", "sleeping"),
            }
            progress = self.progress.get(job_key, {})
            source.update(
                summary_complete=bool(payload.get("summary_complete")),
                details_pending=bool(payload.get("details_pending")),
                details_on_demand=bool(payload.get("details_on_demand")),
                details_complete=bool(payload.get("details_complete", payload.get("complete"))),
                summary_updated_at=payload.get("summary_updated_at"),
                details_updated_at=payload.get(
                    "details_updated_at", cached["updated"] if cached and not payload.get("details_pending") else None
                ),
            )
            if read_only:
                from .home_counts import source_fields

                source.update(source_fields(payload))
                source.update(
                    collector_online=True,
                    paused=not config.get("enabled", True)
                    or collection.mode(kind, options, config, record.get("platform", "pdd")) == "off",
                )
            source.update(
                sync_phase=progress.get("phase"),
                sync_stage=progress.get("stage", "等待同步"),
                sync_loaded=progress.get("loaded", 0),
                sync_total=progress.get("total"),
                elapsed_seconds=round(time.time() - progress["started_at"], 1) if progress.get("started_at") else None,
                duration_seconds=payload.get("_sync", {}).get("duration_seconds"),
                source_time=payload.get("metrics", {}).get("source_time") or payload.get("latest_summary_day"),
                data_status="waiting_login"
                if source["waiting_login"]
                else progress.get("phase")
                or (
                    "error"
                    if source["error"]
                    else "missing"
                    if not cached
                    else "partial"
                    if not source["complete"]
                    else "empty"
                    if not source["loaded_count"] and not payload.get("metrics")
                    else "ready"
                ),
            )
            sources.append(source)
            if kind == "tax":
                source["entity"] = payload.get("entity")
            if "_creation_counts" in payload:
                source["_creation_counts"] = payload["_creation_counts"]
            if payload.get("metrics"):
                metrics.append({**payload["metrics"], **source})
            rows.extend(
                {
                    **decorate(kind, row, record.get("platform", "pdd")),
                    **identity(record),
                    "shop_id": record["id"],
                    "shop_name": source["shop_name"],
                    "operator": record.get("operator", ""),
                }
                for row in payload.get("rows", [])
            )
        totals = {}
        if kind == "overview":
            for key in (
                "sales",
                "orders",
                "ad_spend",
                "visitors",
                "views",
                "reviews_today",
                "pending_payment",
                "pending_shipping",
                "pending_aftersales",
                "pending_tickets",
                "shipping_due",
                "aftersales_due",
            ):
                values = [row[key] for row in metrics if row.get(key) is not None]
                totals[key] = round(sum(values), 2) if values else None
        comparison = {}
        if kind == "overview":
            from .home_comparison import read as previous_overview

            comparison = {
                "previous": previous_overview(
                    self.store,
                    records,
                    metrics,
                    keys={
                        r["id"]: (
                            self.key("overview", {}, r),
                            self.key("transactions", transactions.options_for("yesterday"), r),
                        )
                        for r in records
                    },
                )
            }
        return {
            "sources": sources,
            "rows": rows,
            "metrics": metrics,
            "totals": totals,
            "timezone": "Asia/Shanghai",
            **comparison,
        }


def create_router(service, authorize):
    if hasattr(service, "store") and not hasattr(service, "export_jobs"):
        from .export_jobs import ExportJobs

        service.export_jobs = ExportJobs(service.store.path.parent)
    router = APIRouter(prefix="/api/data", dependencies=[Depends(authorize)])
    from .list_completion import install as install_completions

    install_completions(router, service)

    @router.post("/{kind}/sync", status_code=202)
    async def request_sync(
        kind: str,
        shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
        force: bool = False,
        date_from: str = "",
        date_to: str = "",
        status: str = "",
        stars: str = "",
        reply: str = "",
        period: str = Query(default="", max_length=7),
        trade_range: str = "realtime",
        platform: str = "",
        exact_dates: bool = False,
        source: str = Query(default="manual", pattern=r"^(manual|automatic)$"),
    ):
        if platform and not any(supports(p, kind) for p in values(platform)):
            raise HTTPException(400, "该平台经营数据尚未接入")
        if kind not in KINDS:
            raise HTTPException(404, "数据类型不存在")
        try:
            options = (
                promotions.options_for(trade_range)
                if kind in promotions.KINDS
                else goods_data.options_for(trade_range)
                if kind == "goodsdata"
                else transactions.options_for(trade_range)
                if kind == "transactions"
                else tax_info.options_for(period)
                if kind == "tax"
                else reconciliation.options_for(kind, period)
                if kind in reconciliation.KINDS
                else options_for(kind, date_from, date_to, status, stars, reply)
            )
            if exact_dates:
                from .date_collection import exact_options

                if platform and platform != "pdd":
                    raise ValueError("该平台暂未接入指定日期采集")
                platform = "pdd"
                options = exact_options(kind, options, date_from, date_to)
            # Only the center persists dispatch intent. Local registration keeps
            # its existing behavior; explicit refresh is always a manual demand.
            intent = (
                {"source": "automatic"} if source == "automatic" and not force and hasattr(service, "database") else {}
            )
            return service.request_sync(
                kind,
                options,
                int(shop_id) if shop_id and "," not in str(shop_id) else shop_id,
                force,
                platform=platform or None,
                **intent,
            )
        except ValueError as error:
            raise HTTPException(400, str(error))

    @router.get("/{kind}")
    def get_data(
        kind: str,
        shop_id: str | None = Query(default=None, max_length=4000, pattern=r"^[1-9]\d*(,[1-9]\d*)*$"),
        keyword: str = Query(default="", max_length=200),
        status: str = "",
        stars: str = "",
        reply: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
        min_price: float | None = Query(default=None, ge=0),
        max_price: float | None = Query(default=None, ge=0),
        low_stock: int | None = Query(default=None, ge=0),
        refresh: bool = False,
        force: bool = False,
        priority_only: bool = False,
        carrier: str = Query(default="", max_length=100),
        period: str = Query(default="", max_length=7),
        subject: str = Query(default="", max_length=100),
        trade_range: str = "realtime",
        sort_by: str = "",
        sort_direction: str = "desc",
        export: bool = False,
        dashboard: bool = False,
        activity_type: str = Query(default="", max_length=100),
        brand_reason: str = Query(default="", max_length=300),
        activity_group: str = Query(default="", max_length=30),
        new_products_year: int | None = Query(default=None, ge=2000, le=2100),
        platform: str = "",
        exact_dates: bool = False,
        background: bool = False,
        home_counts: bool = False,
    ):
        if export and background:
            import inspect

            frame = dict(locals())
            arguments = {key: frame[key] for key in inspect.signature(get_data).parameters}
            arguments["background"] = False
            return service.export_jobs.submit(lambda: get_data(**arguments))
        if platform and not any(supports(p, kind) for p in values(platform)):
            raise HTTPException(400, "该平台经营数据尚未接入，请使用对应平台的独立适配模块")
        if kind not in KINDS:
            raise HTTPException(404, "数据类型不存在")
        try:
            if home_counts:
                from .home_counts import STATUSES

                if status not in STATUSES.get(kind, set()) or export or dashboard:
                    raise ValueError("首页数量查询范围无效")
            options = (
                promotions.options_for(trade_range)
                if kind in promotions.KINDS
                else goods_data.options_for(trade_range)
                if kind == "goodsdata"
                else transactions.options_for(trade_range)
                if kind == "transactions"
                else tax_info.options_for(period)
                if kind == "tax"
                else reconciliation.options_for(kind, period)
                if kind in reconciliation.KINDS
                else options_for(kind, date_from, date_to, status, stars, reply)
            )
            if exact_dates:
                from .date_collection import exact_options

                if platform and platform != "pdd":
                    raise ValueError("该平台暂未接入指定日期采集")
                platform = "pdd"
                options = exact_options(kind, options, date_from, date_to)
            if activity_group not in ("", "待处理", "审核中", "活动中", "已结束"):
                raise ValueError("活动快捷分类无效")
            if sort_direction not in ("asc", "desc"):
                raise ValueError("排序方向无效")
            if min_price is not None and max_price is not None and min_price > max_price:
                raise ValueError("最低价格不能高于最高价格")
            from . import table_index

            indexed = isinstance(service, BusinessService) and kind in table_index.KINDS and not dashboard
            if home_counts and hasattr(service, "home_counts"):
                local_start, local_end = (
                    (options["date_from"], options["date_to"])
                    if kind in ("tickets", "orders")
                    else (date_from, date_to)
                )
                filters = dict(
                    keyword=keyword,
                    status=status,
                    stars=options.get("stars", ()),
                    reply=reply,
                    date_from=local_start,
                    date_to=local_end,
                    min_price=min_price,
                    max_price=max_price,
                    low_stock=low_stock,
                    priority_only=priority_only,
                    carrier=carrier,
                    activity_type=activity_type,
                    brand_reason=brand_reason,
                    activity_group=activity_group,
                )
                data = service.home_counts(
                    kind, options, shop_id, platform=platform or None, filters=filters, page_size=page_size
                )
            else:
                data = service.data(
                    kind,
                    options,
                    int(shop_id) if shop_id and "," not in str(shop_id) else shop_id,
                    refresh,
                    force,
                    **({"platform": platform} if platform else {}),
                    **({"indexed": True} if indexed or dashboard and kind in table_index.KINDS else {}),
                    **({"read_only": True} if home_counts else {}),
                )
            if kind == "tax":
                data["subjects"] = tax_info.subject_options(data.get("sources", []) + data.get("rows", []))
                data["rows"] = tax_info.group_rows(data["rows"], subject)
                if subject:
                    data["sources"] = [
                        s
                        for s in data.get("sources", [])
                        if not s.get("entity") or tax_info.subject_key(s["entity"], s["shop_id"]) == subject
                    ]
            if kind == "transactions":
                data["trade_totals"] = transactions.summarize(data["metrics"])
            if kind == "goodsdata":
                data["goods_totals"] = goods_data.summarize(data["metrics"])
            if kind in promotions.KINDS:
                data["ad_totals"] = promotions.summarize(data["metrics"])
            if dashboard and kind == "products" and not export:
                from .new_products import summarize

                return summarize(
                    data,
                    date_from or datetime.now(BEIJING).date().isoformat(),
                    new_products_year,
                    service.store.operators() if hasattr(service, "store") else (),
                )
            if dashboard:
                if kind not in ("transactions", "ad_reports", "orders") or export:
                    raise ValueError("该模块不支持概览摘要")
                keep = (
                    (
                        "platform",
                        "shop_label",
                        "shop_id",
                        "shop_name",
                        "refund_orders",
                        "refund_amount",
                        "refund_date",
                        "refund_basis",
                        "refund_complete",
                        "updated_at",
                        "syncing",
                        "error",
                    )
                    if kind == "orders"
                    else (
                        "platform",
                        "shop_label",
                        "shop_id",
                        "shop_name",
                        "summary",
                        "trend",
                        "source_time",
                        "stale",
                        "updated_at",
                        "syncing",
                        "error",
                    )
                    if kind == "transactions"
                    else (
                        "platform",
                        "shop_label",
                        "shop_id",
                        "shop_name",
                        "source_time",
                        "stale",
                        "updated_at",
                        "syncing",
                        "error",
                        *promotions.FIELDS,
                    )
                )
                return {
                    **data,
                    "rows": [],
                    "metrics": [{k: m[k] for k in keep if k in m} for m in data["metrics"]],
                    "scope": options,
                }
            if indexed:
                local_start, local_end = date_from, date_to
                if kind in ("reviews", "tickets", "orders"):
                    local_start, local_end = options["date_from"], options["date_to"]
                if kind in ("intercepts", "navigator", "brands"):
                    local_start = local_end = ""
                ids = {s["shop_id"] for s in data["sources"] if s["updated_at"]}
                selections = [
                    (s, service.key(kind, service.query_options(kind, options, s), s))
                    for s in service.store.list()
                    if s["id"] in ids
                ]
                result = table_index.query(
                    service.store,
                    selections,
                    kind,
                    keyword=keyword,
                    status=status,
                    stars=options.get("stars", ()),
                    reply=reply,
                    date_from=local_start,
                    date_to=local_end,
                    min_price=min_price,
                    max_price=max_price,
                    low_stock=low_stock,
                    priority_only=priority_only,
                    carrier=carrier,
                    activity_type=activity_type,
                    brand_reason=brand_reason,
                    activity_group=activity_group,
                    sort_by=sort_by or ("visitors" if kind == "goodsdata" else ""),
                    sort_direction=sort_direction,
                    page=page,
                    page_size=page_size,
                    export=export,
                    include_focus=not options.get("_exact_dates"),
                    count_only=home_counts,
                )
                data.update(result)
                if home_counts:
                    for item in data["sources"]:
                        item.update(
                            result.get("count_sources", {}).get(
                                item["shop_id"], {"complete": False, "updated_at": None, "snapshot_scope": {}}
                            )
                        )
                page = result.get("page", page)
                if not (
                    platform == "taobao"
                    or bool(data["sources"])
                    and all(s.get("platform") == "taobao" for s in data["sources"])
                ):
                    data["statuses"] = sorted(set(data["statuses"]) | set(OPERATION_STATUS.get(kind, [])))
            elif home_counts and data.get("_home_counted"):
                data.pop("_home_counted", None)
            elif home_counts:
                local_start, local_end = date_from, date_to
                if kind in ("tickets", "orders"):
                    local_start, local_end = options["date_from"], options["date_to"]
                rows = filter_rows(
                    data["rows"],
                    kind,
                    keyword,
                    status,
                    options["stars"],
                    reply,
                    local_start,
                    local_end,
                    min_price,
                    max_price,
                    low_stock,
                    priority_only,
                    carrier,
                    activity_type,
                    brand_reason,
                    activity_group,
                )
                from collections import Counter

                data.update(
                    shop_counts=dict(Counter(row["shop_id"] for row in rows)),
                    total=len(rows),
                    rows=[],
                    page_count=max(1, (len(rows) + page_size - 1) // page_size),
                )
            elif kind not in ("overview", "transactions"):
                if kind == "invoices":
                    from .shop_entities import company_names

                    companies = company_names(service.store)
                    data["rows"] = [{**r, "company_name": companies.get(r["shop_id"], "")} for r in data["rows"]]
                if kind == "brands":
                    data["brand_reasons"] = sorted(
                        {v for r in data["rows"] for v in str(r.get("problem") or "").split("；") if v}
                    )
                if kind == "activities":
                    data["activity_types"] = sorted(
                        {r["activity_type"] for r in data["rows"] if r.get("activity_type")}
                    )
                taobao_only = (
                    platform == "taobao"
                    or bool(data.get("sources"))
                    and all(s.get("platform") == "taobao" for s in data["sources"])
                )
                data["statuses"] = sorted(
                    (set() if taobao_only else set(OPERATION_STATUS.get(kind, [])))
                    | {r["status"] for r in data["rows"] if r.get("status")}
                )
                local_start, local_end = date_from, date_to
                if kind in ("reviews", "tickets", "orders"):
                    local_start, local_end = options["date_from"], options["date_to"]
                # Intercept API filters by order time, which is not returned in its rows.
                if kind in ("intercepts", "navigator", "brands"):
                    local_start = local_end = ""
                rows = filter_rows(
                    data["rows"],
                    kind,
                    keyword,
                    status,
                    options["stars"],
                    reply,
                    local_start,
                    local_end,
                    min_price,
                    max_price,
                    low_stock,
                    priority_only,
                    carrier,
                    activity_type,
                    brand_reason,
                    activity_group,
                )
                rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row["id"])), reverse=True)
                rows.sort(
                    key=lambda row: (
                        (-row.get("priority", 0), row.get("deadline") or "9999")
                        if row.get("priority", 0) > 1
                        else (-row.get("priority", 0), "9999")
                    )
                )
                from .table_sort import sort_rows

                rows = sort_rows(rows, sort_by or ("visitors" if kind == "goodsdata" else ""), sort_direction)
                from .priority_rules import KINDS as FOCUS_KINDS

                if kind in FOCUS_KINDS:
                    rows.sort(key=lambda r: not bool(r.get("priority")))
                data["priority_count"] = sum(bool(row.get("priority")) for row in rows)
                data["total"] = len(rows)
                if kind in reconciliation.KINDS:
                    data["finance_summary"] = reconciliation.summarize(rows)
                if kind == "tax":
                    data["tax_summary"] = tax_info.summarize(rows)
                    data["difference_count"] = sum(bool(row.get("db_changed")) for row in rows)
                    data["missing_count"] = sum(bool(row.get("db_missing")) for row in rows)
                data["page_count"] = max(1, (len(rows) + page_size - 1) // page_size)
                page = max(1, min(page, data["page_count"]))
                data["rows"] = rows if export else rows[(page - 1) * page_size : page * page_size]
            data.update({"page": page, "page_size": page_size, "scope": options})
            if home_counts:
                from .home_counts import project

                data["home_counts"] = project(data, kind, options)
                data["home_counts"]["filters"] = {
                    "status": status,
                    "date_from": date_from,
                    "date_to": date_to,
                    "exact_dates": exact_dates,
                }
                data["rows"] = []
            if export:
                from .xlsx_export import response

                return response(
                    kind,
                    data,
                    {
                        "店铺": shop_id,
                        "关键词": keyword,
                        "状态": status,
                        "星级": stars,
                        "回复": reply,
                        "开始日期": date_from,
                        "结束日期": date_to,
                        "最低价格": min_price,
                        "最高价格": max_price,
                        "库存上限": low_stock,
                        "仅重点": priority_only,
                        "快递公司": carrier,
                        "账期": period,
                        "主体": subject,
                        "统计范围": trade_range,
                        "排序指标": sort_by,
                        "排序方向": sort_direction,
                        "活动类型": activity_type,
                        "无黑标原因": brand_reason,
                        "活动快捷分类": activity_group,
                    },
                )
            return data
        except ValueError as error:
            raise HTTPException(400, str(error))

    return router
