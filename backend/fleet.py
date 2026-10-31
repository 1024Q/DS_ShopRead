from .filter_scope import matches

"""Background machine summaries. The dashboard reads persisted mirrors, never fans out."""
import asyncio, hashlib, json, math, time
from datetime import datetime
import httpx
from fastapi import APIRouter, Depends, Request, Response
from .business_reader import BEIJING
from .business import options_for
from .platform_support import supports
from .remote_nodes import get_node


def export(service):
    store = service.store
    with store.connect() as db:
        machine = db.execute("SELECT value FROM settings WHERE key='insights_identity'").fetchone()[0]
        counts = {
            r["shop_id"]: r["total"]
            for r in db.execute("SELECT shop_id,sum(total) AS total FROM attention_counts GROUP BY shop_id")
        }
        stamps = {
            r["shop_id"]: r["updated"]
            for r in db.execute("SELECT shop_id,min(updated) AS updated FROM attention_snapshots GROUP BY shop_id")
        }
    data = service.data("overview", options_for("overview"))
    metrics = {r["shop_id"]: r for r in data.get("metrics", [])}
    sources = {r["shop_id"]: r for r in data.get("sources", [])}
    rows = []
    for shop in store.list():
        sid = shop["id"]
        source = sources.get(sid, {})
        # Same account on two machines must not double the turnover. Names are never identity keys.
        username = shop.get("username", "").strip().casefold()
        identity = (
            hashlib.sha256((shop.get("platform", "pdd") + ":" + username).encode()).hexdigest()
            if username
            else machine + ":" + str(sid)
        )
        rows.append(
            {
                "shop_id": sid,
                "account_key": identity,
                "name": shop["name"],
                "platform": shop.get("platform", "pdd"),
                "operator": shop.get("operator", ""),
                "operators": shop.get("operators", []),
                "state": service.session_getter(sid).state,
                "metrics": {
                    k: v
                    for k, v in metrics.get(sid, {}).items()
                    if k
                    in (
                        "sales",
                        "orders",
                        "visitors",
                        "ad_spend",
                        "pending_shipping",
                        "pending_aftersales",
                        "pending_tickets",
                    )
                },
                "updated_at": source.get("updated_at"),
                "stale": source.get("stale", True),
                "complete": source.get("complete", False),
                "supported": supports(shop.get("platform", "pdd"), "overview"),
                "attention": counts.get(sid),
                "attention_updated": stamps.get(sid),
                "day_pending": source.get("day_pending", False),
            }
        )
    return {
        "collector_online": getattr(service, "public_summary", {}).get("collector_online", True),
        "version": 1,
        "machine": machine,
        "date": datetime.now(BEIJING).date().isoformat(),
        "rows": rows,
    }


