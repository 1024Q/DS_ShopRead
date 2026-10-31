"""Explicit, resumable shop reassignment; no browser profile or password extraction."""

import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, update
from sqlalchemy.exc import IntegrityError
from .database import settings, nodes
from .business import selected_node


class MoveInput(BaseModel):
    target_node: int = Field(gt=0)
    login_method: str = "qr"
    password: str = Field(default="", max_length=1024)


# A crashed worker cannot hold a shop forever; a move makes a few bounded node calls.
LOCK_STALE = 900


def release_revoked_transfer(database, shop):
    """An explicitly revoked target cannot receive a transfer or upload data."""
    key = f"shop-transfer:{shop['id']}"
    with database.engine.begin() as db:
        pending = db.execute(select(settings.c.value).where(settings.c.key == key).with_for_update()).scalar()
        if not pending or pending.get("source") != shop["node_id"]:
            return False
        lock = db.execute(select(settings.c.value).where(settings.c.key == f"shop-move-lock:{shop['id']}")).scalar()
        if lock and time.time() - float(lock.get("at", 0)) <= LOCK_STALE:
            return False
        target = db.execute(select(nodes.c.token_hash).where(nodes.c.id == pending.get("target"))).first()
        if not target or target.token_hash:
            return False  # Offline is not revoked.
        archive = f"shop-transfer-abandoned:{shop['id']}"
        db.execute(delete(settings).where(settings.c.key == archive))
        db.execute(
            settings.insert().values(key=archive, value={**pending, "reason": "target_revoked", "at": time.time()})
        )
        db.execute(delete(settings).where(settings.c.key == key))
        return True


async def stop_confirmed(remote, node, local_id):
    """A lost mutation reply is ambiguous; inspect live node state, never replay it."""
    try:
        await remote(node, "POST", f"shops/{local_id}/stop", {})
        return
    except HTTPException as error:
        if error.status_code not in (502, 503, 504):
            raise
        try:
            shops = await remote(node, "GET", "shops")
            shop = next((s for s in shops if s.get("id") == local_id), None)
        except HTTPException, TypeError, AttributeError:
            shop = None
        if shop and shop.get("state") == "stopped":
            return
        raise HTTPException(
            409, "停止请求未确认，核对后仍未确认店铺已停止；未迁移店铺，请检查原节点运行日志后重试"
        ) from None


