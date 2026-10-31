"""Bounded, fair per-shop upload rounds; acknowledgements never share a shop database."""

import asyncio, time, sqlite3, logging
from contextlib import closing
from pathlib import Path
from ..collector_update import read_json
from .replication_worker import Delivery, ReceiptCache
from .order_delivery import OrderDelivery
from .delivery_state import save_state

LOG = logging.getLogger("ecmp.agent")


class ShopDeliveryGroup:
    def __init__(self, directory, lane, identity, receipts=None):
        self.directory = Path(directory)
        self.lane = lane
        self.identity = identity
        self.children = {}
        self.wake = asyncio.Event()
        # Network upload slots are independent from browser collection limits.
        # A slow store occupies only one slot; each store still sends in order.
        self.upload_slots = 3
        self.receipts = receipts or ReceiptCache()
        self._aggregate_state = None
        self.woken = set()
        self.wake_all = False
        self._discovered = None
        self._discovered_at = 0

    def notify(self, shop=None):
        """Wake only the shop that published new data (None: every shop)."""
        if shop is None:
            self.wake_all = True
        else:
            self.woken.add(str(shop))
        self.wake.set()

    def fingerprint(self):
        stamps = []
        for name in ("shop-storage.json", "shops.sqlite3", "shops.sqlite3-wal"):
            try:
                info = (self.directory / name).stat()
                stamps.append((name, info.st_mtime_ns, info.st_size))
            except OSError:
                stamps.append((name, 0, 0))
        return tuple(stamps)

    def discover(self, force=False):
        # Re-reading the manifest and shop list every 5 s found the same shops
        # again; rediscover only when those files change (or once a minute).
        stamp = self.fingerprint()
        if not force and stamp == self._discovered and time.monotonic() - self._discovered_at < 60:
            return False
        self._discovered, self._discovered_at = stamp, time.monotonic()
        state = read_json(self.directory / "shop-storage.json", {})
        with closing(sqlite3.connect(self.directory / "shops.sqlite3")) as db:
            live = {str(r[0]) for r in db.execute("SELECT id FROM shops")}
        paths = {
            sid: self.directory / value["path"]
            for sid, value in state.get("shops", {}).items()
            if value.get("state") == "active" and sid in live
        }
        for sid, path in paths.items():
            if sid not in self.children:
                self.children[sid] = (
                    OrderDelivery(path.parent)
                    if self.lane == "orders"
                    else Delivery(path.parent, self.lane, self.identity)
                )
        self.children = {sid: child for sid, child in self.children.items() if sid in paths}
        return True

    async def tick(self, child, client):
        now = time.time()
        if self.lane == "orders":
            if child.summary.get("retry_at", 0) > now:
                return False
            batch = await asyncio.to_thread(child.batch)
            if not batch:
                return False
            if time.monotonic() - child.checked >= 60:
                result = await client.get("/agent/order-receipt", params={"stream": child.stream})
                result.raise_for_status()
                child.ack = int(result.json()["revision"])
                if child.ack > child.high:
                    from ..async_database import run

                    await run(child.restore_stream)
                child.checked = time.monotonic()
                batch = await asyncio.to_thread(child.batch)
            if batch["rows"]:
                await child.send(client, batch)
            child.summary.update(error="", failed=0, retry_at=0)
            await asyncio.to_thread(child.save)
            return bool(batch["rows"])
        else:
            try:
                snapshot = await self.receipts.get(client)
                if snapshot and getattr(child, "receipt_version", None) != snapshot[0]:
                    await asyncio.to_thread(child.apply_receipts, snapshot[2], snapshot[1])
                    child.receipt_version = snapshot[0]
            except Exception as error:
                LOG.warning("中心接收记录暂不可用，使用已存确认记录继续回传：%s", type(error).__name__)
            rows = await asyncio.to_thread(child.collect)
            await child.send(client, rows)
            await asyncio.to_thread(child.save)
            return bool(rows)

    async def tick_safe(self, child, client):
        try:
            return await self.tick(child, client)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.lane == "orders":
                child.summary.update(
                    error="订单回传暂未确认：" + type(error).__name__, failed=1, retry_at=time.time() + 30
                )
            else:
                child.error = "店铺回传暂未确认：" + type(error).__name__
            try:
                await asyncio.to_thread(child.save)
            except OSError, ValueError:
                LOG.warning("店铺上传状态暂未保存，下一轮重试", exc_info=True)
            return False

    def aggregate(self):
        # Active children own the authoritative counters. Reading their JSON
        # during atomic replacement races Windows file sharing and loses status.
        states = [child.summary if self.lane == "orders" else child.snapshot() for child in self.children.values()]
        state = dict(
            per_shop=True,
            pending=sum(s.get("pending", 0) for s in states),
            oldest_pending=min((s["oldest_pending"] for s in states if s.get("oldest_pending")), default=None),
            failed=sum(s.get("failed", 0) for s in states),
            rejected_count=sum(s.get("rejected_count", 0) for s in states),
            rejected_error="；".join(dict.fromkeys(s["rejected_error"] for s in states if s.get("rejected_error"))),
            error="；".join(dict.fromkeys(s["error"] for s in states if s.get("error"))),
            last_success=max((s.get("last_success", 0) for s in states), default=0),
        )
        if state != self._aggregate_state and save_state(
            self.directory / f"center-delivery-{self.lane}.json", {**state, "updated": time.time()}
        ):
            self._aggregate_state = state

    async def run(self, client):
        active = {}
        ready_at = {}
        empty = {}
        rewake = set()
        discover_at = 0
        aggregate_at = 0
        wake_task = asyncio.create_task(self.wake.wait())
        try:
            while True:
                now = time.monotonic()
                if now >= discover_at:
                    await asyncio.to_thread(self.discover)
                    discover_at = now + 5
                    ready_at = {sid: stamp for sid, stamp in ready_at.items() if sid in self.children}
                for sid, task in list(active.items()):
                    if task.done():
                        # Continue backlogs promptly without holding another
                        # store behind this store's network request.
                        # Empty scans of an idle shop back off (5 → 10 → 20 → 30 s);
                        # its own publication hint still wakes it immediately.
                        idle = 0 if task.result() or sid in rewake else empty.get(sid, 0) + 1
                        rewake.discard(sid)
                        empty[sid] = idle
                        ready_at[sid] = now + (0.2 if not idle else min(30, 5 * 2 ** min(3, (idle - 1) // 3)))
                        del active[sid]
                if wake_task.done():
                    self.wake.clear()
                    # A bare wake (no shop named) keeps the old wake-everyone meaning.
                    for sid in list(self.children) if self.wake_all or not self.woken else self.woken:
                        # Only the publishing shop skips its idle wait.
                        ready_at.pop(sid, None)
                        empty.pop(sid, None)
                        if sid in active:
                            rewake.add(sid)  # published during its running scan
                    if self.woken - set(self.children):
                        discover_at = 0  # a new shop published before discovery saw it
                    self.wake_all = False
                    self.woken.clear()
                    wake_task = asyncio.create_task(self.wake.wait())
                waiting = sorted(
                    (sid for sid in self.children if sid not in active), key=lambda sid: ready_at.get(sid, 0)
                )
                for sid in waiting:
                    if len(active) >= self.upload_slots:
                        break
                    if ready_at.get(sid, 0) > now:
                        break
                    active[sid] = asyncio.create_task(self.tick_safe(self.children[sid], client))
                if now >= aggregate_at:
                    try:
                        await asyncio.to_thread(self.aggregate)
                    except OSError, ValueError:
                        LOG.warning("上传状态汇总暂不可用，上传继续，下一轮重新汇总", exc_info=True)
                    aggregate_at = now + 5
                deadlines = [discover_at, aggregate_at]
                if len(active) < self.upload_slots:
                    deadlines.extend(ready_at.get(sid, 0) for sid in self.children if sid not in active)
                await asyncio.wait(
                    [wake_task, *active.values()],
                    timeout=max(0.01, min(deadlines) - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
        finally:
            wake_task.cancel()
            for task in active.values():
                task.cancel()
            await asyncio.gather(wake_task, *active.values(), return_exceptions=True)