class Fleet:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.client = None
        self.node_states = {}
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS fleet_mirrors(node_id INTEGER PRIMARY KEY,payload TEXT,updated REAL,checked REAL,error TEXT,fingerprint TEXT)"
            )

    def save(self, node_id, payload, error="", fingerprint="local"):
        now = time.time()
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM fleet_mirrors WHERE node_id=?", (node_id,)).fetchone()
            text = (
                json.dumps(payload, ensure_ascii=False)
                if payload
                else old["payload"]
                if old and old["fingerprint"] == fingerprint
                else None
            )
            updated = now if payload else old["updated"] if old and old["fingerprint"] == fingerprint else 0
            db.execute(
                "INSERT OR REPLACE INTO fleet_mirrors(node_id,payload,updated,checked,error,fingerprint) VALUES(?,?,?,?,?,?)",
                (node_id, text, updated, now, error, fingerprint),
            )

    async def tick(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=5),
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        await asyncio.to_thread(self.save, 0, await asyncio.to_thread(export, self.service))
        with self.store.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM remote_nodes")]
        gate = asyncio.Semaphore(4)

        async def pull(sid):
            async with gate:
                try:
                    node = get_node(self.store, sid)
                except Exception:
                    return
                fingerprint = hashlib.sha256((node["url"] + node["token"]).encode()).hexdigest()
                state = self.node_states.setdefault(sid, {})
                if state.get("fingerprint") != fingerprint:
                    state.clear()
                    state["fingerprint"] = fingerprint
                if state.get("next", 0) > time.monotonic():
                    return
                try:
                    headers = {"Authorization": "Bearer " + node["token"]}
                    if state.get("etag"):
                        headers["If-None-Match"] = state["etag"]
                    r = await self.client.get(node["url"] + "/api/fleet/export", headers=headers)
                    if r.status_code == 304:
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE fleet_mirrors SET checked=?,error='' WHERE node_id=? AND fingerprint=?",
                                (time.time(), sid, fingerprint),
                            )
                        state.update(failures=0, next=time.monotonic() + 55)
                        return
                    if r.status_code == 404:
                        raise ValueError("远端版本未支持汇总，请更新远端服务")
                    if r.status_code in (401, 403):
                        raise ValueError("远端鉴权失败，请检查连接密钥")
                    r.raise_for_status()
                    payload = r.json()
                    if payload.get("version") != 1 or not isinstance(payload.get("rows"), list):
                        raise ValueError("远端汇总格式不兼容")
                    await asyncio.to_thread(self.save, sid, payload, "", fingerprint)
                    state.update(etag=r.headers.get("etag"), failures=0, next=time.monotonic() + 55)
                except Exception as error:
                    failures = state.get("failures", 0) + 1
                    state.update(failures=failures, next=time.monotonic() + min(300, 30 * 2 ** min(failures, 4)))
                    await asyncio.to_thread(
                        self.save,
                        sid,
                        None,
                        str(error) if isinstance(error, ValueError) else "远端暂不可用，保留上次结果",
                        fingerprint,
                    )

        await asyncio.gather(*(pull(sid) for sid in ids))

    async def run(self):
        try:
            while True:
                started = time.monotonic()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.log_event("warning", "system", "fleet_sync", "跨机器汇总暂未完成，下轮重试")
                await asyncio.sleep(max(1, 60 - (time.monotonic() - started)))
        finally:
            if self.client:
                await self.client.aclose()
                self.client = None

    def read(self, platform="", operator=""):
        now = time.time()
        today = datetime.now(BEIJING).date().isoformat()
        with self.store.connect() as db:
            nodes = [{"id": 0, "name": "主服务"}] + [dict(r) for r in db.execute("SELECT id,name FROM remote_nodes")]
            mirrors = {r["node_id"]: dict(r) for r in db.execute("SELECT * FROM fleet_mirrors")}
        machines = []
        rows = []
        seen_machines = set()
        for node in nodes:
            m = mirrors.get(node["id"], {})
            data = json.loads(m["payload"]) if m.get("payload") else {}
            if node["id"] and m:
                try:
                    configured = get_node(self.store, node["id"])
                    fingerprint = hashlib.sha256((configured["url"] + configured["token"]).encode()).hexdigest()
                    if fingerprint != m.get("fingerprint"):
                        m = {}
                        data = {}
                except Exception:
                    m = {}
                    data = {}
            duplicate = bool(data.get("machine") and data["machine"] in seen_machines)
            if data.get("machine"):
                seen_machines.add(data["machine"])
            online = bool(
                m.get("checked")
                and now - m["checked"] < 100
                and not m.get("error")
                and data.get("collector_online", True)
            )
            machines.append(
                {
                    "node_id": node["id"],
                    "name": node["name"],
                    "online": online,
                    "updated_at": m.get("updated"),
                    "error": m.get("error") or ("尚未完成首次汇总" if not data else ""),
                    "duplicate": duplicate,
                }
            )
            if duplicate:
                continue
            for shop in data.get("rows", []):
                if (
                    not matches(platform, shop["platform"])
                    or operator
                    and operator not in (shop.get("operators") or [shop.get("operator")])
                ):
                    continue
                rows.append(
                    {
                        **shop,
                        "id": f"{node['id']}:{shop['shop_id']}",
                        "node_id": node["id"],
                        "machine_name": node["name"],
                        "online": online,
                        "current_day": data.get("date") == today,
                        "mirror_updated": m.get("updated"),
                    }
                )
        chosen = {}
        for row in rows:
            key = row["account_key"]
            old = chosen.get(key)
            rank = lambda r: (r["current_day"], r["online"], str(r.get("updated_at") or ""), -r["node_id"])
            if old is None or rank(row) > rank(old):
                chosen[key] = row
        for row in rows:
            row["duplicate_account"] = chosen[row["account_key"]] is not row
        totals = {}
        coverage = {}
        for key in (
            "sales",
            "orders",
            "visitors",
            "ad_spend",
            "pending_shipping",
            "pending_aftersales",
            "pending_tickets",
        ):
            values = [
                r["metrics"].get(key)
                for r in chosen.values()
                if r["current_day"] and r["online"] and not r.get("day_pending")
            ]
            values = [v for v in values if type(v) in (int, float) and math.isfinite(v)]
            totals[key] = sum(values) if values else None
            coverage[key] = len(values)
        return {
            "rows": rows,
            "machines": machines,
            "totals": totals,
            "coverage": coverage,
            "unique_shops": len(chosen),
            "date": today,
            "interval_seconds": 60,
        }


def router(fleet, authorize):
    api = APIRouter(dependencies=[Depends(authorize)])

    @api.get("/api/fleet")
    async def summary(platform: str = "", operator: str = ""):
        return await asyncio.to_thread(fleet.read, platform, operator)

    @api.get("/api/fleet/export")
    async def snapshot(request: Request):
        data = await asyncio.to_thread(export, fleet.service)
        body = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        tag = '"' + hashlib.sha256(body).hexdigest() + '"'
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers={"ETag": tag})
        return Response(body, media_type="application/json", headers={"ETag": tag})

    return api
