"""Independent bridge loops: local work never stalls the online heartbeat."""

import asyncio
import logging

LOG = logging.getLogger("ecmp.agent")


async def periodic(action, interval, label):
    while True:
        start = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(action(), 8 if label == "心跳" else 45)
        except Exception as error:
            LOG.warning("%s暂不可用（%s），按独立周期继续", label, type(error).__name__)
        await asyncio.sleep(max(0.1, interval - (asyncio.get_running_loop().time() - start)))


async def jobs_loop(action, wake):
    wake.set()
    while True:
        try:
            await asyncio.wait_for(wake.wait(), 30)
        except TimeoutError:
            pass
        wake.clear()
        try:
            await action()
        except Exception as error:
            LOG.warning("任务下发暂不可用（%s），5 秒后重试", type(error).__name__)
            await asyncio.sleep(5)
            wake.set()
