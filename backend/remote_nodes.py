"""Authenticated proxy for explicitly configured browser-service machines."""

import re
from urllib.parse import urlsplit
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

PATH = re.compile(
    r"^(invoice-imports(?:/[a-f0-9]{32}(?:/(?:confirm|files(?:/[a-f0-9]{32}/(?:match|pdf|cancel))?))?)?|invoices/refresh-pending|exports/[a-f0-9]{32}(?:/download)?|home/(?:progress|history)|events|attention(?:/(?:sync|changes|history))?|operators|shops(?:/operators|/import|/tile|/windows|/\d+(?:/(?:start|stop|qr-login|browser|show|hide|debug-on|debug-off|retry|home|ad_logs|ad_products|ad_reports|goodsdata|transactions|overview|products|tickets|reviews|packages|invoices|aftersales|evidence|intercepts|navigator|quality|brands|activities|reconciliation_day|reconciliation_month|screenshot|input))?)?|browsers|logs|todos(?:/sync)?|system/(?:status|summary|progress|sources|settings|concurrency|maintenance|backup|backup-verify|health|priority|retry-scope)|product/\d+/\d+(?:/sync)?|tax-review/\d+/\d{4}-\d{2}|data/(?:tax|ad_logs|ad_products|ad_reports|goodsdata|transactions|overview|products|tickets|reviews|packages|invoices|aftersales|evidence|intercepts|navigator|quality|brands|activities|reconciliation_day|reconciliation_month)(?:/sync)?)$"
)


class NodeInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=300)
    token: str = Field(default="", max_length=256)


def initialize(store):
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS remote_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,url TEXT NOT NULL UNIQUE,token BLOB NOT NULL)""")


def validate_url(value):
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("机器地址需为完整的 HTTP / HTTPS 服务地址，不包含路径或账号密码")
    try:
        parsed.port
    except ValueError:
        raise ValueError("机器端口无效") from None
    return value.strip().rstrip("/")


def get_node(store, node_id):
    with store.connect() as db:
        row = db.execute("SELECT * FROM remote_nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "远程机器不存在")
    result = dict(row)
    result["token"] = store.cipher.decrypt(result["token"]).decode()
    return result


async def check_node(url, token):
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            r = await client.get(url + "/api/shops", headers={"Authorization": "Bearer " + token})
            if r.status_code != 200 or not isinstance(r.json(), list):
                raise ValueError()
    except Exception:
        raise HTTPException(400, "远程服务验证失败，请检查地址、网络和 API 密钥") from None


def create_router(store, authorize):
    initialize(store)
    router = APIRouter(prefix="/api/nodes", dependencies=[Depends(authorize)])

    @router.get("")
    def nodes():
        with store.connect() as db:
            return [dict(r) for r in db.execute("SELECT id,name,url FROM remote_nodes ORDER BY id")]

    @router.post("")
    async def add(value: NodeInput):
        try:
            url = validate_url(value.url)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        if not value.name.strip() or len(value.token) < 16:
            raise HTTPException(400, "请填写机器名称和有效密钥")
        await check_node(url, value.token)
        with store.connect() as db:
            if db.execute("SELECT 1 FROM remote_nodes WHERE url=?", (url,)).fetchone():
                raise HTTPException(409, "该机器地址已添加")
            node_id = db.execute(
                "INSERT INTO remote_nodes(name,url,token) VALUES(?,?,?)",
                (value.name.strip(), url, store.cipher.encrypt(value.token.encode())),
            ).lastrowid
        return {"id": node_id, "name": value.name.strip(), "url": url}

    @router.put("/{node_id}")
    async def edit(node_id: int, value: NodeInput):
        old = get_node(store, node_id)
        token = value.token or old["token"]
        try:
            url = validate_url(value.url)
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        if not value.name.strip() or len(token) < 16:
            raise HTTPException(400, "请填写机器名称和有效密钥")
        await check_node(url, token)
        with store.connect() as db:
            if db.execute("SELECT 1 FROM remote_nodes WHERE url=? AND id<>?", (url, node_id)).fetchone():
                raise HTTPException(409, "该机器地址已添加")
            db.execute(
                "UPDATE remote_nodes SET name=?,url=?,token=? WHERE id=?",
                (value.name.strip(), url, store.cipher.encrypt(token.encode()), node_id),
            )
        return {"id": node_id, "name": value.name.strip(), "url": url}

    @router.delete("/{node_id}")
    def delete(node_id: int):
        with store.connect() as db:
            db.execute("DELETE FROM remote_nodes WHERE id=?", (node_id,))
        return {"ok": True}

    @router.api_route("/{node_id}/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(node_id: int, path: str, request: Request):
        if not PATH.fullmatch(path):
            raise HTTPException(404, "远程接口不在允许范围内")
        node = get_node(store, node_id)
        if path == "events":
            if request.method != "GET":
                raise HTTPException(405, "推送接口仅支持读取")
            client = httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False)
            try:
                upstream = await client.send(
                    client.build_request(
                        "GET",
                        node["url"] + "/api/events",
                        headers={
                            "Authorization": "Bearer " + node["token"],
                            **(
                                {"Last-Event-ID": request.headers["last-event-id"]}
                                if "last-event-id" in request.headers
                                else {}
                            ),
                        },
                    ),
                    stream=True,
                )
                if upstream.status_code != 200:
                    code = upstream.status_code
                    await upstream.aclose()
                    await client.aclose()
                    raise HTTPException(code if code in (401, 403, 404) else 502, "远程推送暂不可用")
            except httpx.HTTPError:
                await client.aclose()
                raise HTTPException(503, "远程推送连接失败") from None

            async def relay():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                relay(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
            )
        if path.startswith("exports/") and path.endswith("/download"):
            client = httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False)
            try:
                upstream = await client.send(
                    client.build_request(
                        "GET", node["url"] + "/api/" + path, headers={"Authorization": "Bearer " + node["token"]}
                    ),
                    stream=True,
                )
            except httpx.HTTPError:
                await client.aclose()
                raise HTTPException(503, "远程文件暂不可用") from None

            async def download_stream():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            return StreamingResponse(
                download_stream(),
                status_code=upstream.status_code,
                headers={
                    k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "content-disposition")
                },
            )
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False, follow_redirects=False) as client:
                result = await client.request(
                    request.method,
                    node["url"] + "/api/" + path,
                    params=request.query_params,
                    headers={"Authorization": "Bearer " + node["token"], "Content-Type": "application/json"},
                    content=await request.body() if request.method != "GET" else None,
                )
            if 300 <= result.status_code < 400:
                raise HTTPException(502, "远程服务地址发生跳转，请更新机器配置")
            return Response(
                result.content,
                status_code=result.status_code,
                headers={
                    "Content-Type": result.headers.get("content-type", "application/json"),
                    "Cache-Control": "no-store",
                    **(
                        {"Content-Disposition": result.headers["content-disposition"]}
                        if "content-disposition" in result.headers
                        else {}
                    ),
                },
            )
        except httpx.HTTPError:
            raise HTTPException(503, "远程机器无法连接，请检查该机器的 Python 服务与网络") from None

    return router
