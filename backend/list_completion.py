"""Explicit, durable one-shot list completion; never changes subscriptions."""

import json
import math
import time
import uuid
import threading

ALLOWED = {"orders": "待发货", "aftersales": "待商家处理", "tickets": "待处理"}
MARKER = "_list_completion"
CAPABILITY = "list-completion-v1"


def identifier(value):
    try:
        return str(uuid.UUID(str(value)))
    except ValueError, TypeError, AttributeError:
        raise ValueError("一次性补齐编号必须是 UUID") from None


def valid(kind, scope):
    value = scope.get(MARKER)
    if kind not in ALLOWED or scope.get("status") != ALLOWED[kind] or not isinstance(value, dict):
        return False
    try:
        return (
            value.get("version") == 1
            and identifier(value.get("id")) == value.get("id")
            and type(value.get("created_at")) in (int, float)
            and math.isfinite(value["created_at"])
            and value["created_at"] > 0
            and isinstance(value.get("frequency_group"), str)
        )
    except ValueError:
        return False


def pending(record):
    scope = record.get("options", {})
    if isinstance(scope, str):
        scope = json.loads(scope)
    return valid(record["kind"], scope) and record.get("last_success", 0) < scope[MARKER]["created_at"]


def frequency(kind, scope, config):
    """Use an existing cadence's execution budget, without enabling its module."""
    if not valid(kind, scope):
        return None
    wanted = scope[MARKER]["frequency_group"]
    groups = config.get("frequency_groups", [])
    return next(
        (g["id"] for g in groups if g["id"] == wanted),
        next((g["id"] for g in groups if not g.get("time")), groups[0]["id"] if groups else None),
    )


def clean_scope(kind, scope):
    result = {k: v for k, v in scope.items() if k != MARKER}
    if kind in ("aftersales", "tickets"):
        result["_relative_days"] = 180 if kind == "tickets" else 7
    return result


def receipt_key(kind, request_id):
    return f"list_completion:{kind}:{request_id}"


def order_key(shop):
    return "orders:list-v1:unshipped:" + str(shop.get("profile_version", 0))


def order_candidate(payload, options):
    proof = payload.get("_list_coverage", {})
    return (
        payload.get("complete")
        and proof.get("version") == 1
        and proof.get("status") == options.get("status")
        and proof.get("date_from", "9999") <= options.get("date_from", "")
        and proof.get("date_to", "") >= options.get("date_to", "9999")
        and bool(proof.get("snapshot_started_at"))
    )


def count_fields(payload, loaded=None):
    """Completed progress is the accepted snapshot, not the last page callback."""
    if loaded is None:
        loaded = len(payload["rows"]) if isinstance(payload.get("rows"), list) else payload.get("source_loaded")

    def count(value):
        return (
            int(value)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0 and int(value) == value
            else None
        )

    loaded = count(loaded)
    total = count(payload.get("source_total"))
    return {"loaded": loaded, "total": loaded if total is None else total}


def completed_counts(service, shop, record, result_key, request_id):
    # table_snapshots contains no business rows. The request-specific copy can
    # still prove its counts after a newer repair replaces the readable list.
    with service.store.connect() as db:
        saved = db.execute(
            "SELECT cache_key,metadata,row_count FROM table_snapshots WHERE shop_id=? AND cache_key IN (?,?)",
            (shop["id"], record["cache_key"], result_key),
        ).fetchall()
    for row in sorted(saved, key=lambda r: r["cache_key"] != record["cache_key"]):
        data = json.loads(row["metadata"])
        identity = data.get("_list_coverage", {}).get("request_id") or data.get("_sync", {}).get("scope", {}).get(
            MARKER, {}
        ).get("id")
        if data.get("complete") and identity == request_id:
            return count_fields(data, row["row_count"])
    return {"loaded": None, "total": None}


