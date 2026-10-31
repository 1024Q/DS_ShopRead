"""Single node dispatcher and execution lifecycle adapter for SchedulingKernel."""
import asyncio
import contextvars
import math
import time
from collections import Counter
from contextlib import contextmanager
from .scheduling_kernel import SchedulingKernel, Demand

current_execution = contextvars.ContextVar('node_execution', default=None)


class Execution:
    def __init__(self, dispatcher, key, token, meta):
        self.dispatcher, self.key, self.token, self.meta = dispatcher, key, token, meta

    async def database(self):
        self.dispatcher.core.phase(self.key, self.token, 'database')
        self.meta.update(execution_phase='database', reading_slot=False)
        self.dispatcher.request_dispatch()

    async def resume(self):
        from .read_budget import current
        budget = current.get()
        if budget and budget.done:
            self.finish();return
        if asyncio.current_task().cancelling():
            return  # Cleanup never waits for a new read slot.
        owner = self.dispatcher
        if not owner.core.phase(self.key, self.token, 'resuming'):
            return
        future = asyncio.get_running_loop().create_future()
        owner.resumes[self.key] = future
        self.meta.update(execution_phase='resuming', reading_slot=False,
                         resource_wait='本地入库完成，等待续读名额')
        owner.request_dispatch()
        try:
            from .collection_metrics import phase
            from contextlib import nullcontext
            with phase('slot_wait', '入库后等待续读名额'), budget.pause('slot_wait_seconds') if budget else nullcontext():
                await future
        finally:
            if owner.resumes.get(self.key) is future:owner.resumes.pop(self.key, None)
        self.meta.update(execution_phase='reading', reading_slot=True, resource_wait='')

    def finish(self):
        self.dispatcher.core.phase(self.key, self.token, 'finishing')
        self.meta.update(execution_phase='finishing', reading_slot=False,
                         resource_wait='读取已结束，等待结果与断点确认')
        self.dispatcher.request_dispatch()


