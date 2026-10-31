import hashlib
import secrets
import time
import uuid
import threading
from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    Column,
    String,
    Float,
    JSON,
    Integer,
    Index,
    UniqueConstraint,
    select,
    update,
    delete,
    text,
    or_,
    and_,
)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
nodes = Table(
    "center_nodes",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("token_hash", String, nullable=False),
    Column("seen", Float, default=0),
)
snapshots = Table(
    "center_snapshots",
    metadata,
    Column("node", String, primary_key=True),
    Column("shop", String, primary_key=True),
    Column("scope", String, primary_key=True),
    Column("updated", Float, nullable=False),
    Column("payload", JSON, nullable=False),
)
jobs = Table(
    "center_jobs",
    metadata,
    Column("id", String, primary_key=True),
    Column("node", String, index=True),
    Column("request", JSON, nullable=False),
    Column("state", String, nullable=False),
    Column("created", Float),
    Column("expires", Float, default=0),
    Column("lease", String, default=""),
    Column("result", JSON),
)
node_ids = Table(
    "center_node_ids",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("node", String, unique=True, nullable=False),
)
shop_ids = Table(
    "center_shop_ids",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("node", String, nullable=False),
    Column("local_id", Integer, nullable=False),
    UniqueConstraint("node", "local_id"),
)
settings = Table(
    "center_settings", metadata, Column("key", String, primary_key=True), Column("value", JSON, nullable=False)
)
order_rows = Table(
    "center_order_rows",
    metadata,
    Column("node", String, primary_key=True),
    Column("stream", String, primary_key=True),
    Column("shop", String, primary_key=True),
    Column("scope", String, primary_key=True),
    Column("id", String, primary_key=True),
    Column("revision", Integer, nullable=False),
    Column("payload", JSON),
    Column("observed", Float, nullable=False),
)
Index("center_order_shop_scope", order_rows.c.node, order_rows.c.shop, order_rows.c.scope)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


# Node clock may drift; only timestamps this far beyond the node clock count as pre-rollback.
CLOCK_ROLLBACK_SLACK = 600


