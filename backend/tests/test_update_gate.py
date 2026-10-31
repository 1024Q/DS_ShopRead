import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from backend import update_gate
from backend.business import BusinessService
from backend.scheduler import Scheduler
from backend.invoice_upload import UploadWorker


class GateTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        update_gate.release()

    async def test_pending_work_cannot_start_until_lease_expires(self):
        service = BusinessService(None, None)
        from backend.tests.collection_fixture import policy

        service.configure_queue(policy())
        item = SimpleNamespace(lock=asyncio.Lock())
        key = (1, "orders")
        entered = asyncio.Event()

        async def job():
            async with BusinessService.slot(service, key, item, "orders"):
                entered.set()

        with (
            patch("backend.business.queue_for", return_value="realtime"),
        ):
            update_gate.renew(3)
            task = asyncio.create_task(job())
            try:
                await asyncio.sleep(0.03)
                self.assertFalse(entered.is_set())
                self.assertEqual(service.running_shops, set())
                update_gate.deadline = 1
                service.wake.set()
                await asyncio.wait_for(task, 1)
                self.assertTrue(entered.is_set())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await service.close()

    async def test_scheduler_and_upload_do_not_start_more_work(self):
        update_gate.renew()
        await Scheduler(SimpleNamespace()).tick()
        with patch("backend.invoice_upload.ledger.get_file") as get:
            await UploadWorker(None, None).process("file")
            get.assert_not_called()

    async def test_blockers_keep_active_upload_and_remote_control_safe(self):
        lock = asyncio.Lock()
        await lock.acquire()
        item = SimpleNamespace(lock=lock, remote_control_active=True)
        self.assertEqual(
            update_gate.blockers(SimpleNamespace(running_shops={1, 2}), [item]),
            {"collecting": 2, "busy_windows": 1, "remote_controls": 1},
        )
        update_gate.renew()
        self.assertTrue(update_gate.active())
        update_gate.release()
        self.assertFalse(update_gate.active())

    async def test_default_lease_is_short_so_a_dead_updater_cannot_pause_for_minutes(self):
        update_gate.renew()
        self.assertLessEqual(update_gate.deadline - time.monotonic(), 30)
