import asyncio
import copy
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.scheduling_kernel import Demand, SchedulingKernel
from backend.business import BusinessService, TaskMeta
from backend import collection
from backend.frequency_policy import migrate
from backend.async_database import run


def demand(sid, scope='a', kind='orders', **kwargs):
    return Demand((sid, scope), kind, kwargs.pop('due_at', 0), kwargs.pop('waiting_since', 0), **kwargs)


class KernelTests(unittest.TestCase):
    def setUp(self):
        self.core = SchedulingKernel()
        self.config = dict(max_concurrent=2, shop_concurrent=2, max_active_shops=2, batch_seconds=60)

    def test_one_atomic_reservation_and_business_mutex_includes_database_and_cancellation(self):
        a, b = demand(1), demand(1, 'b')
        grants, waits = self.core.choose([a,b], self.config, 10)
        self.assertEqual(len(grants), 1)
        token = grants[0].token
        self.core.phase(a.key, token, 'database')
        self.core.cancel(a.key)
        grants, waits = self.core.choose([b,demand(2)], self.config, 11)
        self.assertEqual([r.key for r in grants], [(2,'a')])
        self.assertEqual(waits[b.key].code, 'module')
        self.assertFalse(self.core.release(a.key, token+1))
        self.assertTrue(self.core.release(a.key, token))
        self.assertEqual([r.key for r in self.core.choose([b],self.config,12)[0]], [b.key])

    def test_round_robin_prevents_many_overdue_failures_buying_all_turns(self):
        self.config['max_concurrent'] = 1
        selected = []
        for now in range(1,16):
            demands = [demand(1,str(i),due_at=-10000) for i in range(100)] + [demand(2),demand(3)]
            grant = self.core.choose(demands,self.config,now)[0][0]
            selected.append(grant.key[0]);self.core.release(grant.key,grant.token)
        self.assertEqual(selected,[1,2,3]*5)

    def test_within_shop_more_overdue_first_and_lowering_limits_does_not_evict(self):
        old, recent = demand(1, 'old',due_at=-100), demand(1,'recent')
        grants, waits = self.core.choose([recent,old,demand(2)],self.config,10)
        self.assertEqual(grants[0].key, old.key)
        self.config['max_concurrent'] = 1
        self.assertFalse(self.core.choose([demand(3)],self.config,11)[0])
        self.assertEqual(len(self.core.reservations),2)

    def test_sleeping_peer_triggers_segment_yield_without_cancelling_current_reader(self):
        self.config['max_active_shops'] = 1
        a=demand(1);self.core.choose([a],self.config,0)
        peer=demand(2,browser_ready=False)
        _,waits=self.core.choose([peer,demand(1,'b','overview')],self.config,61)
        self.assertEqual(self.core.yielding,{1})
        self.assertEqual(waits[1,'b'].code,'turn')
        self.assertIn(a.key,self.core.reservations)

    def test_unopened_shop_reports_capacity_instead_of_a_nonexistent_browser_recovery(self):
        self.core.preparing_shops.update((1,2))
        _,waits=self.core.choose([demand(1,browser_ready=False),demand(3,browser_ready=False)],self.config,10)
        self.assertEqual(waits[1,'a'].code,'browser')
        self.assertEqual(waits[3,'a'].code,'shops')

    def test_slow_browser_preparation_does_not_spend_the_execution_turn(self):
        self.config['max_active_shops']=1
        self.core.preparing_shops.add(1)
        self.core.choose([demand(1,browser_ready=False),demand(2,browser_ready=False)],self.config,0)
        self.core.choose([demand(1,browser_ready=False),demand(2,browser_ready=False)],self.config,90)
        self.assertFalse(self.core.yielding)
        self.assertFalse(self.core.turn_started)
        self.core.preparing_shops.clear()
        grant=self.core.choose([demand(1)],self.config,91)[0]
        self.assertEqual(grant[0].key,(1,'a'))

    def test_wait_reasons_are_disjoint_and_have_release_conditions(self):
        values=[demand(1,enabled=False),demand(2,logged_in=False),demand(3,retry_at=100),
                demand(4,due_at=100),demand(5,database_pressure=True),demand(6,browser_ready=False),
                demand(7,dependencies=((1,'x'),))]
        grants,waits=self.core.choose(values,self.config,10)
        self.assertFalse(grants)
        self.assertEqual({w.code for w in waits.values()}, {'paused','login','retry','not_due','database','browser','dependency'})
        self.assertTrue(all(w.condition for w in waits.values()))

    def test_safe_segment_yield_saves_checkpoint_before_leaving(self):
        from backend.collection_checkpoint import Checkpoint, YieldCollection
        from backend.node_dispatch import current_execution
        cp=Checkpoint.__new__(Checkpoint)
        cp.pages=1;cp.budget=100;cp.started=time.monotonic();cp.seconds=100
        cp.store=SimpleNamespace(writer_for=lambda _:SimpleNamespace(status=lambda:{'pressure':False}));cp.shop=1
        saved=[];cp.save=lambda:saved.append('committed')
        token=current_execution.set(SimpleNamespace(meta={'yield_requested':True}))
        try:
            with self.assertRaises(YieldCollection):cp.check_budget()
        finally:current_execution.reset(token)
        self.assertEqual(saved,['committed'])

    def test_fleet_runtime_counters_sum_without_creating_a_global_limit(self):
        from backend.center.configuration import aggregate_status
        values=[{'collection':{'max_concurrent':n},'dispatch':{'reading':n-1,'database':1,
            'limits':{'max_concurrent':n},'waiting_reasons':{'node':2},'reason_labels':{'node':'等待节点读取名额'}}}
            for n in (3,5)]
        result=aggregate_status({},values)
        self.assertEqual(result['dispatch']['reading'],6)
        self.assertEqual(result['dispatch']['waiting_reasons']['node'],4)
        self.assertEqual(result['dispatch']['limits']['max_concurrent'],8)
        self.assertIsNone(result['collection']['max_concurrent'])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service=BusinessService(None,None)
        self.service.queue_config={**migrate(copy.deepcopy(collection.DEFAULTS)), 'max_concurrent':1,
            'shop_concurrent':2,'max_active_shops':2,'home_paused':False}
        self.tasks=[]

    async def asyncTearDown(self):
        for task in self.tasks:task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.service.close()

    def start(self,sid,key,kind,body):
        item=SimpleNamespace(shop={'id':sid,'platform':'pdd'},state='logged_in',retired=False,lock=asyncio.Lock())
        async def worker():
            async with self.service.slot((sid,key),item,kind):await body()
        task=asyncio.create_task(worker());self.tasks.append(task);return task

    async def test_database_wait_releases_read_slot_but_not_module_then_resumes(self):
        entered=threading.Event();finish=threading.Event();second=asyncio.Event();resume=asyncio.Event()
        def durable():entered.set();finish.wait(3)
        async def first():await run(durable);resume.set()
        async def peer():second.set();await asyncio.sleep(.08)
        a=self.start(1,'today','orders',first)
        while not entered.is_set():await asyncio.sleep(.005)
        self.assertEqual(self.service.due_plans.core.counts()['database'],1)
        b=self.start(2,'other','overview',peer)
        await asyncio.wait_for(second.wait(),1)
        self.assertIn((1,'today'),self.service.due_plans.core.reservations)
        self.assertEqual(self.service.due_plans.core.counts()['reading'],1)
        finish.set();await asyncio.gather(a,b)
        self.assertTrue(resume.is_set())

    async def test_cancel_waits_for_native_commit_before_same_business_can_replace(self):
        entered=threading.Event();finish=threading.Event();commit=[];replacement=asyncio.Event()
        def durable():entered.set();finish.wait(3);commit.append('durable')
        async def first():await run(durable)
        async def later():replacement.set()
        a=self.start(1,'today','orders',first)
        while not entered.is_set():await asyncio.sleep(.005)
        b=self.start(1,'yesterday','orders',later)
        a.cancel();await asyncio.sleep(.03)
        self.assertFalse(a.done());self.assertFalse(replacement.is_set())
        finish.set();await asyncio.gather(a,return_exceptions=True)
        await asyncio.wait_for(b,1)
        self.assertEqual(commit,['durable'])

    async def test_update_drain_allows_existing_database_resume_but_no_new_execution(self):
        entered=threading.Event();finish=threading.Event();resumed=asyncio.Event();peer=asyncio.Event()
        def durable():entered.set();finish.wait(3)
        async def first():await run(durable);resumed.set()
        async def other():peer.set()
        a=self.start(1,'today','orders',first)
        while not entered.is_set():await asyncio.sleep(.005)
        with patch('backend.update_gate.active',return_value=True):
            b=self.start(2,'next','overview',other)
            finish.set()
            await asyncio.wait_for(a,1)
            self.assertTrue(resumed.is_set())
            self.assertFalse(peer.is_set())
            self.assertNotIn((1,'today'),self.service.due_plans.core.reservations)
        self.service.due_plans.request_dispatch()
        await asyncio.wait_for(b,1)
        self.assertTrue(peer.is_set())

    async def test_update_blockers_use_live_reservations_not_stale_shop_hints(self):
        from backend.update_gate import blockers
        s=self.service;s.running_shops.add(99)
        self.assertEqual(blockers(s,[])['collecting'],0)
        s.due_plans.core.choose([demand(1,'one','orders')],s.queue_config,time.time())
        self.assertEqual(blockers(s,[])['collecting'],1)

    async def test_browser_preparation_uses_no_read_capacity_and_stop_drains_it(self):
        s=self.service;started=asyncio.Event();closed=asyncio.Event()
        item=SimpleNamespace(shop={'id':1,'platform':'pdd'},state='sleeping',sleeping=True,retired=False,lock=asyncio.Lock())
        async def wake(_):
            started.set()
            try:await asyncio.Event().wait()
            finally:closed.set()
        s.due_plans.add((1,'x'),item,TaskMeta(kind='overview',scope={},queued_at=time.time(),force=True))
        with patch('backend.browser_sleep.wake',side_effect=wake):
            s.due_plans.dispatch();await asyncio.wait_for(started.wait(),1)
            self.assertFalse(s.jobs);self.assertEqual(s.due_plans.core.counts()['reading'],0)
            await s.cancel_shop(1)
        self.assertTrue(closed.is_set());self.assertFalse(s.due_plans.core.preparing_shops)

    async def test_ready_shop_is_not_displaced_by_starting_another_browser_preparation(self):
        s=self.service;ready=asyncio.Event();reading=asyncio.Event();hold=asyncio.Event();waking=[]
        items={sid:SimpleNamespace(shop={'id':sid,'platform':'pdd'},state='sleeping',sleeping=True,
                                  retired=False,lock=asyncio.Lock()) for sid in range(1,5)}
        async def wake(item):
            waking.append(item.shop['id'])
            if item.shop['id']==1:
                await ready.wait();item.state='logged_in';item.sleeping=False
            else:await asyncio.Event().wait()
        async def sync(item,kind,scope,key):
            async with s.slot((item.shop['id'],key),item,kind):reading.set();await hold.wait()
        s.sync=sync
        with patch('backend.browser_sleep.wake',side_effect=wake):
            for sid,item in items.items():
                s.due_plans.add((sid,'overview'),item,TaskMeta(kind='overview',scope={},force=True,queued_at=time.time()))
            s.due_plans.dispatch();await asyncio.sleep(.01)
            self.assertEqual(waking,[1,2]);ready.set()
            await asyncio.wait_for(reading.wait(),1)
            self.assertNotIn(3,waking)
            self.assertEqual(s.due_plans.core.counts()['reading'],1)
            await s.close()

    async def test_finished_outer_cleanup_retains_module_until_task_exit(self):
        s=self.service;cleaning=asyncio.Event();release=asyncio.Event();other=asyncio.Event()
        item=SimpleNamespace(shop={'id':1,'platform':'pdd'},state='logged_in',retired=False,lock=asyncio.Lock())
        async def sync(item,kind,scope,key):
            async with s.slot((1,key),item,kind):pass
            if key=='first':cleaning.set();await release.wait()
            else:other.set()
        s.sync=sync
        for key in ('first','second'):
            s.due_plans.add((1,key),item,TaskMeta(kind='overview',scope={},queued_at=time.time(),force=True))
        s.due_plans.dispatch();await asyncio.wait_for(cleaning.wait(),1)
        self.assertFalse(other.is_set());self.assertEqual(s.due_plans.core.counts()['finishing'],1)
        release.set();await asyncio.wait_for(other.wait(),1)

    async def test_dispatch_is_not_a_self_waking_discovery_loop(self):
        s=self.service;s.schedule_wake.clear()
        s.due_plans.request_dispatch();await asyncio.sleep(.03)
        self.assertFalse(s.schedule_wake.is_set())

    async def test_writer_resources_are_probed_once_per_shop_and_never_for_future_plans(self):
        from unittest.mock import Mock
        s=self.service;now=time.time()
        writer=SimpleNamespace(status=Mock(return_value={'pressure':False}))
        s.store=SimpleNamespace(writer_for=Mock(return_value=writer))
        resources={'config':s.queue_config,'pressures':{}}
        item=SimpleNamespace(shop={'platform':'pdd'},state='logged_in')
        try:
            for index in range(200):
                s.due_plans._demand((index%2+1,str(index)),item,{'kind':'overview','force':True,'queued_at':now},now,resources=resources)
            self.assertEqual(s.store.writer_for.call_count,2)
            s.due_plans._demand((3,'future'),item,{'kind':'overview','next_run_at':now+100},now,resources=resources)
            self.assertEqual(s.store.writer_for.call_count,2)
        finally:s.store=None

    async def test_scanner_batch_selects_once_after_registration_not_once_per_plan(self):
        s=self.service
        item=SimpleNamespace(shop={'id':1},state='logged_in',retired=False,lock=asyncio.Lock())
        s.queue_config['home_paused']=True
        with patch.object(s.due_plans.core,'choose',wraps=s.due_plans.core.choose) as choose:
            with s.due_plans.registration_batch():
                for index in range(200):
                    s.due_plans.add((1,str(index)),item,TaskMeta(kind='overview',scope={},queued_at=time.time()))
                    s.due_plans.dispatch()
                self.assertEqual(choose.call_count,0)
            await asyncio.sleep(.02)
            self.assertEqual(choose.call_count,1)
        self.assertEqual(s.due_plans.registration_depth,0)

    async def test_adapter_uses_cadence_deadline_not_discovery_time_for_overdue(self):
        s=self.service;now=time.time();item=SimpleNamespace(state='logged_in',shop={'platform':'pdd'})
        meta={'kind':'overview','scope':{},'last_success':now-1000,'queued_at':now,'next_run_at':now}
        d=s.due_plans._demand((1,'overview'),item,meta,now)
        self.assertEqual(d.due_at,meta['last_success']+collection.interval_seconds('overview',s.queue_config,'pdd',{}))
        self.assertLess(d.due_at,now)

    async def test_page_load_has_separate_clock_from_interface_reading(self):
        from backend.collection_metrics import measured
        from backend.read_budget import ReadBudget
        @measured('page_load')
        async def page():await asyncio.sleep(.08)
        meta={}
        async with ReadBudget(.04,meta):
            await page();await asyncio.sleep(.005)
        self.assertGreater(meta['page_load_seconds'],.07)

    async def test_manual_rerun_keeps_its_claim_marker_even_when_it_must_wait_for_a_shop_place(self):
        s=self.service;key=(1,'overview');item=SimpleNamespace(shop={'id':1})
        meta=TaskMeta(kind='overview',scope={},last_success=time.time(),result='success')
        s.due_plans.core.choose([demand(1,'overview','overview')],s.queue_config,time.time())
        token=s.due_plans.core.reservations[key].token
        old=asyncio.create_task(asyncio.sleep(0));await old
        s.master_steps[key]=dict(meta);s.jobs[key]=old;s.progress[key]=meta
        s.due_plans.plans[key]=(item,dict(meta));s.due_plans.repeat[key]=(item,'overview',{})
        def enqueue(*args,**kwargs):s.due_plans.plans[key]=(item,TaskMeta(kind='overview',scope={},force=True))
        s.enqueue=enqueue
        with patch.object(s.due_plans,'request_dispatch'):
            s.due_plans._done(key,token,meta,old)
        self.assertNotIn(key,s.progress)
        self.assertTrue(s.due_plans.plans[key][1]['deferred_force'])

    async def test_finishing_keeps_actual_followup_step_visible_and_resume_shows_its_wait(self):
        from backend.master_tasks import project
        s=self.service
        meta={'kind':'overview','scope':{},'phase':'running','stage':'核对小额退款（独立操作时限）',
              'execution_phase':'finishing','resource_wait':'读取已结束，等待结果与断点确认'}
        s.progress[(1,'overview')]=meta
        self.assertEqual(project(s)[0]['steps'][0]['stage'],meta['stage'])
        meta.update(execution_phase='resuming',resource_wait='本地入库完成，等待续读名额')
        self.assertEqual(project(s)[0]['steps'][0]['stage'],meta['resource_wait'])
