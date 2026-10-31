"""Independent durable replication lanes, acknowledged by the center per snapshot."""

import asyncio
import json
import logging
import time
from .replication import encode
from .delivery_outbox import Outbox
from ..collector_update import read_json
from .delivery_state import save_state
from ..audit_store import clean_message

LOG = logging.getLogger("ecmp.agent")


class ReceiptCache:
    """One receipt snapshot per node, shared by the business and auxiliary lanes."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_check = 0
        self.version = 0
        self.snapshot = None

    async def get(self, client):
        async with self.lock:
            if time.monotonic() < self.next_check:
                return self.snapshot
            started = time.time()
            try:
                response = await client.get("/agent/receipts")
                if response.status_code in (404, 405):
                    self.next_check = time.monotonic() + 300
                    return self.snapshot
                response.raise_for_status()
                value = response.json()
                if not isinstance(value.get("records"), list):
                    raise ValueError("中心回执格式无效")
            except Exception:
                self.next_check = time.monotonic() + 30
                raise
            self.version += 1
            self.snapshot = (self.version, started, value)
            self.next_check = time.monotonic() + 300
            return self.snapshot


def idle_wait(empty_scans):
    if not empty_scans:
        return 0.2
    return min(20, 5 * 2 ** min(2, max(0, empty_scans - 3) // 3))


class Delivery:
    def __init__(self, directory, lane, identity):
        self.directory = directory
        self.lane = lane
        self.identity = identity
        self.database = directory / (
            "business.sqlite3" if (directory / "business.sqlite3").exists() else "shops.sqlite3"
        )
        self.file = directory / f"center-delivery-{lane}.json"
        saved = read_json(self.file, {})
        self.acks = saved.get("acks", {}) if saved.get("identity") == identity else {}
        self.failures = saved.get("retries", {}) if saved.get("identity") == identity else {}
        self.confirmed = saved.get("confirmed", {}) if saved.get("identity") == identity else {}
        self.inflight = {}
        self.wake = asyncio.Event()
        self.ignored = set()
        self.pending = 0
        self.pending_since = saved.get("oldest_pending") if saved.get("identity") == identity else None
        self.last_success = saved.get("last_success", 0) if saved.get("identity") == identity else 0
        self.error = ""
        self.checked = 0
        self.outbox = None
        self.legacy = saved
        self.receipts_pending = None
        self.rejected = {}
        self._saved_state = None

    @staticmethod
    def key(shop, scope):
        return json.dumps([str(shop), scope], ensure_ascii=False, separators=(",", ":"))

    def ensure_outbox(self):
        if self.outbox is None and self.database.exists():
            self.outbox = Outbox(self.database, self.lane, self.identity, self.legacy)
            for shop, scope, ack, confirmed, attempts, retry_at, error, rejected, updated in self.outbox.state():
                key = self.key(shop, scope)
                if ack >= 0:
                    self.acks[key], self.confirmed[key] = ack, confirmed
                if attempts:
                    value = dict(attempts=attempts, retry_at=retry_at, error=error, updated=updated)
                    (self.rejected if rejected else self.failures)[key] = value
            if self.receipts_pending:
                self.outbox.reconcile(*self.receipts_pending)
                self.receipts_pending = None
        return self.outbox

    def collect(self, now=None):
        now = time.time() if now is None else now
        outbox = self.ensure_outbox()
        candidates = outbox.rows(now, self.ignored)
        self.pending, rejected_count = outbox.counts()
        dates = [v for v in (self.pending_since, getattr(outbox, 'oldest_pending', None)) if v is not None]
        self.pending_since = min(dates) if dates and (self.pending or rejected_count) else None
        rows, size = [], 0
        for row in candidates:
            key = self.key(row["shop"], row["scope"])
            if self.failures.get(key, {}).get("updated") != row["updated"]:
                self.failures.pop(key, None)
            # A new source version releases a permanently rejected record.
            if self.rejected.get(key, {}).get("updated") != row["updated"]:
                self.rejected.pop(key, None)
            length = len(row["payload"].encode("utf-8"))
            if rows and size + length > 2 * 1024 * 1024:
                break
            rows.append(row)
            size += length
        return rows

    def commit(self, rows):
        outbox = self.ensure_outbox()
        if outbox:
            outbox.commit(rows, time.time())
        for row in rows:
            key = self.key(row["shop"], row["scope"])
            self.acks[key] = row["updated"]
            self.confirmed[key] = time.time()
            self.failures.pop(key, None)
            self.rejected.pop(key, None)
            self.inflight.pop(key, None)
        self.pending = max(0, self.pending - len(rows))
        if not self.pending and not self.rejected:self.pending_since = None
        self.last_success = time.time()
        self.error = next((v["error"] for v in self.failures.values()), "")
        self.save()

    def fail(self, rows, error):
        if hasattr(error, "response") and error.response is not None:
            reason = f"中心回传 HTTP {error.response.status_code}"
        else:
            reason = type(error).__name__ + "：" + clean_message(error)
        # Credential errors belong to the node, not to a record, and remain
        # recoverable after authorization is repaired without recollection.
        permanent = getattr(getattr(error, "response", None), "status_code", 0) in (400, 413, 422)
        failures = []
        for row in rows:
            key = self.key(row["shop"], row["scope"])
            self.inflight.pop(key, None)
            attempts = self.failures.get(key, {}).get("attempts", 0) + 1
            self.failures[key] = {
                "attempts": attempts,
                "retry_at": time.time() + min(300, 5 * 2 ** min(attempts, 6)),
                "error": reason,
                "updated": row["updated"],
            }
            failures.append(self.failures[key])
            if permanent and attempts >= 3:
                self.rejected[key] = self.failures.pop(key)
                LOG.error(
                    "中心拒收记录已隔离 · shop=%s · scope=%s · %s；数据保留本地，数据更新后自动重试",
                    row["shop"],
                    row["scope"],
                    reason,
                )
        if self.ensure_outbox():
            self.outbox.fail(rows, failures, permanent)
            self.pending, _ = self.outbox.counts()
        self.error = next((v["error"] for v in self.failures.values()), "")
        self.save()
        from ..collection_metrics import delivery
        delivery(self.directory, self.lane, rows, 0, state='failed', reason=reason)

    def snapshot(self):
        return {
            "identity": self.identity,
            **({"acks": self.acks, "confirmed": self.confirmed} if not self.outbox else {"outbox": True}),
            "pending": self.pending,
            "oldest_pending": self.pending_since,
            "retries": self.failures,
            "rejected": self.rejected,
            "rejected_count": len(self.rejected),
            "rejected_error": "；".join(dict.fromkeys(v["error"] for v in self.rejected.values())),
            "failed": len(self.failures),
            "last_success": self.last_success,
            "error": self.error,
            "retry_at": min((v["retry_at"] for v in self.failures.values()), default=0),
        }

    def save(self):
        state = self.snapshot()
        encoded = json.dumps(state, sort_keys=True)
        if encoded == self._saved_state:
            return
        if save_state(self.file, {**state, "updated": time.time()}):
            self._saved_state = encoded

    async def send(self, client, rows):
        if not rows:
            return
        self.inflight = {
            self.key(r["shop"], r["scope"]): {"updated": r["updated"], "started_at": time.time()} for r in rows
        }
        from .snapshot_freshness import prepare
        sent_at = time.monotonic()
        from ..collection_metrics import delivery
        await asyncio.to_thread(delivery, self.directory, self.lane, rows, 0, state='uploading', reason='向中心发送中，等待确认')

        wire = await asyncio.to_thread(prepare, self.ensure_outbox(), rows)
        try:
            response = await client.post(
                "/agent/snapshots", content=encode(wire), headers={"Content-Type": "application/json"}, timeout=120
            )
            response.raise_for_status()
        except Exception as error:
            # Isolate rejected records. Network/5xx failures back off this batch
            # but later keys and the other lane continue independently.
            code = getattr(getattr(error, "response", None), "status_code", 0)
            if len(rows) > 1 and code in (400, 413, 422):
                for row in rows:
                    await self.send(client, [row])
            else:
                await asyncio.to_thread(self.fail, rows, error)
            return
        await asyncio.to_thread(self.commit, rows)
        from ..collection_metrics import delivery
        await asyncio.to_thread(delivery, self.directory, self.lane, rows, time.monotonic() - sent_at)

    async def reconcile(self, client):
        response = await client.get("/agent/receipts")
        if response.status_code in (404, 405):
            return
        response.raise_for_status()
        # The center is authoritative: a restore that lost rows must cause re-send.
        await asyncio.to_thread(self.apply_receipts, response.json())

    def apply_receipts(self, value, observed_at=None):
        self.ignored = {str(sid) for sid in value.get("ignored_shops", [])}
        receipts = {
            self.key(r["shop"], r["scope"]): r["updated"]
            for r in value["records"]
            if self.database.name != "business.sqlite3" or str(r["shop"]) == self.directory.name
        }
        content_receipts = dict(receipts)
        # A heartbeat is valid only alongside the original content receipt. A
        # center restore losing that content must resend the complete snapshot.
        for r in value["records"]:
            if r["scope"].startswith("freshness:"):
                key = self.key(r["shop"], r["scope"][10:])
                if key in receipts:
                    receipts[key] = max(receipts[key], r["updated"])
        # A shared snapshot can predate a concurrent successful send. Never
        # replace a newer acknowledgement with that older cached response.
        if observed_at is not None:
            receipts.update(
                {key: stamp for key, stamp in self.acks.items() if self.confirmed.get(key, 0) >= observed_at}
            )
        for key, stamp in receipts.items():
            if self.acks.get(key) != stamp or key not in self.confirmed:
                self.confirmed[key] = time.time()
        self.acks = receipts
        observed = time.time() if observed_at is None else observed_at
        if self.outbox:
            self.outbox.reconcile(receipts, observed, content_receipts)
        else:
            self.receipts_pending = (receipts, observed, content_receipts)

    async def run(self, client):
        while True:
            try:
                self.wake.clear()
                if time.monotonic() - self.checked > 300:
                    self.checked = time.monotonic()
                    try:
                        await self.reconcile(client)
                    except Exception as error:
                        LOG.warning("中心接收记录暂不可用，使用已存确认记录继续回传：%s", type(error).__name__)
                rows = await asyncio.to_thread(self.collect)
                await self.send(client, rows)
                await asyncio.to_thread(self.save)
                # Publications wake this loop at once. Without them, consecutive
                # empty scans back off (5 → 10 → 20 s) instead of rescanning.
                self.idle_scans = 0 if rows else getattr(self, "idle_scans", 0) + 1
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=idle_wait(self.idle_scans))
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.error = type(error).__name__ + "：" + clean_message(error)
                LOG.warning("%s 回传暂不可用：%s", self.lane, self.error)
                try:
                    self.save()
                except OSError:
                    pass
                await asyncio.sleep(5)


def status(directory):
    result = {}
    for lane in ("business", "auxiliary", "orders"):
        try:
            saved = read_json(directory / f"center-delivery-{lane}.json", {})
            result[lane] = {
                k: v for k, v in saved.items() if k not in ("identity", "acks", "retries", "confirmed", "inflight")
            }
        except OSError, ValueError:
            result[lane] = {}
    return result
