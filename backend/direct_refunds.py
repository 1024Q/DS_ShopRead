"""Event-driven refund operations, independent of collection admission and shop turns."""
import asyncio
import contextvars
import logging
import time

from .async_database import read, financial_file as durable
from . import auto_refunds, refund_priority, update_gate

logger = logging.getLogger(__name__)


class DirectRefunds:
    def __init__(self, service):
        self.service, self.store = service, service.store
        self.tasks, self.status, self.retry_at = {}, {}, {}
        self.dirty = set()
        self.boot_seen = set()
        self.loop = None
        self.closed = False

    def allowed(self, item):
        config = self.service.queue_config
        from .home_only_pause import held
        from .collection import mode

        return (not self.closed and not update_gate.active() and config.get('enabled', True)
                and not getattr(item, 'retired', False) and getattr(item, 'wanted_running', False) is True
                and item.shop.get('platform', 'pdd') == 'pdd' and item.state in ('logged_in', 'sleeping')
                and not config.get('home_paused')
                and not held('aftersales', {'_attention': True}, config, 'pdd')
                and mode('aftersales', {'_attention': True}, config, 'pdd') != 'off')

    def notify(self, shop_id):
        if self.loop and not self.closed:
            try:
                self.loop.call_soon_threadsafe(self.offer, shop_id, context=contextvars.Context())
            except RuntimeError:
                pass  # The owning service has already stopped.

    def offer(self, shop_id):
        if self.closed:
            return
        self.dirty.add(shop_id)
        if not update_gate.active() and shop_id not in self.tasks and self.retry_at.get(shop_id, 0) <= time.monotonic():
            self.tasks[shop_id] = asyncio.create_task(self.handle(shop_id), context=contextvars.Context())

    async def handle(self, shop_id):
        item = None
        active = False
        candidates = []
        try:
            self.dirty.discard(shop_id)
            item = self.service.session_getter(shop_id)
            if not self.allowed(item):
                return
            candidates = await read(lambda: refund_priority.ordered(self.store, shop_id, refund_priority.pending(self.store, shop_id)))
            if not candidates:
                return
            from .request_reuse import cooling
            from .operations import ENDPOINTS

            wait = max(cooling(item, path) for path in (ENDPOINTS['aftersales'], auto_refunds.PRECHECK, auto_refunds.SUBMIT))
            def stored_cooldown():
                with self.store.shop_db(shop_id) as db:
                    return db.execute("SELECT coalesce(max(retry_at),0) FROM collection_tasks WHERE shop_id=? AND kind='aftersales' AND error_kind='rate_limit'", (shop_id,)).fetchone()[0]
            wait = max(wait, await read(stored_cooldown) - time.time())
            wait = max(wait, getattr(item, 'control_until', 0) - time.time())
            if wait > 0 or getattr(item, 'remote_control_active', False):
                self.dirty.add(shop_id)
                self.retry_at[shop_id] = time.monotonic() + max(1, wait)
                self.status[shop_id] = '平台限流或人工控制中，恢复后直接处理'
                await durable(refund_priority.deferred, self.store, shop_id, candidates, self.status[shop_id], max(1, wait))
                return
            item.direct_refund_active = True
            active = True
            self.status[shop_id] = '平台处理中：直接核验，不经过采集队列'
            await durable(self.store.log_event, 'info', 'aftersales', 'refund_direct_start', self.status[shop_id], shop_id)
            if item.state == 'sleeping':
                from .browser_sleep import wake

                await wake(item)
            if not self.allowed(item):
                return
            checked = await auto_refunds.prioritize(item, self.store, 'aftersales', {'_attention': True}, direct=True)
            receipt = await durable(refund_priority.receipt, self.store, shop_id)
            results = receipt.get('results', {})
            summary = '；'.join(dict.fromkeys(results[case]['reason'] for case in checked if case in results))
            self.status[shop_id] = f'直接核验完成 {len(checked)} 单；{summary or "未完成核验的订单将重试"}'
            # No tight retry loop after an unavailable page or a concurrent refund operation.
            self.retry_at[shop_id] = time.monotonic() + (15 if not checked else 0)
            if not checked:
                self.dirty.add(shop_id)
            if checked:
                await durable(self.store.log_event, 'info', 'aftersales', 'refund_direct_checked', self.status[shop_id], shop_id)
                remaining = await read(refund_priority.pending, self.store, shop_id)
                if await read(refund_priority.needs_run, self.store, shop_id, remaining):
                    self.dirty.add(shop_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if getattr(error, 'status_code', None) == 404 or isinstance(error, KeyError):
                self.dirty.discard(shop_id)
                self.retry_at.pop(shop_id, None)
                return
            self.dirty.add(shop_id)
            self.retry_at[shop_id] = time.monotonic() + 30
            detail = str(error)[:160] or type(error).__name__
            self.status[shop_id] = f'直接处理暂缓：{detail}；未提交的订单将在恢复后重新核验'
            if candidates:
                await durable(refund_priority.deferred, self.store, shop_id, candidates, self.status[shop_id])
            logger.warning('直接退款处理暂缓 · shop=%s · %s', shop_id, type(error).__name__)
        finally:
            if item is not None and active:
                item.direct_refund_active = False
                item.last_collection_activity = time.monotonic()
            self.tasks.pop(shop_id, None)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.store.direct_refunds = self
        scanned = 0
        was_paused = False
        try:
            while True:
                # Also recovers persisted cases at startup and missed notifications, without waking stopped shops.
                config = self.service.queue_config
                paused = bool(config.get('home_paused') or not config.get('enabled', True))
                resumed = was_paused and not paused
                was_paused = paused
                if time.monotonic() >= scanned or resumed:
                    try:
                        shops = await read(self.store.list)
                    except Exception as error:
                        logger.warning('直接退款待办扫描暂缓 · %s', type(error).__name__)
                        shops = []
                    for shop in shops:
                        item = self.service.session_getter(shop['id'])
                        if self.allowed(item) and shop.get('platform', 'pdd') == 'pdd':
                            try:
                                cases = await read(refund_priority.pending, self.store, shop['id'])
                                if cases and (shop['id'] not in self.boot_seen or resumed or await read(refund_priority.needs_run, self.store, shop['id'], cases)):
                                    self.boot_seen.add(shop['id'])
                                    self.offer(shop['id'])
                            except Exception as error:
                                logger.warning('直接退款待办扫描暂缓 · shop=%s · %s', shop['id'], type(error).__name__)
                    scanned = time.monotonic() + 10
                for sid in list(self.dirty):
                    self.offer(sid)
                config = self.service.queue_config
                if config.get('home_paused') or not config.get('enabled', True):
                    await self.cancel_all()
                await asyncio.sleep(1)
        finally:
            await self.close()

    async def cancel_shop(self, shop_id):
        self.dirty.discard(shop_id)
        task = self.tasks.get(shop_id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.tasks.get(shop_id) is task:
                self.tasks.pop(shop_id, None)

    async def cancel_all(self):
        self.dirty.clear()
        await asyncio.gather(*(self.cancel_shop(sid) for sid in list(self.tasks)))

    async def close(self):
        self.closed = True
        # Retain the owner marker during shutdown, so an ordinary reader cannot
        # take financial work back while this service is cancelling its tasks.
        await self.cancel_all()


def notify(store, shop_id, kind):
    worker = getattr(store, 'direct_refunds', None)
    if kind == 'aftersales' and worker:
        worker.notify(shop_id)