class Database:
    def __init__(self, url):
        from .change_signal import ChangeSignal

        self.changes = ChangeSignal()
        self.job_changes = ChangeSignal()
        self._auth_cache = {}
        self._auth_generation = 0
        self._node_cache = {}
        self._cache_lock = threading.Lock()
        self.engine = create_engine(url, pool_pre_ping=True)
        from .schema_migrations import core

        core(self.engine, metadata)
        self.backfill()

    def backfill(self):
        """Give every node a short public id; runs at startup and when a gap is seen."""
        try:
            with self.engine.begin() as db:
                missing = db.execute(select(nodes.c.id).where(~nodes.c.id.in_(select(node_ids.c.node)))).scalars().all()
                if missing:
                    db.execute(node_ids.insert(), [{"node": node} for node in missing])
        except IntegrityError:
            pass  # Another worker filled the same gap.

    def register(self, name):
        node, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        with self.engine.begin() as db:
            db.execute(nodes.insert().values(id=node, name=name, token_hash=digest(token), seen=0))
            db.execute(node_ids.insert().values(node=node))
        return {"id": node, "token": token}

    def invalidate_node(self, node):
        with self._cache_lock:
            self._auth_generation += 1
            self._auth_cache.pop(node, None)
            self._node_cache.clear()

    def authenticate(self, node, token, fresh=False):
        now = time.monotonic()
        with self._cache_lock:
            cached = self._auth_cache.get(node)
            generation = self._auth_generation
        if not fresh and cached and cached[0] > now:
            return secrets.compare_digest(cached[1], digest(token))
        with self.engine.connect() as db:
            row = db.execute(select(nodes.c.token_hash).where(nodes.c.id == node)).scalar()
        if row:
            with self._cache_lock:
                if generation == self._auth_generation:
                    self._auth_cache[node] = (now + 10, row)
        return bool(row and secrets.compare_digest(row, digest(token)))

    def resolve_node(self, public_id):
        now = time.monotonic()
        with self._cache_lock:
            cached = self._node_cache.get(public_id)
        if cached and cached[0] > now:
            return cached[1]
        with self.engine.connect() as db:
            node = db.execute(
                select(node_ids.c.node)
                .join(nodes, nodes.c.id == node_ids.c.node)
                .where(node_ids.c.id == public_id, nodes.c.token_hash != "")
            ).scalar()
        if node:
            with self._cache_lock:
                self._node_cache[public_id] = (now + 5, node)
        return node

    def lock_shops(self, db, identities):
        # PostgreSQL transaction locks fence ingestion against migration, without
        # serializing unrelated shops or heartbeat/lease operations on the node.
        if self.engine.dialect.name == "postgresql":
            for node, shop in sorted(set(identities)):
                key = int.from_bytes(hashlib.sha256(f"{node}:{shop}".encode()).digest()[:8], "big", signed=True)
                db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    def ingest(self, node, records):
        latest = {}
        for record in records:
            key = (record["shop"], record["scope"])
            if key not in latest or record["updated"] > latest[key]["updated"]:
                latest[key] = record
        with self.engine.begin() as db:
            self.lock_shops(db, [(node, r["shop"]) for r in latest.values() if r["shop"] != "0"])
            retired = set(
                db.execute(select(settings.c.key).where(settings.c.key.like(f"retired-shop:{node}:%"))).scalars()
            )
            values = [
                {"node": node, "shop": r["shop"], "scope": r["scope"], "updated": r["updated"], "payload": r["payload"]}
                for r in latest.values()
                if f"retired-shop:{node}:{r['shop']}" not in retired
            ]
            received = time.time()
            from ..business_kinds import KINDS

            offset = None

            for row in values:
                if row["scope"].split(":")[0] in KINDS | {
                    "freshness",
                    "attention",
                    "archive",
                    "history",
                    "resolved",
                    "tax_record",
                    "summary",
                } and isinstance(row["payload"], dict):
                    row["payload"] = {**row["payload"], "_center_received_at": received}
                if row["scope"] == "collector_status":
                    stamp = row["payload"].get("server_time")
                    if isinstance(stamp, (int, float)):
                        offset = stamp - received
                        row["payload"] = {
                            **row["payload"],
                            "clock_offset_seconds": round(offset, 1),
                            "clock_warning": abs(offset) > 30,
                        }
            local = {int(r["shop"]) for r in values if r["scope"] == "shop_metadata" and r["shop"].isdigit()}
            if local:
                known = set(
                    db.execute(
                        select(shop_ids.c.local_id).where(shop_ids.c.node == node, shop_ids.c.local_id.in_(local))
                    ).scalars()
                )
                if local - known:
                    db.execute(shop_ids.insert(), [{"node": node, "local_id": i} for i in sorted(local - known)])
            if offset is None:
                status = db.execute(
                    select(snapshots.c.payload).where(
                        snapshots.c.node == node, snapshots.c.shop == "0", snapshots.c.scope == "collector_status"
                    )
                ).scalar()
                saved = (status or {}).get("clock_offset_seconds")
                offset = saved if isinstance(saved, (int, float)) else 0
            # A stored timestamp far ahead of the node's current clock can only come
            # from before a clock rollback; let fresh captures replace it.
            future_limit = received + offset + CLOCK_ROLLBACK_SLACK
            for start in range(0, len(values), 500):
                self.upsert(db, values[start : start + 500], future_limit)
            from .list_completion import confirm_received

            confirm_received(db, node, values, received)
        # Heartbeat row update occurs only after payload/job locks are released.
        with self.engine.begin() as db:
            db.execute(update(nodes).where(nodes.c.id == node).values(seen=time.time()))

        self.changes.notify(node, values)

    def upsert(self, db, values, future_limit=None):
        """One statement per batch; an existing snapshot is replaced only by a strictly newer one
        (or when the stored timestamp is beyond ``future_limit``, i.e. written before a clock rollback)."""
        dialect = self.engine.dialect.name
        if dialect in ("postgresql", "sqlite"):
            from importlib import import_module

            statement = import_module(f"sqlalchemy.dialects.{dialect}").insert(snapshots).values(values)
            db.execute(
                statement.on_conflict_do_update(
                    index_elements=["node", "shop", "scope"],
                    set_={"updated": statement.excluded.updated, "payload": statement.excluded.payload},
                    where=or_(
                        statement.excluded.updated > snapshots.c.updated,
                        snapshots.c.payload["_transferred_from_node"].as_string().is_not(None),
                        *([snapshots.c.updated > future_limit] if future_limit is not None else []),
                    ),
                )
            )
            return
        for record in values:
            key = (
                (snapshots.c.node == record["node"])
                & (snapshots.c.shop == record["shop"])
                & (snapshots.c.scope == record["scope"])
            )
            old = db.execute(select(snapshots.c.updated).where(key)).scalar()
            if old is None:
                db.execute(snapshots.insert().values(**record))
            elif record["updated"] > old or (future_limit is not None and old > future_limit):
                db.execute(update(snapshots).where(key).values(updated=record["updated"], payload=record["payload"]))

    def registry(self, repaired=False):
        """Plain read used on every proxied request; node ids are backfilled at startup or on a gap."""
        with self.engine.connect() as db:
            rows = list(
                db.execute(
                    select(node_ids.c.id, nodes.c.id.label("node"), nodes.c.name, nodes.c.seen)
                    .select_from(nodes.outerjoin(node_ids, node_ids.c.node == nodes.c.id))
                    .where(nodes.c.token_hash != "")
                    .order_by(node_ids.c.id)
                ).mappings()
            )
            if any(r["id"] is None for r in rows) and not repaired:
                db.close()
                self.backfill()
                return self.registry(True)
            statuses = {
                r["node"]: r["payload"]
                for r in db.execute(
                    select(snapshots.c.node, snapshots.c.payload).where(
                        snapshots.c.shop == "0", snapshots.c.scope == "collector_status"
                    )
                ).mappings()
            }
            return [{**dict(r), "collector_status": statuses.get(r["node"], {})} for r in rows if r["id"] is not None]

    def move_shop(self, shop, target, local_id, public):
        """Preserve global identity and history; fence late writes from the old node."""
        with self.engine.begin() as db:
            self.lock_shops(db, [(shop["node_id"], str(shop["local_id"])), (target, str(local_id))])
            for node in sorted({shop["node_id"], target}):
                db.execute(select(nodes).where(nodes.c.id == node).with_for_update()).first()
            current = db.execute(select(shop_ids).where(shop_ids.c.id == shop["id"]).with_for_update()).mappings().one()
            if current["node"] != shop["node_id"] or current["local_id"] != shop["local_id"]:
                raise ValueError("店铺所属节点已变更，请刷新")
            old_key = f"retired-shop:{shop['node_id']}:{shop['local_id']}"
            db.execute(delete(settings).where(settings.c.key == old_key))
            db.execute(settings.insert().values(key=old_key, value={"shop_id": shop["id"]}))
            db.execute(delete(settings).where(settings.c.key == f"retired-shop:{target}:{local_id}"))
            # Target captures are authoritative. Copied source data keeps original
            # payload timestamps and a transfer marker that cannot fence off the target node after clock skew.
            rows = list(
                db.execute(
                    select(snapshots).where(
                        snapshots.c.node == shop["node_id"], snapshots.c.shop == str(shop["local_id"])
                    )
                ).mappings()
            )
            for row in rows:
                if row["scope"] == "shop_metadata":
                    continue
                row = dict(row)
                if row["payload"].get("_order_rows_v2"):
                    row["scope"] = "orders:view-v2:" + str(public.get("profile_version", 0))
                    row["payload"] = {
                        **row["payload"],
                        "_order_receipt_node": row["payload"].get("_order_receipt_node", shop["node_id"]),
                    }
                key = (
                    (snapshots.c.node == target)
                    & (snapshots.c.shop == str(local_id))
                    & (snapshots.c.scope == row["scope"])
                )
                previous = db.execute(select(snapshots.c.updated).where(key)).scalar()
                if previous is None:
                    db.execute(
                        snapshots.insert().values(
                            node=target,
                            shop=str(local_id),
                            scope=row["scope"],
                            updated=row["updated"],
                            payload={**row["payload"], "_transferred_from_node": shop["node_id"]},
                        )
                    )
                # Never replace an existing target capture using the source clock.
            from importlib import import_module

            transferred = [
                {
                    **dict(r),
                    "node": target,
                    "shop": str(local_id),
                    "scope": "orders:view-v2:" + str(public.get("profile_version", 0)),
                }
                for r in db.execute(
                    select(order_rows).where(
                        order_rows.c.node == shop["node_id"],
                        order_rows.c.shop == str(shop["local_id"]),
                        order_rows.c.scope == "orders:view-v2:" + str(shop.get("profile_version", 0)),
                    )
                ).mappings()
            ]
            for offset in range(0, len(transferred), 200):
                statement = (
                    import_module("sqlalchemy.dialects." + self.engine.dialect.name)
                    .insert(order_rows)
                    .values(transferred[offset : offset + 200])
                )
                db.execute(
                    statement.on_conflict_do_update(
                        index_elements=["node", "stream", "shop", "scope", "id"],
                        set_={k: getattr(statement.excluded, k) for k in ("payload", "observed", "revision")},
                        where=statement.excluded.observed > order_rows.c.observed,
                    )
                )
            db.execute(
                delete(snapshots).where(
                    snapshots.c.node == target, snapshots.c.shop == str(local_id), snapshots.c.scope == "shop_metadata"
                )
            )
            fields = (
                "name",
                "alias",
                "detected_name",
                "platform",
                "profile_version",
                "browser",
                "operators",
                "operator",
                "login_method",
                "login_url",
                "username",
                "adapter_available",
                "data_adapter_available",
            )
            payload = {k: public[k] for k in fields if k in public}
            payload.update(state="stopped", visible=False)
            db.execute(
                snapshots.insert().values(
                    node=target, shop=str(local_id), scope="shop_metadata", updated=time.time(), payload=payload
                )
            )
            db.execute(
                delete(shop_ids).where(
                    shop_ids.c.node == target, shop_ids.c.local_id == local_id, shop_ids.c.id != shop["id"]
                )
            )
            db.execute(update(shop_ids).where(shop_ids.c.id == shop["id"]).values(node=target, local_id=local_id))
            for job in db.execute(
                select(jobs).where(jobs.c.node == shop["node_id"], jobs.c.state.in_(["queued", "leased"]))
            ).mappings():
                if job["request"].get("shop_id") == shop["local_id"]:
                    db.execute(
                        update(jobs)
                        .where(jobs.c.id == job["id"])
                        .values(state="failed", lease="", result={"ok": False, "message": "店铺已切换采集节点"})
                    )
            db.execute(
                delete(snapshots).where(
                    snapshots.c.node.in_([target, shop["node_id"]]),
                    snapshots.c.shop == "0",
                    snapshots.c.scope.like("response:%"),
                )
            )

    def shops(self):
        with self.engine.connect() as db:
            rows = db.execute(
                select(
                    shop_ids, snapshots.c.payload, snapshots.c.updated, nodes.c.name.label("machine_name"), nodes.c.seen
                )
                .join(
                    snapshots,
                    (snapshots.c.node == shop_ids.c.node)
                    & (snapshots.c.shop == shop_ids.c.local_id.cast(String))
                    & (snapshots.c.scope == "shop_metadata"),
                )
                .join(nodes, nodes.c.id == shop_ids.c.node)
                .where(nodes.c.token_hash != "")
            ).mappings()
            return [
                {
                    **r["payload"],
                    "id": r["id"],
                    "local_id": r["local_id"],
                    "node_id": r["node"],
                    "machine_name": r["machine_name"],
                    "state": r["payload"].get("state", "stopped") if time.time() - r["seen"] < 60 else "stopped",
                    "collector_online": time.time() - r["seen"] < 60,
                }
                for r in rows
                if not r["payload"].get("_deleted")
            ]

    def enqueue(self, node, request):
        from .collection_requests import identity, expired, metadata

        now = time.time()
        wanted = identity(request)
        with self.engine.begin() as db:
            if not db.execute(select(nodes).where(nodes.c.id == node).with_for_update()).first():
                raise ValueError("采集节点不存在")
            for row in db.execute(
                select(jobs).where(
                    jobs.c.node == node,
                    jobs.c.state.in_(["queued", "leased"])
                    | ((jobs.c.state == "dispatched") & (jobs.c.created > time.time() - 120)),
                )
            ).mappings():
                if expired(row["request"], now):
                    if row["state"] == "queued":
                        db.execute(
                            update(jobs)
                            .where(jobs.c.id == row["id"])
                            .values(
                                state="failed",
                                lease="",
                                result={
                                    "ok": False,
                                    "expired": True,
                                    "message": "自动采集需求已过期；未删除业务数据或断点",
                                },
                            )
                        )
                    continue
                if identity(row["request"]) == wanted:
                    # Renew only queued automatic demand. Never mutate a leased
                    # request or clear the checkpoint of work already running.
                    if row["state"] == "queued" and metadata(request).get("source") == "automatic":
                        db.execute(update(jobs).where(jobs.c.id == row["id"]).values(request=request))
                    return row["id"]
            job = str(uuid.uuid4())
            db.execute(
                jobs.insert().values(
                    id=job, node=node, request=request, state="queued", created=time.time(), expires=0, lease=""
                )
            )
        self.job_changes.notify(node, [{"scope": "job"}])
        return job

    def claim(self, node):
        from .collection_requests import expired

        now = time.time()
        with self.engine.begin() as db:
            db.execute(select(nodes).where(nodes.c.id == node).with_for_update()).first()
            capabilities = (
                db.execute(
                    select(snapshots.c.payload).where(
                        snapshots.c.node == node, snapshots.c.shop == "0", snapshots.c.scope == "collector_status"
                    )
                ).scalar()
                or {}
            ).get("capabilities", [])
            pending_ids = [
                int(key.rsplit(":", 1)[1])
                for key in db.execute(select(settings.c.key).where(settings.c.key.like("shop-transfer:%"))).scalars()
            ]
            moving = (
                set(
                    db.execute(
                        select(shop_ids.c.local_id).where(shop_ids.c.node == node, shop_ids.c.id.in_(pending_ids))
                    ).scalars()
                )
                if pending_ids
                else set()
            )
            claimable = ((jobs.c.state == "queued") & (jobs.c.expires <= now)) | (
                (jobs.c.state == "leased") & (jobs.c.expires < now)
            )
            # Claims for one node are already serialized by the node row lock above,
            # so candidates are read without locking every queued row; only the
            # chosen job is updated, and only if it is still claimable.
            candidates = (
                db.execute(select(jobs).where(jobs.c.node == node, claimable).order_by(jobs.c.created)).mappings().all()
            )
            ready = []
            obsolete = []
            for candidate in candidates:
                if (
                    candidate["request"].get("_list_completion", {}).get("version") == 1
                    and "list-completion-v1" not in capabilities
                ):
                    continue
                if expired(candidate["request"], now):
                    obsolete.append(candidate["id"])
                    continue
                if candidate["request"].get("shop_id") not in moving:
                    ready.append(candidate)
            if obsolete:
                db.execute(
                    update(jobs)
                    .where(jobs.c.id.in_(obsolete), claimable)
                    .values(
                        state="failed",
                        lease="",
                        result={
                            "ok": False,
                            "expired": True,
                            "message": "自动采集需求已过期；等待当前范围需求，保留业务数据和断点",
                        },
                    )
                )
            lease = secrets.token_urlsafe(24)
            for row in ready:
                taken = db.execute(
                    update(jobs)
                    .where(jobs.c.id == row["id"], claimable)
                    .values(state="leased", lease=lease, expires=now + 120)
                )
                if taken.rowcount == 1:
                    return {**dict(row), "lease": lease}
            return None

    def finish(self, node, job, lease, result):
        with self.engine.begin() as db:
            # Lock only this job; ingestion confirmations acquire the same row lock.
            current = (
                db.execute(
                    select(jobs.c.request, jobs.c.result, jobs.c.created)
                    .where(jobs.c.id == job, jobs.c.node == node)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            request = current["request"] if current else {}
            confirmation = (current["result"] or {}).get("list_completion") if current else None
            if confirmation:
                result = {**result, "list_completion": confirmation}
            retry = not result.get("ok") and request.get("_list_completion", {}).get("version") == 1
            if retry:
                attempts = int((current["result"] or {}).get("attempts", 0)) + 1
                retry = attempts < 20 and time.time() - current["created"] < 86400
                result = {**result, "attempts": attempts, "retry_exhausted": not retry}
                if not retry:
                    result["message"] = (
                        "补齐任务已达到重试上限，保留断点，请处理原因后重新提交："
                        + str(result.get("message", ""))[:300]
                    )
                retry_seconds = min(3600, 60 * 2 ** min(attempts - 1, 6))
            updated = db.execute(
                update(jobs)
                .where(
                    jobs.c.id == job,
                    jobs.c.node == node,
                    jobs.c.lease == lease,
                    jobs.c.state == "leased",
                    jobs.c.expires >= time.time(),
                )
                .values(
                    state="queued" if retry else "dispatched" if result.get("ok") else "failed",
                    result=result,
                    **({"expires": time.time() + retry_seconds, "lease": ""} if retry else {}),
                )
            )
            return updated.rowcount == 1

    def purge_jobs(self, age=7 * 86400):
        with self.engine.begin() as db:
            return db.execute(
                delete(jobs).where(
                    jobs.c.state.in_(["dispatched", "failed"]),
                    or_(
                        and_(
                            jobs.c.request["_list_completion"]["version"].as_integer().is_(None),
                            jobs.c.created < time.time() - age,
                        ),
                        and_(
                            jobs.c.request["_list_completion"]["version"].as_integer() == 1,
                            jobs.c.created < time.time() - max(age, 30 * 86400),
                        ),
                    ),
                )
            ).rowcount

    def list(self, table, columns=None, **filters):
        statement = select(*(table.c[name] for name in columns)) if columns else select(table)
        for key, value in filters.items():
            statement = statement.where(table.c[key] == value)
        if table is jobs:
            statement = statement.order_by(jobs.c.created.desc()).limit(200)
        with self.engine.connect() as db:
            return [dict(row) for row in db.execute(statement).mappings()]
