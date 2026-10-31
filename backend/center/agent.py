"""Outbound bridge. Reads published local snapshots, never credentials or browser profiles."""

import asyncio
import logging
import os
from pathlib import Path
import ssl
import time
import httpx
from .replication import encode, fingerprint

LOG = logging.getLogger("ecmp.agent")
from ..business_kinds import KINDS

FIELDS = (
    "id",
    "name",
    "alias",
    "detected_name",
    "platform",
    "state",
    "browser",
    "profile_version",
    "operators",
    "operator",
    "login_method",
    "login_url",
    "visible",
    "debug_mode",
    "error",
    "adapter_available",
    "data_adapter_available",
    "username",
)
# Unchanged shop metadata is resent periodically so a restored or restarted center converges.
METADATA_HEARTBEAT = 300


def tls_context():
    """Optional private CA for a LAN center serving its own certificate."""
    path = os.environ.get("ECMP_CENTER_CA_FILE", "")
    return ssl.create_default_context(cafile=path) if path else None


def supervise(task, factory, name):
    if not task.done():
        return task
    if not task.cancelled() and task.exception():
        LOG.error("%s隧道任务异常退出，正在重启", name, exc_info=task.exception())
    else:
        LOG.warning("%s隧道任务已退出，正在重启", name)
    return asyncio.create_task(factory())