def publish(service, shop, kind, scope, payload):
    """Only a complete native status scan can replace its readable snapshot."""
    if not valid(kind, scope):
        return
    if not payload.get("complete"):
        raise ValueError("一次性列表尚未读取完整，保留断点继续读取")
    options = clean_scope(kind, scope)
    started = payload.get("_sync", {}).get("snapshot_started_at")
    if not started:
        raise ValueError("一次性列表缺少本次读取时间，不能确认完整")
    data = {
        **payload,
        "_sync": {**payload.get("_sync", {}), "scope": options},
        "_list_coverage": {
            "version": 1,
            "request_id": scope[MARKER]["id"],
            "status": options["status"],
            "date_from": options["date_from"],
            "date_to": options["date_to"],
            "snapshot_started_at": started,
        },
    }
    target = order_key(shop) if kind == "orders" else service.key(kind, options, shop)
    service.store.write_cache(shop["id"], target, data)


def status(service, shop, record):
    kind = record["kind"]
    scope = json.loads(record["options"])
    meta = scope[MARKER]
    job_key = (shop["id"], record["cache_key"])
    progress = service.progress.get(job_key, {})
    completed = not pending(record)
    target = order_key(shop) if kind == "orders" else service.key(kind, clean_scope(kind, scope), shop)
    if completed:
        counts = completed_counts(service, shop, record, target, meta["id"])
    else:
        with service.store.connect() as db:
            checkpoint = db.execute(
                "SELECT loaded,total FROM collection_checkpoints WHERE shop_id=? AND cache_key=?", job_key
            ).fetchone()
        counts = {
            "loaded": progress.get("loaded", checkpoint["loaded"] if checkpoint else 0),
            "total": progress.get("total", checkpoint["total"] if checkpoint else None),
        }
    current = (
        "uploading"
        if completed
        else "running"
        if progress.get("phase") == "running"
        else "waiting_login"
        if record.get("error_kind") == "login"
        else "retry"
        if record.get("failures")
        else "partial"
        if record.get("continuation")
        else "queued"
    )
    return {
        "request_id": meta["id"],
        "kind": kind,
        "shop_id": shop["id"],
        "shop_name": shop.get("name", ""),
        "status": current,
        "stage": "本机已完成，等待中心确认"
        if completed
        else progress.get("stage")
        or (
            "保留断点等待重试"
            if record.get("failures")
            else "已保存分页，等待下一批"
            if record.get("continuation")
            else "等待既有采集名额"
        ),
        "scope": clean_scope(kind, scope),
        **counts,
        "failures": record.get("failures", 0),
        "retry_at": record.get("retry_at", 0),
        "last_error": record.get("last_error", ""),
        "completed_at": record.get("last_success") if completed else None,
        "created_at": meta["created_at"],
        "center_confirmed": False,
        "result_key": target,
    }


def publication_state(service, shop_id, key):
    states = service.__dict__.setdefault("_list_completion_updates", {})
    return states.setdefault(
        (shop_id, key), {"lock": threading.RLock(), "at": -float("inf"), "signature": None, "completed": False}
    )


def write_status(service, shop, record, value, *, live=False, now=None):
    state = publication_state(service, shop["id"], record["cache_key"])
    stamp = time.monotonic() if now is None else now
    with state["lock"]:
        # A scheduler scan can overlap task completion. A stale scan must never
        # replace the final receipt with queued/running state.
        if state["completed"] and not value.get("completed_at"):
            return False
        if live and stamp - state["at"] < 10:
            return False
        fields = ("status", "stage", "loaded", "total", "failures", "retry_at", "last_error", "completed_at")
        signature = tuple(value.get(k) for k in fields)
        if signature == state["signature"]:
            return False
        service.store.write_cache(shop["id"], receipt_key(record["kind"], value["request_id"]), value)
        state.update(at=stamp, signature=signature, completed=bool(value.get("completed_at")))
        return True


def publish_status(service, shop, kind, scope, key):
    if not valid(kind, scope):
        return
    from .collection import state

    record = state(service.store, shop["id"], key)
    if record:
        write_status(service, shop, record, status(service, shop, record))


def publish_live_statuses(service, shop, records, *, limit=3, now=None):
    """Reuse scheduler metadata, with at most 3 changed receipts per shop scan.

    No timer or collection frequency is added. Completed requests are never
    polled for publication, and each pending request writes at most every 10s.
    """
    stamp = time.monotonic() if now is None else now
    candidates = []
    for record in records.values() if isinstance(records, dict) else records:
        if not pending(record):
            continue
        state = publication_state(service, shop["id"], record["cache_key"])
        if not state["completed"] and stamp - state["at"] >= 10:
            candidates.append((state["at"], record))
    written = 0
    for _, record in sorted(candidates, key=lambda item: item[0]):
        if written >= limit:
            break
        if write_status(service, shop, record, status(service, shop, record), live=True, now=stamp):
            written += 1
    return written