def install(app, database, hub, admin):
    # Database work runs in worker threads so the single center event loop
    # keeps serving tunnels and heartbeats while a move is in progress.
    def read_setting(key):
        with database.engine.connect() as db:
            return db.execute(select(settings.c.value).where(settings.c.key == key)).scalar()

    def write_setting(key, value):
        with database.engine.begin() as db:
            if db.execute(select(settings.c.key).where(settings.c.key == key)).first():
                db.execute(update(settings).where(settings.c.key == key).values(value=value))
            else:
                db.execute(settings.insert().values(key=key, value=value))

    def delete_setting(key):
        with database.engine.begin() as db:
            db.execute(delete(settings).where(settings.c.key == key))

    @asynccontextmanager
    async def locked(shop_id):
        # The primary key makes the move lock exclusive across workers and processes.
        key = f"shop-move-lock:{shop_id}"
        owner = secrets.token_hex(8)

        def acquire():
            with database.engine.begin() as db:
                held = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar()
                if held and time.time() - float(held.get("at", 0)) > LOCK_STALE:
                    db.execute(delete(settings).where(settings.c.key == key))
                db.execute(settings.insert().values(key=key, value={"at": time.time(), "owner": owner}))

        def release():
            with database.engine.begin() as db:
                held = db.execute(select(settings.c.value).where(settings.c.key == key)).scalar()
                if held and held.get("owner") == owner:
                    db.execute(delete(settings).where(settings.c.key == key))

        try:
            await asyncio.to_thread(acquire)
        except IntegrityError:
            raise HTTPException(409, "该店铺正在切换节点，请等待完成") from None
        try:
            yield
        finally:
            await asyncio.to_thread(release)

    async def remote(node, method, path, body=None):
        response = await hub.request(node, method, path, body=json.dumps(body).encode() if body is not None else b"")
        from .tunnel import content

        try:
            value = json.loads(content(response))
        except ValueError, TypeError:
            # Never echo a raw node response: this channel may carry credentials.
            raise HTTPException(409, "节点接口返回异常，请确认采集端已更新并重启后重试") from None
        if response["status"] >= 300:
            raise HTTPException(response["status"], value.get("detail", "节点操作失败"))
        return value

    async def start_moved(shop_id, node, local_id):
        result = {"ok": True, "shop_id": shop_id, "node": node, "requires_login": True}
        try:
            started = await remote(node, "POST", f"shops/{local_id}/start", {})
            result.update(
                login_state=started.get("state", "logging_in"), requires_login=started.get("state") != "logged_in"
            )
        except HTTPException as error:
            result.update(login_state="login_failed", login_error=str(error.detail))
        return result

    @app.post("/api/shops/{shop_id}/move-node", dependencies=[Depends(admin)])
    async def move(shop_id: int, body: MoveInput):
        async with locked(shop_id):
            shop = next((s for s in await asyncio.to_thread(database.shops) if s["id"] == shop_id), None)
            if not shop or selected_node.get() and selected_node.get() != shop["node_id"]:
                raise HTTPException(404, "店铺不存在于当前节点")
            registry = await asyncio.to_thread(database.registry)
            target = next((n for n in registry if n["id"] == body.target_node), None)
            if not target:
                raise HTTPException(404, "目标采集节点不存在")
            if target["node"] == shop["node_id"]:
                if shop.get("state") in ("logged_in", "logging_in", "needs_verification"):
                    return {
                        "ok": True,
                        "shop_id": shop_id,
                        "node": target["node"],
                        "login_state": shop["state"],
                        "requires_login": shop["state"] != "logged_in",
                    }
                return await start_moved(shop_id, target["node"], shop["local_id"])
            if body.login_method not in ("qr", "password"):
                raise HTTPException(400, "请选择有效登录方式")
            if any(n not in hub.peers for n in (target["node"], shop["node_id"])):
                raise HTTPException(409, "原节点和目标节点均需在线，确认停止原节点后才能切换")
            key = f"shop-transfer:{shop_id}"
            pending = await asyncio.to_thread(read_setting, key)
            if pending and pending["target"] != target["node"]:
                old_target = next((n for n in registry if n["node"] == pending["target"]), None)
                old_name = (old_target or {}).get("name", "原目标节点")
                if pending.get("phase") == "target_write_started":
                    raise HTTPException(
                        409, f"上次已向 {old_name} 创建或更新店铺，结果需核对；请先选择 {old_name} 继续切换"
                    )
                # Read the original target directly, not its potentially stale center snapshot.
                old_shops = await remote(pending["target"], "GET", "shops")
                exists = any(
                    s.get("platform", "pdd") == shop.get("platform", "pdd")
                    and s.get("username", "").casefold() == shop.get("username", "").casefold()
                    for s in old_shops
                )
                if exists:
                    raise HTTPException(409, f"上次切换已在 {old_name} 留下店铺，请先选择 {old_name} 完成核对后再切换")
                if pending.get("source") != shop["node_id"]:
                    raise HTTPException(409, "店铺归属已变化，请刷新后核对")
                # A pending transfer to a different target never authorizes merging a pre-existing account here.
                pending = None
            candidates = await remote(target["node"], "GET", "shops")
            found = next(
                (
                    s
                    for s in candidates
                    if s.get("platform", "pdd") == shop.get("platform", "pdd")
                    and s.get("username", "").casefold() == shop.get("username", "").casefold()
                ),
                None,
            )
            if found and not pending:
                retired = await asyncio.to_thread(read_setting, f"retired-shop:{target['node']}:{found['id']}")
                if not retired or retired.get("shop_id") != shop_id:
                    raise HTTPException(409, "目标节点已有相同平台账号，为避免合并错误，请先核对已有店铺")
            password = body.password
            if body.login_method == "password" and not password:
                saved = await remote(shop["node_id"], "POST", f"shops/{shop['local_id']}/transfer-credentials", {})
                password = saved.get("password", "")
                if not password:
                    raise HTTPException(409, "原节点未保存密码，请选择扫码登录或先补充密码")
                saved.clear()
            # Stop cancels active collection before target config is created. No password is persisted centrally.
            if not pending:
                await asyncio.to_thread(
                    write_setting,
                    key,
                    {"target": target["node"], "source": shop["node_id"], "phase": "source_stopping"},
                )
            try:
                await stop_confirmed(remote, shop["node_id"], shop["local_id"])
            except HTTPException as error:
                raise HTTPException(error.status_code, "停止原节点店铺失败：" + str(error.detail)) from None
            from ..shop_name import complete

            name = next((shop[k] for k in ("alias", "detected_name", "name") if complete(shop.get(k))), "")
            config = {
                "username": shop.get("username", ""),
                "name": name,
                "browser": shop.get("browser", "system"),
                "platform": shop.get("platform", "pdd"),
                "operator": ",".join(shop.get("operators") or [shop.get("operator", "")]),
                "login_url": shop.get("login_url", ""),
                "login_method": body.login_method,
                "password": password,
            }
            await asyncio.to_thread(
                write_setting,
                key,
                {"target": target["node"], "source": shop["node_id"], "phase": "target_write_started"},
            )
            if found:
                try:
                    await stop_confirmed(remote, target["node"], found["id"])
                except HTTPException as error:
                    raise HTTPException(error.status_code, "停止目标节点旧店铺失败：" + str(error.detail)) from None
                public = await remote(target["node"], "PUT", f"shops/{found['id']}", config)
            else:
                public = await remote(target["node"], "POST", "shops", config)
            try:
                await asyncio.to_thread(database.move_shop, shop, target["node"], int(public["id"]), public)
            except ValueError as error:
                raise HTTPException(409, str(error))
            await asyncio.to_thread(delete_setting, key)
            # Ownership is committed before starting: a login failure must never replay migration.
            return await start_moved(shop_id, target["node"], public["id"])
