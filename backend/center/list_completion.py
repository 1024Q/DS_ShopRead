"""Durable center commands and result receipts for explicitly authorized list repairs."""

import time
import uuid
from collections import Counter
from sqlalchemy import select, update, func
from .database import jobs, nodes, snapshots
from ..list_completion import ALLOWED, MARKER, CAPABILITY, identifier, receipt_key, count_fields


def confirm_received(db, node, records, received):
    """Confirm business data in the ingest transaction, never a status-only receipt.

    Keep the proof on the durable center command. The next successful request
    may replace the readable list snapshot without undoing this request's
    completed state; no duplicate copy of all business rows is needed.
    """
    for record in records:
        kind = record["scope"].split(":", 1)[0]
        payload = record["payload"]
        proof = payload.get("_list_coverage", {}) if isinstance(payload, dict) else {}
        if (
            kind not in ALLOWED
            or not isinstance(payload, dict)
            or payload.get("complete") is not True
            or proof.get("version") != 1
        ):
            continue
        if proof.get("status") != ALLOWED[kind] or not proof.get("snapshot_started_at"):
            continue
        rid = proof.get("request_id")
        if not rid:
            continue
        # An older publication rejected by the ingest timestamp fence is not a
        # newly accepted result. Read only the proof, never the row-heavy JSON.
        accepted = (
            db.execute(
                select(
                    snapshots.c.updated,
                    snapshots.c.payload["_list_coverage"]["request_id"].as_string().label("request_id"),
                ).where(
                    snapshots.c.node == node, snapshots.c.shop == record["shop"], snapshots.c.scope == record["scope"]
                )
            )
            .mappings()
            .first()
        )
        if not accepted or accepted["updated"] != record["updated"] or accepted["request_id"] != rid:
            continue
        statement = select(jobs).where(
            jobs.c.node == node,
            jobs.c.request["kind"].as_string() == kind,
            jobs.c.request["shop_id"].as_integer() == int(record["shop"]),
            jobs.c.request[MARKER]["id"].as_string() == rid,
        )
        for job in db.execute(statement.with_for_update()).mappings():
            previous = job.get("result") or {}
            if previous.get("list_completion", {}).get("center_confirmed"):
                continue
            confirmation = {
                "center_confirmed": True,
                "center_received_at": received,
                "scope": {k: proof.get(k) for k in ("status", "date_from", "date_to")},
                "completed_at": received,
                "result_key": record["scope"],
                **count_fields(payload),
            }
            db.execute(
                update(jobs).where(jobs.c.id == job["id"]).values(result={**previous, "list_completion": confirmation})
            )


def create(service, kind, body):
    request_id = identifier(body["request_id"])
    if kind not in ALLOWED or body["status"] != ALLOWED[kind]:
        raise ValueError("仅支持一次性补齐待发货、待商家处理售后、待处理工单")
    shops = [s for s in service.store.list() if s["id"] in body["shop_ids"] and s.get("platform") == "pdd"]
    if len(shops) != len(set(body["shop_ids"])):
        raise ValueError("请选择有效的拼多多店铺")
    with service.database.engine.begin() as db:
        for shop in sorted(shops, key=lambda s: (s["node_id"], s["id"])):
            # Node lock also serializes duplicate POSTs without a new schema.
            db.execute(select(nodes.c.id).where(nodes.c.id == shop["node_id"]).with_for_update()).one()
            job = str(uuid.uuid5(uuid.NAMESPACE_URL, f"list-completion:{kind}:{request_id}:{shop['id']}"))
            if db.execute(select(jobs.c.id).where(jobs.c.id == job)).first():
                continue
            request = {
                "kind": kind,
                "shop_id": shop["local_id"],
                "status": ALLOWED[kind],
                MARKER: {"version": 1, "id": request_id},
                "_dispatch": {
                    "version": 1,
                    "source": "manual",
                    "force": False,
                    "generated_at": time.time(),
                    "valid_until": 0,
                },
            }
            db.execute(
                jobs.insert().values(
                    id=job,
                    node=shop["node_id"],
                    request=request,
                    state="queued",
                    created=time.time(),
                    expires=0,
                    lease="",
                )
            )
    for node in {shop["node_id"] for shop in shops}:
        service.database.job_changes.notify(node, [{"scope": "job"}])
    return listing(service, kind, request_id)