class NodeDispatch:
    def __init__(self, service):
        self.service = service
        self.core = SchedulingKernel()
        self.plans = {}
        self.resumes = {}
        self.direct = {}
        self.preparations = {}
        self.browser_failures = {}
        self.browser_times = {}
        self.handle = None
        self.timer = None
        self.owner_loop = None
        self.dispatching = False
        self.closed = False
        self.waits = {}
        self.workers = {}
        self.specs = {}
        self.repeat = {}
        self.registration_depth = 0

    @contextmanager
    def registration_batch(self):
        """Coalesce one bounded synchronous discovery batch, never a DB await."""
        self.registration_depth += 1
        try:yield
        finally:
            self.registration_depth -= 1
            self.request_dispatch()

    @property
    def entries(self):
        return self.plans

    @property
    def grants(self):
        return {k for k,r in self.core.reservations.items() if r.phase == 'reserved'}

    def pending(self, key):
        entry = self.plans.get(key)
        if not entry or key in self.service.jobs or entry[1].get('await_discovery'):return False
        demand = self._demand(key,*entry,time.time())
        return demand.enabled and demand.due_at <= time.time()

    def add(self, key, item, meta):
        old = self.plans.get(key)
        if old:
            previous = old[1]
            for name in ('waiting_since', 'queued_at'):
                meta[name] = previous.get(name, previous.get('queued_at', time.time()))
            for name in ('force', 'foreground', 'resume'):
                meta[name] = bool(meta.get(name) or previous.get(name))
        meta.setdefault('queued_at', time.time())
        meta.setdefault('waiting_since', meta['queued_at'])
        meta.setdefault('next_run_at', meta['queued_at'])
        meta['phase'] = 'waiting'
        self.plans[key] = (item, meta)
        if key in self.waits:
            self.service.deferred[key] = meta
        self.request_dispatch()

    def observe(self, waits):
        """A discovery snapshot supplies future/retry plans without workers."""
        s = self.service
        for key, raw in waits.items():
            if key in s.jobs or self.plans.get(key, (None, {}))[1].get('force'):continue
            try:item = s.session_getter(key[0])
            except (KeyError, TypeError):continue
            meta = {**getattr(s, 'master_steps', {}).get(key, {}), **raw}
            meta.setdefault('queued_at', time.time())
            self.add(key, item, meta)

    def _config(self):
        return {**self.service.queue_config, **getattr(self.service, 'runtime_limits', {})}

    def _demand(self, key, item, meta, now, resume=False, resources=None):
        from . import collection
        from .home_only_pause import held
        from .shop_rotation import home_paused
        from .queue_control import held_until
        from .collection_dispatch import browser_blocked
        config = resources['config'] if resources is not None else self._config()
        kind = meta.get('kind', key[1].split(':')[0]);scope = meta.get('scope', {})
        platform = meta.get('platform', getattr(item, 'shop', {}).get('platform', 'pdd'))
        enabled = (not self.closed and not getattr(self.service, 'closing', False)
                   and not getattr(item, 'retired', False) and getattr(item, 'state', 'logged_in') != 'stopped'
                   and not home_paused(kind, scope, config) and not held(kind, scope, config, platform)
                   and (meta.get('force') or config.get('enabled', True)
                        and collection.mode(kind, scope, config, platform) != 'off'))
        blocked = ''
        blocked_code = 'browser'
        if getattr(item, 'control_until', 0) > now:blocked, blocked_code = '等待人工控制结束', 'control'
        elif hasattr(item, 'lock') and browser_blocked(item):
            blocked = getattr(item, 'collection_recovery_stage', '') or '等待浏览器独占操作结束'
        elif meta.get('blocked') or meta.get('stage') == '需要人工处理':
            blocked = meta.get('reason') or meta.get('last_error') or '等待处理权限或页面适配异常'
            blocked_code = 'blocked'
        elif meta.get('lease_until', 0) > now:
            blocked = '等待旧执行持久化占位核验，禁止重复运行'
            blocked_code = 'lease'
        failure = self.browser_failures.get(key[0], {})
        sleeping = getattr(item, 'sleeping', False) or getattr(item, 'state', '') == 'sleeping'
        retry = max(meta.get('retry_at') or 0, held_until(self.service, key) if not meta.get('force') else 0)
        if sleeping and failure.get('until', 0) > now:
            retry = max(retry, failure['until'])
            meta['resource_error'] = failure['reason']
        due = meta.get('next_run_at') or meta.get('queued_at', now)
        if meta.get('last_success') and not any(meta.get(k) for k in ('force','resume','continuation','failures')):
            from .frequency_policy import next_run
            # Discovery time is not the deadline. Preserve the real cadence
            # deadline so normalized overdue ordering works after a slow scan.
            due = min(due, next_run(kind,scope,config,platform,meta['last_success'],now) or due)
        if meta.get('await_discovery'):due = math.inf
        if resume:due, retry = now, 0
        pressure = False
        if self.service.store is not None and enabled and not blocked and retry <= now and due <= now and not resume:
            # A filesystem-backed writer lookup belongs to a shop resource,
            # not each of its many plans. Probe at most once per dispatch.
            pressures = resources['pressures'] if resources is not None else {}
            if key[0] not in pressures:
                pressures[key[0]] = bool(self.service.store.writer_for(key[0]).status()['pressure'])
            pressure = pressures[key[0]]
        dependencies = tuple(tuple(k) for k in meta.get('dependency_keys', ())
                             if not getattr(self.service, 'master_steps', {}).get(tuple(k), {}).get('last_success'))
        return Demand(key, kind, due, meta.get('waiting_since', now),
                      max(1, collection.interval_seconds(kind, config, platform, scope)), retry,
                      '' if resume else blocked, True if resume else bool(enabled),
                      True if resume else getattr(item, 'state', 'logged_in') in ('logged_in', 'sleeping'),
                      True if resume else not sleeping, False if resume else pressure,
                      () if resume else dependencies, blocked_code)

    def request_dispatch(self):
        if self.closed:return
        try:loop = asyncio.get_running_loop()
        except RuntimeError:
            if self.owner_loop:self.owner_loop.call_soon_threadsafe(self.request_dispatch)
            return
        self.owner_loop = loop
        if self.timer:self.timer.cancel();self.timer = None
        if self.handle is None:self.handle = loop.call_soon(self.dispatch, context=contextvars.Context())
        # Dispatch never wakes the discovery feeder itself: refill -> dispatch
        # -> refill would form a hot event loop even with no due work.

    @staticmethod
    def spawn(coroutine):
        return asyncio.create_task(coroutine, context=contextvars.Context())

    def _prepare(self, demands, now):
        config = self._config()
        active = {k[0] for k in self.core.reservations} | self.core.preparing_shops
        for demand in sorted(demands, key=lambda d:(self.core.last_turn.get(d.key[0], 0), d.waiting_since, str(d.key))):
            if demand.browser_ready or demand.key[0] in self.preparations:continue
            if demand.key[0] in self.core.yielding:continue
            from dataclasses import replace
            if self.core.base_wait(replace(demand, browser_ready=True), now):continue
            if demand.key[0] not in active and len(active) >= config.get('max_active_shops', 2):continue
            sid = demand.key[0]
            item = self.plans[demand.key][0] if demand.key in self.plans else self.direct[demand.key][1]
            self.core.preparing_shops.add(sid);active.add(sid)
            task = self.spawn(self._wake_browser(sid, item))
            self.preparations[sid] = task

    async def _wake_browser(self, sid, item):
        started = time.monotonic()
        try:
            from .browser_sleep import wake
            await wake(item.source if hasattr(item, 'source') else item)
            if getattr(item, 'state', '') == 'logged_in':
                self.browser_times[sid] = time.monotonic()-started
                self.browser_failures.pop(sid, None)
            else:
                self.browser_failures[sid] = {'until':time.time()+60, 'reason':'浏览器准备后登录尚未就绪，等待验证'}
        except asyncio.CancelledError:raise
        except Exception as error:
            self.browser_failures[sid] = {'until':time.time()+60,
                                         'reason':f'浏览器准备失败（{type(error).__name__}），保留计划与断点'}
        finally:
            self.core.preparing_shops.discard(sid)
            if not any(k[0] == sid for k in self.core.reservations):
                self.core.turn_started.pop(sid,None)
                self.core.yielding.discard(sid)
            self.preparations.pop(sid, None)
            self.request_dispatch()

    def dispatch(self):
        if self.handle:self.handle.cancel();self.handle = None
        if self.dispatching or self.registration_depth or self.closed or getattr(self.service, 'closing', False):return []
        from . import update_gate
        maintenance = update_gate.active()
        self.dispatching = True
        try:
            s = self.service;now = time.time();demands = []
            config = self._config()
            resources = {'config':config, 'pressures':{}}
            for key,(item,meta) in list(self.plans.items()):
                if not maintenance and key not in self.core.reservations and key not in s.jobs:
                    demands.append(self._demand(key,item,meta,now,resources=resources))
            for key,(_,item,meta,future) in list(self.direct.items()):
                if not maintenance and not future.done() and key not in self.core.reservations:
                    demands.append(self._demand(key,item,meta,now,resources=resources))
            from dataclasses import replace
            eligible = {d.key[0] for d in demands if not self.core.base_wait(replace(d,browser_ready=True),now)}
            for sid,task in list(self.preparations.items()):
                if sid not in eligible and not task.cancelling():task.cancel()
            for key, future in list(self.resumes.items()):
                # Maintenance stops new work, not the continuation needed by
                # already-running work to commit its final checkpoint and exit.
                entry = self.plans.get(key) or self.specs.get(key)
                if not future.done() and entry and key in self.core.reservations:
                    demands.append(self._demand(key,*entry,now,resume=True,resources=resources))
            granted, self.waits = self.core.choose(demands, config, now, self.resumes)
            # Ready browsers take their actual execution reservations first.
            # Otherwise each completed wake can be displaced by a newly
            # prepared shop, starving all ready work under the shop limit.
            self._prepare([d for d in demands if d.key not in self.resumes], now)
            by_key = {d.key:d for d in demands}
            self.waits = {key:self.core.wait(by_key[key],config,now,resume=key in self.resumes)
                          for key in self.waits}
            self.waits = {k:w for k,w in self.waits.items() if w is not None}
            for key,wait in self.waits.items():
                entry = self.plans.get(key)
                direct = self.direct.get(key)
                if direct:entry = direct[1], direct[2]
                if entry and key not in s.jobs:
                    item,meta = entry
                    fields = wait.fields()
                    if meta.get('wait_code') != wait.code:
                        meta['wait_reason_since'] = now
                    if fields['unblock_at'] is not None and not math.isfinite(fields['unblock_at']):fields['unblock_at'] = None
                    meta.update(**fields, stage=wait.reason, phase='queued' if direct else 'waiting',
                                database_pressure=wait.code == 'database', browser_blocked=wait.code in ('browser','control'))
                    if not direct:s.deferred[key] = meta
            for key,meta in s.progress.items():
                if key in self.core.reservations:meta['yield_requested'] = key[0] in self.core.yielding
            for allocation in granted:
                key = allocation.key
                if key in self.resumes:
                    future = self.resumes.pop(key)
                    if not future.done():future.set_result(None)
                    continue
                if key in self.direct:
                    task,item,meta,future = self.direct.pop(key)
                    self._own(key, allocation.token, task, meta, item)
                    if not future.done():future.set_result(allocation.token)
                    continue
                item, plan = self.plans[key]
                plan.pop('await_discovery', None)
                from .business import TaskMeta
                meta = TaskMeta({**plan, 'phase':'queued','slot_granted':True,
                                 'execution_phase':'reserved','reading_slot':True,
                                 'execution_token':allocation.token,
                                 'timing_seconds':{'slot_wait':max(0,now-plan.get('waiting_since',now)),
                                                   'browser_start':self.browser_times.pop(key[0],0)}})
                s.deferred.pop(key,None);s.progress[key] = meta
                task = self.spawn(s.sync(item, meta['kind'], meta.get('scope', {}), key[1]))
                s.jobs[key] = task
                self._own(key, allocation.token, task, meta, item)
            return [a.key for a in granted]
        finally:
            self.dispatching = False
            # Resource readiness can change outside the scheduler (login,
            # browser locks, writer pressure). One bounded fallback, never
            # one polling coroutine per waiting plan.
            if not self.closed and self.owner_loop and self.timer is None:
                self.timer = self.owner_loop.call_later(1, self.request_dispatch, context=contextvars.Context())

    def _own(self, key, token, task, meta, item):
        meta['execution_token'] = token
        self.workers[key] = task
        self.specs[key] = (item, meta)
        task.add_done_callback(lambda done:self._done(key, token, meta, done), context=contextvars.Context())

    def _done(self, key, token, meta, task):
        s = self.service
        released = self.core.release(key, token)
        if self.workers.get(key) is task:
            self.workers.pop(key,None)
            self.specs.pop(key,None)
        if not released:return
        if s.jobs.get(key) is task:s.jobs.pop(key, None)
        if s.progress.get(key) is meta:s.progress.pop(key, None)
        self.resumes.pop(key, None)
        entry = self.plans.get(key)
        if entry:
            item,plan = entry
            state = getattr(s,'master_steps',{}).get(key)
            if state and not self.closed:
                from .frequency_policy import next_run
                plan.update({k:v for k,v in state.items() if k not in ('phase','stage')})
                plan.update(force=False, resume=False, await_discovery=True)
                if (state.get('failures') or state.get('continuation')) and meta.get('result') != 'success':
                    plan['next_run_at'] = max(time.time(),state.get('retry_at',0))
                    plan['await_discovery'] = False
                else:
                    plan['next_run_at'] = next_run(plan['kind'],plan.get('scope',{}),s.queue_config,
                                                  plan.get('platform','pdd'),state.get('last_success',time.time()),time.time())
                plan['waiting_since'] = time.time()
            else:plan.update(await_discovery=True, next_run_at=None, force=False)
        if not task.cancelled():task.exception()
        repeat = self.repeat.pop(key,None)
        if repeat and not self.closed:
            item,kind,options = repeat
            self.service.enqueue(item.shop,item,kind,options,force=True)
            pending = s.progress.get(key) or self.plans.get(key,(None,None))[1]
            if pending is not None:pending['deferred_force'] = True
        self.request_dispatch()

    async def enter(self, key, item, meta):
        allocation = self.core.reservations.get(key)
        from .collection_dispatch import browser_blocked
        if allocation and (browser_blocked(item) or getattr(item,'sleeping',False)):
            # A control/login operation may have acquired the browser between
            # reservation and coroutine startup. Return capacity immediately.
            self.core.release(key,allocation.token)
            allocation = None
        if allocation is None:
            future = asyncio.get_running_loop().create_future()
            task = asyncio.current_task()
            self.direct[key] = (task,item,meta,future)
            self.request_dispatch()
            try:token = await future
            finally:self.direct.pop(key,None)
            allocation = self.core.reservations[key]
        self.core.phase(key,allocation.token,'reading')
        meta.update(slot_granted=False, execution_phase='reading', reading_slot=True)
        return Execution(self,key,allocation.token,meta)

    def status(self):
        counts = self.core.counts()
        reasons = Counter(wait.code for wait in self.waits.values())
        from .scheduling_kernel import REASONS
        return {**counts,'plans':len(self.plans),'waiting_reasons':dict(reasons),
                'reason_labels':{code:REASONS[code][0] for code in reasons},
                'limits':{k:self._config().get(k) for k in ('max_concurrent','shop_concurrent','max_active_shops')}}

    async def cancel_shop(self, sid):
        for key in list(self.plans):
            if key[0] == sid:self.plans.pop(key,None);self.service.deferred.pop(key,None)
        task = self.preparations.get(sid)
        if task:task.cancel();await asyncio.gather(task,return_exceptions=True)
        tasks = [task for key,task in list(self.workers.items()) if key[0] == sid]
        for key in self.core.reservations:
            if key[0] == sid:self.core.cancel(key)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.request_dispatch()

    async def close(self):
        self.closed = True
        if self.handle:self.handle.cancel();self.handle = None
        if self.timer:self.timer.cancel();self.timer = None
        tasks = list(self.preparations.values()) + list(self.workers.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        waiting = [entry[0] for entry in self.direct.values()]
        for task,item,meta,future in self.direct.values():future.cancel()
        await asyncio.gather(*waiting,return_exceptions=True)
        self.direct.clear();self.plans.clear()
