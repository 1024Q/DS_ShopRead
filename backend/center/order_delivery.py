"""Coalesced order changes; commit receipts only after an atomic center transaction."""

import asyncio, json, time, uuid
from pathlib import Path
from .replication import connect
from .delivery_state import save_state


class OrderDelivery:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.wake = asyncio.Event()
        self.file = self.directory / "center-delivery-orders.json"
        self.summary = {}
        self.ack = 0
        self.stream = ""
        self.checked = 0
        self.database = self.directory / (
            "business.sqlite3" if (self.directory / "business.sqlite3").exists() else "shops.sqlite3"
        )

    def batch(self):
        with connect(self.database) as db:
            clock = db.execute("SELECT stream,value FROM order_clock WHERE id=1").fetchone()
            if not clock:
                return None
            stream, self.high = clock
            if stream != self.stream:
                self.stream = stream
                self.ack = 0
            rows = db.execute(
                "SELECT revision,shop_id,cache_key,id,payload,observed FROM order_entities WHERE revision>? ORDER BY revision LIMIT 200",
                (self.ack,),
            ).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM order_entities WHERE revision>?", (self.ack,)).fetchone()[0]
        self.summary["pending"] = pending
        return {
            "stream": stream,
            "rows": [
                {
                    "revision": r[0],
                    "shop": str(r[1]),
                    "scope": r[2],
                    "id": r[3],
                    "payload": json.loads(r[4]) if r[4] else None,
                    "observed": r[5],
                }
                for r in rows
            ],
        }

    def restore_stream(self):
        # A local database restored behind the center needs a new revision domain.
        # Keep order identities and observations; do not overwrite newer center rows.
        from ..database_writer import connection

        with connection(self.database, immediate=True) as db:
            clock = db.execute("SELECT stream,value FROM order_clock WHERE id=1").fetchone()
            if clock[0] != self.stream or clock[1] >= self.ack:
                return
            stream = uuid.uuid4().hex
            db.execute("UPDATE order_clock SET stream=? WHERE id=1", (stream,))
            db.execute(
                "UPDATE business_cache SET payload=json_set(payload,'$._order_stream',?),updated=? WHERE cache_key LIKE 'orders:view-v2:%'",
                (stream, time.time()),
            )
        self.stream = stream
        self.ack = 0

    async def send(self, client, batch):
        sent_at = time.monotonic()
        response = await client.post("/agent/order-changes", json=batch, timeout=60)
        response.raise_for_status()
        confirmed = int(response.json()["revision"])
        if confirmed < batch["rows"][-1]["revision"]:
            raise ValueError("中心未确认完整订单批次")
        self.ack = confirmed
        self.summary.update(last_success=time.time(), pending=max(0, self.summary["pending"] - len(batch["rows"])))
        from ..collection_metrics import delivery
        scopes = {}
        for row in batch['rows']:
            if not all(k in row for k in ('shop', 'scope', 'observed')):
                continue  # Diagnostic fields never determine upload success.
            key = (row['shop'], row['scope'])
            value = scopes.setdefault(key, {'shop': row['shop'], 'scope': row['scope'], 'updated': row['observed']})
            value['updated'] = max(value['updated'], row['observed'])
        await asyncio.to_thread(delivery, self.directory, 'orders', list(scopes.values()), time.monotonic() - sent_at)

    def save(self):
        save_state(
            self.file, {**self.summary, "confirmed_revision": self.ack, "stream": self.stream, "updated": time.time()}
        )

    async def run(self, client):
        failures = 0
        checked = 0
        while True:
            try:
                self.wake.clear()
                batch = await asyncio.to_thread(self.batch)
                if not batch:
                    await asyncio.sleep(5)
                    continue
                if time.monotonic() - checked >= 60:
                    result = await client.get("/agent/order-receipt", params={"stream": self.stream})
                    result.raise_for_status()
                    self.ack = int(result.json()["revision"])
                    if self.ack > self.high:
                        await asyncio.to_thread(self.restore_stream)
                    checked = time.monotonic()
                    batch = await asyncio.to_thread(self.batch)
                if batch["rows"]:
                    await self.send(client, batch)
                failures = 0
                self.summary.update(error="", failed=0, retry_at=0)
                await asyncio.to_thread(self.save)
                try:
                    await asyncio.wait_for(self.wake.wait(), 0.2 if batch["rows"] else 5)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                delay = min(300, 5 * 2 ** min(failures, 6))
                self.summary.update(
                    error="订单回传暂未确认：" + type(error).__name__, failed=1, retry_at=time.time() + delay
                )
                await asyncio.to_thread(self.save)
                await asyncio.sleep(delay)
