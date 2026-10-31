"""Durable per-node home pause commands, including delivery after reconnection."""

import asyncio, json, logging, uuid
from fastapi import Depends, HTTPException
from sqlalchemy import select
from .database import settings
from .tunnel import content
from .business import selected_node

PREFIX = "home-collection-control:"
logger = logging.getLogger("ecmp.center.home_collection")


def install(app, database, hub, admin):
    acknowledged = {}
    locks = {}

    def desired():
        with database.engine.connect() as db:
            return {
                key[len(PREFIX) :]: value
                for key, value in db.execute(
                    select(settings.c.key, settings.c.value).where(settings.c.key.like(PREFIX + "%"))
                )
            }

    def targets():
        node = selected_node.get()
        return [n for n in database.registry() if not node or n["node"] == node]

    async def deliver(entry, value):
        async with locks.setdefault(entry["node"], asyncio.Lock()):
            current = await asyncio.to_thread(desired)
            if current.get(entry["node"], {}).get("revision") != value["revision"]:
                return False
            return await deliver_current(entry, value)

    async def deliver_current(entry, value):
        node = entry["node"]
        peer = hub.peers.get(node)
        if peer is None:
            return False
        try:
            async with asyncio.timeout(8):
                result = await hub.request(
                    node, "PUT", "system/home-collection", body=json.dumps({"paused": value["paused"]}).encode()
                )
            payload = json.loads(content(result))
            if result["status"] == 200 and payload.get("confirmed") and payload.get("paused") == value["paused"]:
                acknowledged[node] = (value["revision"], id(peer))
                return True
        except Exception:
            logger.debug("首页采集暂停指令下发失败：节点 %s", node, exc_info=True)
        return False

    async def read_one(entry, commands):
        node = entry["node"]
        wanted = commands.get(node)
        actual = None
        confirmed = False
        if node in hub.peers:
            try:
                async with asyncio.timeout(6):
                    result = await hub.request(node, "GET", "system/home-collection")
                if result["status"] == 200:
                    actual = json.loads(content(result)).get("paused")
                    confirmed = type(actual) is bool and (not wanted or actual == wanted["paused"])
            except Exception:
                logger.debug("读取节点 %s 首页采集暂停状态失败", node, exc_info=True)
        return {
            "id": entry["id"],
            "name": entry["name"],
            "online": node in hub.peers,
            "paused": wanted["paused"] if wanted else actual,
            "actual_paused": actual,
            "confirmed": confirmed,
        }

    @app.get("/api/system/home-collection", dependencies=[Depends(admin)])
    async def status():
        commands = await asyncio.to_thread(desired)
        entries = await asyncio.to_thread(targets)
        rows = await asyncio.gather(*(read_one(n, commands) for n in entries))
        values = {r["paused"] for r in rows}
        return {
            "nodes": rows,
            "paused": next(iter(values)) if len(values) == 1 else None,
            "pending": sum(not r["confirmed"] for r in rows),
        }

    @app.put("/api/system/home-collection", dependencies=[Depends(admin)])
    async def change(body: dict):
        if set(body) != {"paused"} or type(body["paused"]) is not bool:
            raise HTTPException(400, "暂停状态应为布尔值")
        value = {**body, "revision": uuid.uuid4().hex}
        entries = await asyncio.to_thread(targets)

        def persist():
            with database.engine.begin() as db:
                for entry in entries:
                    key = PREFIX + entry["node"]
                    if db.execute(select(settings.c.key).where(settings.c.key == key)).first():
                        db.execute(settings.update().where(settings.c.key == key).values(value=value))
                    else:
                        db.execute(settings.insert().values(key=key, value=value))

        await asyncio.to_thread(persist)
        results = await asyncio.gather(*(deliver(n, value) for n in entries))
        return {
            "paused": body["paused"],
            "nodes": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "online": n["node"] in hub.peers,
                    "paused": body["paused"],
                    "confirmed": ok,
                }
                for n, ok in zip(entries, results)
            ],
            "pending": sum(not ok for ok in results),
        }

    async def reconcile():
        failures = 0
        while True:
            try:
                commands = await asyncio.to_thread(desired)
                registry = await asyncio.to_thread(database.registry) if commands else []
                await asyncio.gather(
                    *(
                        deliver(n, commands[n["node"]])
                        for n in registry
                        if n["node"] in commands
                        and n["node"] in hub.peers
                        and acknowledged.get(n["node"]) != (commands[n["node"]]["revision"], id(hub.peers[n["node"]]))
                    )
                )
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                # Rate-limited: first failure and then every 60th (about 5 minutes).
                if failures == 1 or failures % 60 == 0:
                    logger.warning("首页采集暂停指令对账失败（连续 %d 次）", failures, exc_info=True)
            await asyncio.sleep(5)

    async def start():
        app.state.home_control_task = asyncio.create_task(reconcile())

    async def stop():
        app.state.home_control_task.cancel()
        await asyncio.gather(app.state.home_control_task, return_exceptions=True)

    from .lifecycle import chain

    chain(app, start, stop)
