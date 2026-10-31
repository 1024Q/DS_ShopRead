"""Durable pagination batches; unfinished snapshots never replace saved data."""

import asyncio, contextvars, hashlib, json, time, sqlite3
from contextlib import contextmanager, asynccontextmanager
from .read_errors import PageReadError
from .async_database import run as database_work

current = contextvars.ContextVar("collection_checkpoint", default=None)


class YieldCollection(Exception):
    pass


def initialize(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS collection_checkpoints (shop_id INTEGER,cache_key TEXT,payload TEXT NOT NULL,updated REAL NOT NULL,loaded INTEGER NOT NULL DEFAULT 0,total INTEGER,PRIMARY KEY(shop_id,cache_key))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS checkpoint_rows(shop_id INTEGER,cache_key TEXT,part TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(shop_id,cache_key,part,id))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS checkpoint_ids(shop_id INTEGER,cache_key TEXT,part TEXT,id TEXT,pos INTEGER,PRIMARY KEY(shop_id,cache_key,part,id))"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS checkpoint_cleanup AFTER DELETE ON collection_checkpoints BEGIN DELETE FROM checkpoint_rows WHERE shop_id=OLD.shop_id AND cache_key=OLD.cache_key; DELETE FROM checkpoint_ids WHERE shop_id=OLD.shop_id AND cache_key=OLD.cache_key; END"
    )


def clear(store, shop, key):
    with store.connect() as db:
        db.execute("DELETE FROM collection_checkpoints WHERE shop_id=? AND cache_key=?", (shop, key))
        db.execute("UPDATE collection_tasks SET continuation=0 WHERE shop_id=? AND cache_key=?", (shop, key))