def listing(service, kind, request_id=None):
    if kind not in ALLOWED:
        raise ValueError("不支持该模块的一次性补齐")
    if request_id:
        request_id = identifier(request_id)
    shops = {(s["node_id"], int(s["local_id"])): s for s in service.store.list()}
    rows = []
    wanted = []
    with service.database.engine.connect() as db:
        for row in db.execute(
            select(jobs).where(jobs.c.request["kind"].as_string() == kind).order_by(jobs.c.created.desc())
        ).mappings():
            meta = row["request"].get(MARKER, {})
            if meta.get("version") != 1 or not meta.get("id") or request_id and meta["id"] != request_id:
                continue
            pair = row["node"], int(row["request"]["shop_id"])
            if pair not in shops:
                continue
            wanted.append((dict(row), shops[pair], meta["id"]))
            if not request_id and len(wanted) >= 200:
                break
        statuses = {
            r["node"]: r["payload"]
            for r in db.execute(
                select(snapshots.c.node, snapshots.c.payload).where(
                    snapshots.c.shop == "0", snapshots.c.scope == "collector_status"
                )
            ).mappings()
        }
        for job, shop, rid in wanted:
            saved = (
                db.execute(
                    select(snapshots).where(
                        snapshots.c.node == job["node"],
                        snapshots.c.shop == str(shop["local_id"]),
                        snapshots.c.scope == receipt_key(kind, rid),
                    )
                )
                .mappings()
                .first()
            )
            receipt = dict(saved["payload"]) if saved else {}
            capability = CAPABILITY in statuses.get(job["node"], {}).get("capabilities", [])
            dispatch_error = (job.get("result") or {}) if (job.get("result") or {}).get("ok") is False else {}
            state = receipt.get("status") or ("retry" if dispatch_error else "queued")
            stage = receipt.get("stage") or (
                "下发暂未成功，保留任务等待重试"
                if dispatch_error
                else "等待采集节点接收一次性任务"
                if job["state"] == "queued"
                else "已下发，等待节点保存任务状态"
            )
            confirmation = (job.get("result") or {}).get("list_completion", {})
            confirmed = bool(confirmation.get("center_confirmed"))
            received = confirmation.get("center_received_at") if confirmed else None
            if confirmed:
                receipt.update(confirmation)
                if "loaded" not in confirmation:
                    # Read-only compatibility for confirmations from the first
                    # release. Do not hydrate rows or rewrite completion time.
                    counts = (
                        db.execute(
                            select(
                                func.json_array_length(snapshots.c.payload["rows"]).label("loaded"),
                                snapshots.c.payload["source_total"].as_integer().label("total"),
                            ).where(
                                snapshots.c.node == job["node"],
                                snapshots.c.shop == str(shop["local_id"]),
                                snapshots.c.scope == confirmation.get("result_key"),
                                snapshots.c.payload["_list_coverage"]["request_id"].as_string() == rid,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    receipt.update(
                        count_fields({"source_total": counts["total"]}, counts["loaded"])
                        if counts
                        else {"loaded": None, "total": None}
                    )
                state = "completed"
                stage = "本次状态列表已完整读取，中心已确认接收"
            if not confirmed and not shop.get("collector_online"):
                state = "waiting_node"
                stage = "节点离线，任务已保存，等待上线后继续"
            elif not confirmed and not capability:
                state = "waiting_upgrade"
                stage = "等待采集节点升级到支持一次性补齐的版本"
            rows.append(
                {
                    **receipt,
                    "request_id": rid,
                    "kind": kind,
                    "shop_id": shop["id"],
                    "shop_name": shop.get("name", ""),
                    "node_id": shop["node_id"],
                    "machine_name": shop.get("machine_name", ""),
                    "job_id": job["id"],
                    "status": state,
                    "stage": stage,
                    "center_confirmed": confirmed,
                    "center_received_at": received,
                    "created_at": job["created"],
                    "retry_at": receipt.get("retry_at")
                    or (job["expires"] if dispatch_error and job["state"] == "queued" else 0),
                    "last_error": "" if confirmed else receipt.get("last_error") or dispatch_error.get("message", ""),
                }
            )
    return {"request_id": request_id, "kind": kind, "tasks": rows, "counts": dict(Counter(r["status"] for r in rows))}
