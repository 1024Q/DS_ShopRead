"""Finite shop turns now yield at safe boundaries, without frozen queues."""
import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from backend import collection
from backend.business import BusinessService, TaskMeta
from backend.frequency_policy import migrate
from backend.scheduler import Scheduler


class FiniteTurnExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service=BusinessService(None,None)
        self.service.queue_config={**migrate(copy.deepcopy(collection.DEFAULTS)),
            'max_concurrent':20,'shop_concurrent':4,'max_active_shops':2,'batch_seconds':60}
        self.jobs=[]

    async def asyncTearDown(self):
        for job in self.jobs:job.cancel()
        await asyncio.gather(*self.jobs,return_exceptions=True)
        await self.service.close()

    async def test_late_same_shop_step_cannot_extend_turn_when_sleeping_peer_is_due(self):
        s=self.service;s.queue_config['max_active_shops']=1
        entered={};releases={}
        def start(sid,key,kind):
            identity=(sid,key);entered[identity]=asyncio.Event();releases[identity]=asyncio.Event()
            item=SimpleNamespace(lock=asyncio.Lock(),shop={'id':sid})
            async def work():
                async with s.slot(identity,item,kind):
                    entered[identity].set();await releases[identity].wait()
            job=asyncio.create_task(work());self.jobs.append(job);return job
        start(1,'orders','orders');await asyncio.wait_for(entered[1,'orders'].wait(),1)
        s.due_plans.core.turn_started[1]-=61
        start(1,'late','overview');start(2,'peer','overview')
        await asyncio.sleep(.02)
        self.assertIn(1,s.due_plans.core.yielding)
        self.assertFalse(entered[1,'late'].is_set())
        releases[1,'orders'].set()
        await asyncio.wait_for(entered[2,'peer'].wait(),1)
        self.assertFalse(entered[1,'late'].is_set())

    async def test_clear_queue_during_preparation_drains_browsers_without_creating_readers(self):
        from backend.queue_control import clear
        s=self.service;entered=[asyncio.Event(),asyncio.Event()];drained=[]
        async def wake(item):
            entered[item.shop['id']-1].set()
            try:await asyncio.Event().wait()
            finally:drained.append(item.shop['id'])
        for sid in (1,2):
            item=SimpleNamespace(lock=asyncio.Lock(),shop={'id':sid},state='sleeping',sleeping=True,retired=False)
            s.due_plans.add((sid,'overview'),item,TaskMeta(kind='overview',scope={},force=True,queued_at=time.time()))
        with patch('backend.browser_sleep.wake',side_effect=wake):
            s.due_plans.dispatch()
            await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered)),1)
            self.assertEqual(s.due_plans.core.counts()['reading'],0)
            self.assertEqual(s.due_plans.core.counts()['preparing_shops'],2)
            result=await clear(s)
        self.assertEqual(result['count'],2);self.assertEqual(set(drained),{1,2})
        self.assertFalse(s.jobs);self.assertFalse(s.due_plans.core.preparing_shops)

    async def test_update_gate_does_not_admit_a_pending_continuation(self):
        s=self.service;scheduler=Scheduler(s)
        shop={'id':1,'platform':'pdd'};item=SimpleNamespace(state='logged_in',retired=False)
        scheduler.pending=[(shop,item,'orders',{},dict(continuation=True,waiting_since=1))]
        with patch('backend.update_gate.active',return_value=True):await scheduler.refill()
        self.assertEqual(len(scheduler.pending),1);self.assertFalse(s.jobs)
