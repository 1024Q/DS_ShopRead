"""Address-only enrollment: pending credentials never authenticate as nodes."""

import time
import uuid
from collections import defaultdict, deque
from threading import Lock
from fastapi import Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import Table, Column, String, Float, select, func
from .database import metadata, nodes, node_ids, digest

requests = Table(
    "center_enrollments",
    metadata,
    Column("id", String, primary_key=True),
    Column("token_hash", String, unique=True, nullable=False),
    Column("name", String, nullable=False),
    Column("address", String),
    Column("version", String),
    Column("state", String, nullable=False),
    Column("created", Float, nullable=False),
)


class Enrollment(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(default="", max_length=80)


class Decision(BaseModel):
    action: str = Field(pattern="^(approve|reject)$")


def install(app, database, admin):
    requests.create(database.engine, checkfirst=True)
    attempts = defaultdict(deque)
    lock = Lock()

    def public(row):
        return {k: row[k] for k in ("id", "name", "address", "version", "state", "created")} | {
            "code": row["token_hash"][:12].upper()
        }

    @app.post("/agent/enroll")
    def enroll(body: Enrollment, request: Request, authorization: str = Header(default="")):
        token = authorization.removeprefix("Bearer ")
        if (
            not authorization.startswith("Bearer ")
            or not 43 <= len(token) <= 128
            or not all(c.isalnum() or c in "-_" for c in token)
        ):
            raise HTTPException(400, "申请凭据无效，请使用新版采集客户端")
        hashed = digest(token)
        now = time.time()
        address = request.client.host if request.client else "unknown"
        with lock:
            # Limit new enrollment attempts; repeated polls reuse the persisted request.
            with database.engine.begin() as db:
                node = db.execute(select(nodes).where(nodes.c.token_hash == hashed)).mappings().first()
                if node:
                    return {"state": "approved", "id": node["id"], "name": node["name"]}
                row = db.execute(select(requests).where(requests.c.token_hash == hashed)).mappings().first()
                if row:
                    value = public(row)
                    if value["state"] == "approved":
                        value["state"] = "revoked"
                    if value["state"] == "pending" and now - row["created"] > 7 * 86400:
                        value["state"] = "expired"
                    return value
                for key in list(attempts):
                    while attempts[key] and attempts[key][0] < now - 3600:
                        attempts[key].popleft()
                    if not attempts[key]:
                        del attempts[key]
                if len(attempts[address]) >= 10 or len(attempts) >= 1000:
                    raise HTTPException(429, "接入申请过于频繁，请稍后重试")
                attempts[address].append(now)
                count = db.execute(
                    select(func.count())
                    .select_from(requests)
                    .where(requests.c.state == "pending", requests.c.created > now - 7 * 86400)
                ).scalar()
                if count >= 200:
                    raise HTTPException(429, "待审批申请已满，请联系中心管理员")
                row = {
                    "id": str(uuid.uuid4()),
                    "token_hash": hashed,
                    "name": body.name.strip() or "采集机器",
                    "address": address,
                    "version": body.version,
                    "state": "pending",
                    "created": now,
                }
                db.execute(requests.insert().values(**row))
                return public(row)

    @app.get("/admin/enrollments", dependencies=[Depends(admin)])
    def listing():
        with database.engine.connect() as db:
            return [
                public(r)
                for r in db.execute(
                    select(requests)
                    .where(requests.c.state == "pending", requests.c.created > time.time() - 7 * 86400)
                    .order_by(requests.c.created)
                ).mappings()
            ]

    @app.post("/admin/enrollments/{ident}", dependencies=[Depends(admin)])
    def decide(ident: str, body: Decision):
        with lock, database.engine.begin() as db:
            row = db.execute(select(requests).where(requests.c.id == ident).with_for_update()).mappings().first()
            if not row:
                raise HTTPException(404, "接入申请不存在")
            state = "approved" if body.action == "approve" else "rejected"
            if row["state"] == state:
                return {"ok": True, "state": state}
            if row["state"] != "pending" or row["created"] < time.time() - 7 * 86400:
                raise HTTPException(409, "申请已处理或过期，请刷新")
            if state == "approved":
                db.execute(nodes.insert().values(id=ident, name=row["name"], token_hash=row["token_hash"], seen=0))
                db.execute(node_ids.insert().values(node=ident))
            db.execute(requests.update().where(requests.c.id == ident).values(state=state))
            return {"ok": True, "state": state}