class Checkpoint:
    def __init__(self, store, shop, key, config):
        self.store, self.shop, self.key = store, shop, key
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM collection_checkpoints WHERE shop_id=? AND cache_key=?", (shop, key)
            ).fetchone()
        self.data = json.loads(row[0]) if row else {"started_at": time.time(), "parts": {}}
        self.dirty = {}
        self.persisted = {}
        self.id_counts = {}
        self.reset_parts = set()
        with store.connect() as db:
            for key, part in self.data["parts"].items():
                if self.data.get("storage_version") == 2:
                    part["rows"] = {
                        r["id"]: json.loads(r["payload"])
                        for r in db.execute(
                            "SELECT id,payload FROM checkpoint_rows WHERE shop_id=? AND cache_key=? AND part=?",
                            (shop, self.key, key),
                        )
                    }
                    part["ids"] = [
                        r["id"]
                        for r in db.execute(
                            "SELECT id FROM checkpoint_ids WHERE shop_id=? AND cache_key=? AND part=? ORDER BY pos",
                            (shop, self.key, key),
                        )
                    ]
                    self.id_counts[key] = len(part["ids"])
                    self.persisted[key] = part
                else:
                    self.dirty[key] = set(part.get("rows", {}))
        self.pages = 0
        self.started = time.monotonic()
        self.budget = config.get("batch_pages", 12)
        self.seconds = config.get("batch_seconds", 20)

    def mark(self, key, rows):
        self.dirty.setdefault(key, set()).update(str(r["id"]) for r in rows)

    def save(self):
        # Only replay this atomic transaction, never re-fetch pages or repeat publication.
        for attempt in range(3):
            try:
                return self._save()
            except sqlite3.OperationalError as error:
                if not any(word in str(error).lower() for word in ("locked", "busy")) or attempt == 2:
                    raise
                time.sleep(0.25 * (attempt + 1))

    def _save(self):
        parts = list(self.data["parts"].values())
        loaded = sum(p.get("loaded", 0) for p in parts)
        totals = [p.get("total") for p in parts]
        total = sum(totals) if totals and all(t is not None for t in totals) else None
        meta = {
            **self.data,
            "storage_version": 2,
            "parts": {
                k: {f: v for f, v in p.items() if f not in ("rows", "ids")} for k, p in self.data["parts"].items()
            },
        }
        # Encode outside the write transaction, including large first snapshots.
        removed = set(self.persisted) - set(self.data["parts"])
        batches = []
        for key, part in self.data["parts"].items():
            reset = self.persisted.get(key) is not part
            dirty = set(part["rows"]) if reset else self.dirty.get(key, set())
            rows = [
                (self.shop, self.key, key, i, json.dumps(part["rows"][i], ensure_ascii=False))
                for i in dirty
                if i in part["rows"]
            ]
            start = 0 if reset else self.id_counts.get(key, 0)
            ids = [(self.shop, self.key, key, i, start + n) for n, i in enumerate(part["ids"][start:])]
            batches.append((key, reset, rows, ids))
        encoded = json.dumps(meta, ensure_ascii=False)
        with self.store.write_connection() as db:
            for key in removed | {key for key, reset, _, _ in batches if reset}:
                db.execute(
                    "DELETE FROM checkpoint_rows WHERE shop_id=? AND cache_key=? AND part=?", (self.shop, self.key, key)
                )
                db.execute(
                    "DELETE FROM checkpoint_ids WHERE shop_id=? AND cache_key=? AND part=?", (self.shop, self.key, key)
                )
            for key, reset, rows, ids in batches:
                db.executemany(
                    "INSERT INTO checkpoint_rows(shop_id,cache_key,part,id,payload) VALUES(?,?,?,?,?) ON CONFLICT(shop_id,cache_key,part,id) DO UPDATE SET payload=excluded.payload WHERE checkpoint_rows.payload<>excluded.payload",
                    rows,
                )
                db.executemany(
                    "INSERT OR IGNORE INTO checkpoint_ids(shop_id,cache_key,part,id,pos) VALUES(?,?,?,?,?)", ids
                )
            db.execute(
                "INSERT INTO collection_checkpoints(shop_id,cache_key,payload,updated,loaded,total) VALUES(?,?,?,?,?,?) ON CONFLICT(shop_id,cache_key) DO UPDATE SET payload=excluded.payload,updated=excluded.updated,loaded=excluded.loaded,total=excluded.total",
                (self.shop, self.key, encoded, time.time(), loaded, total),
            )
            db.execute(
                "UPDATE collection_tasks SET continuation=1 WHERE shop_id=? AND cache_key=?", (self.shop, self.key)
            )
        self.persisted = dict(self.data["parts"])
        self.id_counts = {k: len(p["ids"]) for k, p in self.data["parts"].items()}
        self.dirty.clear()

    def clear(self):
        clear(self.store, self.shop, self.key)
        self.data = {"started_at": time.time(), "parts": {}}
        self.persisted = {}
        self.id_counts = {}
        self.dirty = {}

    def check_budget(self):
        from .node_dispatch import current_execution
        execution = current_execution.get()
        yielding = bool(execution and execution.meta.get('yield_requested'))
        if self.pages >= self.budget or (
            self.pages
            and (
                yielding or time.monotonic() - self.started >= self.seconds or self.store.writer_for(self.shop).status()["pressure"]
            )
        ):
            self.save()
            raise YieldCollection()


@contextmanager
def context(store, shop, key, config):
    value = Checkpoint(store, shop, key, config)
    token = current.set(value)
    try:
        yield value
    finally:
        current.reset(token)


@asynccontextmanager
async def async_context(store, shop, key, config):
    value = await database_work(Checkpoint, store, shop, key, config)
    token = current.set(value)
    try:
        yield value
    except asyncio.CancelledError:
        # Also covers cancellation during the inter-page pause or validation.
        await database_work(value.save)
        raise
    finally:
        current.reset(token)


def signature(batch):
    return hashlib.sha256(json.dumps(batch["ids"], ensure_ascii=False).encode()).hexdigest()