def create(service, kind, body):
    from . import collection
    from .business import options_for

    request_id = identifier(body["request_id"])
    if kind not in ALLOWED or body["status"] != ALLOWED[kind]:
        raise ValueError("仅支持一次性补齐待发货、待商家处理售后、待处理工单")
    shops = [s for s in service.store.list() if s["id"] in body["shop_ids"] and s.get("platform", "pdd") == "pdd"]
    if len(shops) != len(set(body["shop_ids"])):
        raise ValueError("请选择有效的拼多多店铺")
    config = collection.settings(service.store)
    for shop in shops:
        # Serialize lookup and creation in the same shop write transaction.
        # In particular, a lost HTTP response followed by a concurrent retry
        # must not change the frozen dates, request timestamp or checkpoint key.
        with service.store.write_connection() as db:
            existing = db.execute(
                "SELECT cache_key FROM collection_tasks WHERE shop_id=? AND kind=? AND json_extract(options,'$._list_completion.id')=?",
                (shop["id"], kind, request_id),
            ).fetchone()
            if existing:
                continue
            groups = config["frequency_groups"]
            chosen = config.get("platform_modes", {}).get("pdd", {}).get(kind)
            group = next(
                (g for g in groups if g["id"] == chosen), next((g for g in groups if not g.get("time")), groups[0])
            )
            scope = options_for(kind, status=ALLOWED[kind])
            scope.pop("_relative_days", None)  # Freeze this one-shot range, including across restarts/midnight.
            scope[MARKER] = {"version": 1, "id": request_id, "created_at": time.time(), "frequency_group": group["id"]}
            key = service.key(kind, scope, shop)
            db.execute(
                "INSERT INTO collection_tasks(shop_id,cache_key,kind,options,last_requested,last_success,schedule_state) VALUES(?,?,?,?,0,0,?)",
                (shop["id"], key, kind, json.dumps(scope), "pending"),
            )
        publish_status(service, shop, kind, scope, key)
    return listing(service, kind, request_id)


def listing(service, kind, request_id=None):
    if kind not in ALLOWED:
        raise ValueError("不支持该模块的一次性补齐")
    if request_id:
        request_id = identifier(request_id)
    result = []
    for shop in service.store.list():
        with service.store.connect() as db:
            records = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM collection_tasks WHERE shop_id=? AND kind=? AND json_extract(options,'$._list_completion.version')=1",
                    (shop["id"], kind),
                )
            ]
        for record in records:
            scope = json.loads(record["options"])
            if valid(kind, scope) and (not request_id or scope[MARKER]["id"] == request_id):
                result.append(status(service, shop, record))
    from collections import Counter

    return {
        "request_id": request_id,
        "kind": kind,
        "tasks": result,
        "counts": dict(Counter(r["status"] for r in result)),
    }


def install(router, service):
    from fastapi import HTTPException, Query
    from pydantic import BaseModel, Field, ConfigDict
    from .async_database import run

    class RequestBody(BaseModel):
        model_config = ConfigDict(extra="forbid")
        request_id: str
        shop_ids: list[int] = Field(min_length=1, max_length=200)
        status: str

    @router.post("/{kind}/complete-once", status_code=202)
    async def complete_once(kind: str, body: RequestBody):
        try:
            handler = create
            if hasattr(service, "database"):
                from .center.list_completion import create as handler
            result = await run(handler, service, kind, body.model_dump())
            if not hasattr(service, "database"):
                service.wake.set()
                service.schedule_wake.set()
            return result
        except ValueError as error:
            raise HTTPException(400, str(error)) from None

    @router.get("/{kind}/complete-once")
    async def completion_status(kind: str, request_id: str | None = Query(default=None)):
        try:
            handler = listing
            if hasattr(service, "database"):
                from .center.list_completion import listing as handler
            return await run(handler, service, kind, request_id)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