async def run():
    from .address import center_url

    url = center_url(os.environ["ECMP_CENTER_URL"])
    directory = Path(os.environ.get("PDD_DATA_DIR", ".data")).resolve()
    node = os.environ["ECMP_NODE_ID"]
    metadata = {}
    heartbeat = 0
    local_token = os.environ.get("PDD_API_TOKEN") or (directory / "api.key").read_text().strip()
    headers = {"Authorization": "Bearer " + os.environ["ECMP_NODE_TOKEN"], "X-Node-ID": node}
    context = tls_context()
    async with (
        httpx.AsyncClient(
            base_url=url, headers=headers, timeout=40, trust_env=False, **({"verify": context} if context else {})
        ) as center,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{int(os.environ.get('ECMP_LOCAL_PORT', '8001'))}",
            headers={"Authorization": "Bearer " + local_token},
            timeout=45,
            trust_env=False,
        ) as local,
    ):
        from .agent_tunnel import run as tunnel

        job_wake = asyncio.Event()
        business = lambda: tunnel(url, headers, local, local_token, ssl_context=context, job_wake=job_wake)
        control = lambda: tunnel(url, headers, local, local_token, control_only=True, ssl_context=context)
        tunnel_task = asyncio.create_task(business())
        control_task = asyncio.create_task(control())

        async def publish(records):
            for start in range(0, len(records), 100):
                response = await center.post(
                    "/agent/snapshots",
                    content=encode(records[start : start + 100]),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()

        from .replication_worker import Delivery

        deliveries = [Delivery(directory, lane, [url, node]) for lane in ("business", "auxiliary")]
        from .order_delivery import OrderDelivery

        deliveries.append(OrderDelivery(directory))
        from ..collector_update import read_json

        manifest = read_json(directory / "shop-storage.json", {})
        if os.environ.get("ECMP_SHOP_DATABASES") == "1" or any(
            e.get("state") == "active" for e in manifest.get("shops", {}).values()
        ):
            from .shop_delivery import ShopDeliveryGroup
            from .replication_worker import ReceiptCache

            receipts = ReceiptCache()
            deliveries = [
                ShopDeliveryGroup(directory, lane, [url, node], receipts=receipts)
                for lane in ("business", "auxiliary", "orders")
            ]
        from .delivery_notify import listen

        notifier = None
        try:
            notifier = await listen(directory, deliveries)
        except OSError:
            LOG.warning("入库通知暂不可用，继续每 5 秒检查待上传数据")
        writers = [asyncio.create_task(item.run(center)) for item in deliveries]
        cached_status = {"capabilities": ["list-completion-v1"]}

        async def metadata_once():
            nonlocal metadata, heartbeat
            # Account identity is needed centrally; passwords, tokens and browser profiles stay local.
            result = await local.get("/api/shops")
            result.raise_for_status()
            shops = result.json()
            from ..collector_update import read_json

            runtime = read_json(directory.parent / "collector-version.json", {})
            update_state = read_json(directory / "updates/status.json", {})
            from .replication_worker import status as delivery_status

            status = {
                "delivery": delivery_status(directory),
                "version": runtime.get("version", "手动部署"),
                "update": update_state.get("message", ""),
                "available": update_state.get("available", ""),
            }
            status["capabilities"] = ["list-completion-v1"]
            # Disk version alone cannot prove the browser-owning process
            # restarted. Forward its immutable boot identity separately.
            try:
                health = await local.get("/health")
                health.raise_for_status()
                status["runtime"] = health.json().get("runtime", {})
                status["server_time"] = health.json().get("server_time")
            except Exception:
                status["runtime"] = {}
            # Frequency metadata is cheap and lets the center label old business
            # snapshots correctly without querying a busy browser process.
            from .runtime_policy import read as read_policy

            policy = await asyncio.to_thread(read_policy, directory)
            if policy:
                status["collection"] = policy
            now = time.time()
            full = now - heartbeat >= METADATA_HEARTBEAT
            current = {str(s["id"]): {k: s[k] for k in FIELDS if k in s} for s in shops}
            changed = [
                {"shop": shop, "scope": "shop_metadata", "updated": now, "payload": payload}
                for shop, payload in current.items()
                if full or metadata.get(shop) != fingerprint(payload)
            ]
            await publish(changed)
            first_runtime = not cached_status.get("runtime")
            cached_status.clear()
            cached_status.update(status)
            if first_runtime and status.get("runtime"):
                job_wake.set()
            metadata = {shop: fingerprint(payload) for shop, payload in current.items()}
            if full:
                heartbeat = now

        async def heartbeat_once():
            # Liveness is independent of slow local browser/API or job dispatch.
            now = time.time()
            await publish(
                [
                    {
                        "shop": "0",
                        "scope": "collector_status",
                        "updated": now,
                        "payload": {**cached_status, "server_time": now},
                    }
                ]
            )

        async def claim_once():
            for _ in range(100):
                response = await center.post("/agent/claim")
                response.raise_for_status()
                job = response.json()
                if not job:
                    return
                request = dict(job["request"])
                try:
                    if request.get("kind") not in KINDS:
                        raise ValueError("该模块尚未接入中心采集")
                    from .collection_requests import dispatch

                    outcome = await dispatch(local, request)
                except (ValueError, httpx.HTTPError) as error:
                    outcome = {"ok": False, "message": str(error)[:400]}
                response = await center.post(
                    f"/agent/jobs/{job['id']}/finish", json={"lease": job["lease"], "result": outcome}
                )
                response.raise_for_status()
            job_wake.set()

        from .agent_loops import periodic, jobs_loop

        metadata_task = asyncio.create_task(periodic(metadata_once, 5, "元数据"))
        heartbeat_task = asyncio.create_task(periodic(heartbeat_once, 5, "心跳"))
        jobs_task = asyncio.create_task(jobs_loop(claim_once, job_wake))
        try:
            while True:
                tunnel_task = supervise(tunnel_task, business, "业务")
                control_task = supervise(control_task, control, "远控")
                writers = [
                    supervise(task, lambda item=item: item.run(center), "回传")
                    for item, task in zip(deliveries, writers)
                ]
                await asyncio.sleep(2)
        finally:
            if notifier:
                notifier.close()
            for task in (tunnel_task, control_task, metadata_task, heartbeat_task, jobs_task, *writers):
                task.cancel()
            await asyncio.gather(
                tunnel_task, control_task, metadata_task, heartbeat_task, jobs_task, *writers, return_exceptions=True
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())