async def paginate(part_key, fetch, progress, required_ids=None, *, restart_all=False):
    ctx = current.get()
    key = hashlib.sha256(part_key.encode()).hexdigest()
    part = ctx.data["parts"].get(key)
    if part and part["done"]:
        return result(part)
    # A succession of one-page subqueries is still one worker batch. Previously
    # only page 2+ checked this budget, so multi-status reads could monopolize it.
    if ctx.pages:
        await database_work(ctx.check_budget)
    from .read_budget import current as read_budget

    budget = read_budget.get()

    async def fetch_page(page):
        if budget:
            reserve = min(
                budget.seconds / 2, max(min(5, budget.seconds / 10), getattr(ctx, "last_page_seconds", 0) * 1.25)
            )
            if budget.remaining() <= reserve:
                await database_work(ctx.save)
                raise YieldCollection()
        started = time.monotonic()
        try:
            return await fetch(page)
        finally:
            ctx.last_page_seconds = time.monotonic() - started

    from .request_reuse import fresh

    with fresh(bool(part)):
        first = await fetch_page(1)
    head = signature(first)

    async def restart_snapshot():
        # Historical analytics keep their summary with all completed subparts.
        # If a list shifts, rebuild that entire unfinished snapshot together.
        def reset():
            ctx.clear()
            ctx.save()  # Preserve the pending demand even if its next cadence is not due.

        await database_work(reset)
        raise YieldCollection()

    if part and (part["head"] != head or part["total"] != first["total"]):
        if restart_all:
            await restart_snapshot()
        part = None
    if part and part["next"] > 2 and not part["done"]:
        with fresh():
            previous = await fetch_page(part["next"] - 1)
        if signature(previous) != part["tail"] or previous["total"] != part["total"]:
            if restart_all:
                await restart_snapshot()
            part = None
    if not part:
        ctx.pages += 1
        part = {
            "head": head,
            "tail": "",
            "next": 1,
            "rows": {},
            "ids": [],
            "loaded": 0,
            "total": first["total"],
            "done": False,
        }
        ctx.data["parts"][key] = part
    else:
        part["rows"].update({str(r["id"]): r for r in first["rows"]})
        ctx.mark(key, first["rows"])
    if part["done"]:
        return result(part)
    if part.get("verify_pending"):
        # The last page was saved before yielding near the read deadline.
        # First/tail validation above is enough; never request page N+1.
        part["done"] = True
        part.pop("verify_pending", None)
        await database_work(ctx.save)
        return result(part)
    while True:
        page = part["next"]
        if page > 1:
            await database_work(ctx.check_budget)
            await asyncio.sleep(0.2)
        batch = first if page == 1 else await fetch_page(page)
        if page > 1:
            ctx.pages += 1
        if batch["total"] != part["total"]:
            if restart_all:
                await restart_snapshot()
            ctx.data["restarts"] = ctx.data.get("restarts", 0) + 1
            ctx.data["parts"].pop(key, None)
            await database_work(ctx.save)
            raise YieldCollection()
        seen = set(part["ids"])
        fresh = [i for i in batch["ids"] if i not in seen]
        if not fresh and part["total"] is not None and part["loaded"] < part["total"]:
            await database_work(ctx.save)
            raise PageReadError("列表分页重复或数量变化，已保留断点，稍后核对")
        part["ids"].extend(dict.fromkeys(fresh))
        part["loaded"] = len(part["ids"])
        part["rows"].update({str(r["id"]): r for r in batch["rows"]})
        ctx.mark(key, batch["rows"])
        part["tail"] = signature(batch)
        part["next"] = page + 1
        progress(part["loaded"], part["total"])
        matched = bool(required_ids) and set(required_ids).issubset(part["rows"])
        done = (
            matched
            or (part["total"] is not None and part["loaded"] >= part["total"])
            or (part["total"] is None and (batch.get("has_more") is False or not batch["ids"]))
        )
        if done:
            if page == 1:
                part["done"] = True
                await database_work(ctx.save)
                return result(part)
            part["verify_pending"] = True
            from .request_reuse import fresh

            with fresh():
                last = await fetch_page(1)
            ctx.pages += 1
            if signature(last) != head or last["total"] != part["total"]:
                if restart_all:
                    await restart_snapshot()
                ctx.data["restarts"] = ctx.data.get("restarts", 0) + 1
                ctx.data["parts"].pop(key, None)
                await database_work(ctx.save)
                raise YieldCollection()
            part["rows"].update({str(r["id"]): r for r in last["rows"]})
            ctx.mark(key, last["rows"])
            part["done"] = True
            part.pop("verify_pending", None)
            await database_work(ctx.save)
            return result(part)
        if not fresh:
            await database_work(ctx.save)
            raise PageReadError("列表分页重复或数量变化，已保留断点，稍后核对")
        await database_work(ctx.check_budget)


def result(part):
    return {
        "rows": list(part["rows"].values()),
        "source_total": part["total"] if part["total"] is not None else part["loaded"],
        "source_loaded": part["loaded"],
        "complete": True,
        "source_unit": "记录",
    }
