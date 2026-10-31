import os
import secrets
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from .database import Database, nodes, snapshots, jobs


class Registration(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class Snapshot(BaseModel):
    shop: str = Field(max_length=100)
    scope: str = Field(max_length=500)
    updated: float = Field(ge=0)
    payload: dict


class Batch(BaseModel):
    records: list[Snapshot] = Field(max_length=100)


class Job(BaseModel):
    node: str
    kind: str = Field(pattern=r"^[a-z_]{1,40}$")
    shop_id: int = Field(gt=0)
    date_from: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    date_to: str = Field(default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class Completion(BaseModel):
    lease: str
    result: dict


def create_app(database=None, admin_token=None):
    from .worker_guard import require_single_worker

    require_single_worker()
    token = admin_token or os.environ.get("ECMP_CENTER_ADMIN_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("ECMP_CENTER_ADMIN_TOKEN 至少需要 32 个字符")
    if database is None:
        url = os.environ.get("ECMP_DATABASE_URL", "")
        if not url.startswith("postgresql+psycopg://"):
            raise RuntimeError("中心服务需要独立 PostgreSQL：配置 ECMP_DATABASE_URL")
        database = Database(url)
    app = FastAPI(title="ECMP Center", docs_url=None, redoc_url=None)
    from .login_guard import LoginGuard

    login_guard = LoginGuard()

    def admin(request: Request, authorization: str = Header(default="")):
        from .login_guard import client_address

        address, trusted = client_address(request)
        if trusted:
            login_guard.check(address)
        if not secrets.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            login_guard.reject(address)
        login_guard.verified(address)

    def agent(x_node_id: str = Header(default=""), authorization: str = Header(default="")):
        if not database.authenticate(x_node_id, authorization.removeprefix("Bearer ")):
            raise HTTPException(401, "采集节点身份验证失败")
        return x_node_id

    from .enrollment import install as install_enrollment

    install_enrollment(app, database, admin)

    @app.get("/agent/identify")
    def identify(authorization: str = Header(default="")):
        from sqlalchemy import select
        from .database import digest

        if not authorization.startswith("Bearer ") or len(authorization) < 39:
            raise HTTPException(401, "节点密钥无效或已被撤销")
        with database.engine.connect() as db:
            row = (
                db.execute(select(nodes.c.id, nodes.c.name).where(nodes.c.token_hash == digest(authorization[7:])))
                .mappings()
                .first()
            )
        if not row:
            raise HTTPException(401, "节点密钥无效或已被撤销")
        return {**dict(row), "connected": row["id"] in app.state.hub.peers}

    @app.get("/health")
    def health():
        from sqlalchemy import text

        try:
            with database.engine.connect() as db:
                db.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(503, "中心数据库不可用") from None
        return {"ok": True, "role": "center", "database": "connected"}

    @app.post("/admin/nodes", dependencies=[Depends(admin)])
    def register(body: Registration):
        return database.register(body.name)

    @app.get("/admin/nodes", dependencies=[Depends(admin)])
    def list_nodes():
        return [{**row, "id": row["node"]} for row in database.registry()]

    @app.put("/admin/nodes/{node}", dependencies=[Depends(admin)])
    def rename(node: str, body: Registration):
        from sqlalchemy import update

        with database.engine.begin() as db:
            result = db.execute(
                update(nodes).where(nodes.c.id == node, nodes.c.token_hash != "").values(name=body.name)
            )
            if not result.rowcount:
                raise HTTPException(404, "节点不存在")
        return {"ok": True}

    @app.delete("/admin/nodes/{node}", dependencies=[Depends(admin)])
    async def revoke(node: str):
        import asyncio
        from sqlalchemy import update

        def remove():
            with database.engine.begin() as db:
                result = db.execute(update(nodes).where(nodes.c.id == node).values(token_hash="", seen=0))
                if not result.rowcount:
                    raise HTTPException(404, "节点不存在")
                db.execute(
                    update(jobs)
                    .where(jobs.c.node == node, jobs.c.state.in_(["queued", "leased"]))
                    .values(state="failed", result={"ok": False, "message": "节点已移除"})
                )
            database.invalidate_node(node)

        await asyncio.to_thread(remove)
        await app.state.hub.revoke(node)
        return {"ok": True, "snapshots_preserved": True}

    @app.get("/admin/snapshots", dependencies=[Depends(admin)])
    def list_snapshots(node: str = "", shop: str = "", limit: int = 0):
        filters = {k: v for k, v in {"node": node, "shop": shop}.items() if v}
        if limit <= 0:
            return database.list(snapshots, columns=["node", "shop", "scope", "updated"], **filters)
        # Bounded, newest first: the console only needs recent scopes on each refresh.
        from sqlalchemy import select

        statement = select(snapshots.c.node, snapshots.c.shop, snapshots.c.scope, snapshots.c.updated)
        for key, value in filters.items():
            statement = statement.where(snapshots.c[key] == value)
        statement = statement.order_by(snapshots.c.updated.desc()).limit(min(limit, 5000))
        with database.engine.connect() as db:
            return [dict(row) for row in db.execute(statement).mappings()]

    @app.get("/admin/data", dependencies=[Depends(admin)])
    def data(node: str, shop: str, scope: str):
        return database.list(snapshots, node=node, shop=shop, scope=scope)

    @app.get("/admin/jobs", dependencies=[Depends(admin)])
    def list_jobs():
        return database.list(jobs)

    @app.post("/admin/jobs", dependencies=[Depends(admin)])
    def enqueue(body: Job):
        try:
            return {"id": database.enqueue(body.node, body.model_dump(exclude={"node"}))}
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.get("/agent/receipts")
    def receipts(node=Depends(agent)):
        from sqlalchemy import select
        from .database import settings

        prefix = f"retired-shop:{node}:"
        with database.engine.connect() as db:
            ignored = [
                key[len(prefix) :]
                for key in db.execute(select(settings.c.key).where(settings.c.key.like(prefix + "%"))).scalars()
            ]
        return {
            "records": database.list(snapshots, columns=["shop", "scope", "updated"], node=node),
            "ignored_shops": ignored,
        }

    @app.post("/agent/snapshots")
    def ingest(body: Batch, node=Depends(agent)):
        database.ingest(node, [r.model_dump() for r in body.records])
        return {"ok": True}

    @app.post("/agent/claim")
    def claim(node=Depends(agent)):
        return database.claim(node)

    @app.post("/agent/jobs/{job}/finish")
    def finish(job: str, body: Completion, node=Depends(agent)):
        if not database.finish(node, job, body.lease, body.result):
            raise HTTPException(409, "任务租约已过期")
        return {"ok": True}

    from .api import install
    from .order_api import install as install_orders

    install_orders(app, database, agent)
    install(app, database, admin, agent, token)
    from .updates import install as install_updates

    install_updates(app, agent)
    return app
