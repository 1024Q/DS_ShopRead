import asyncio
import base64
import json
import logging
import re
import uuid
import httpx
from urllib.parse import urlsplit
import websockets
from .tunnel_protocol import REQUEST_LIMIT, CONTROL_LIMIT, CHUNK_BYTES

# Same protocol as the local authenticated API, never arbitrary URLs or shell commands.
PATH = re.compile(
    r"^(report-exports(?:/(?:settings|[a-f0-9]{32}(?:/(?:retry|chunk|screenshot))?))?|shops(?:/[1-9]\d*(?:/[a-z_-]+)?|/operators|/import|/tile|/windows)?|browsers|operators|logs|todos(?:/sync)?|attention(?:/[a-z_-]+)?|home/(?:progress|history)|system/[a-z_-]+|data/[a-z_]+(?:/sync)?|product/\d+/\d+(?:/sync)?|tax-review/\d+/\d{4}-\d{2}|invoice-imports(?:/[a-f0-9]{32}(?:/(?:confirm|files(?:/[a-f0-9]{32}(?:/(?:match|pdf|cancel))?)?))?)?|invoices/refresh-pending|exports/[a-f0-9]{32}(?:/download)?|fleet/export)$"
)
# Advertised on connect; bodies travel as raw binary frames only after the center answers with the same capability.
CAPABILITIES = "binary-body"


async def run(url, headers, local, local_token, control_only=False, ssl_context=None, job_wake=None):
    address = urlsplit(url)
    target = (
        ("wss" if address.scheme == "https" else "ws")
        + "://"
        + address.netloc
        + address.path.rstrip("/")
        + "/agent/tunnel"
    )
    if control_only:
        target = target.removesuffix("/tunnel") + "/control-tunnel"
    logger = logging.getLogger("ecmp.tunnel")
    options = {"ssl": ssl_context} if ssl_context and target.startswith("wss") else {}
    while True:
        tasks = {}
        streams = {}
        inputs = {}
        waiting = {}
        download_credits = {}
        try:
            async with websockets.connect(
                target,
                additional_headers={
                    **headers,
                    "X-Tunnel-Capabilities": CAPABILITIES + ",multipart-upload,chunked-body,job-notify",
                },
                max_size=48 * 1024 * 1024,
                max_queue=2 if control_only else 16,
                compression=None,
                ping_interval=20,
                ping_timeout=20,
                proxy=None,
                **options,
            ) as socket:
                lock = asyncio.Lock()
                binary = False

                async def send(value):
                    async with lock:
                        await socket.send(value if isinstance(value, bytes) else json.dumps(value))

                async def respond(ident, status, content, kept):
                    if not binary:
                        return await send(
                            {
                                "type": "response",
                                "id": ident,
                                "status": status,
                                "body": base64.b64encode(content).decode(),
                                "headers": kept,
                            }
                        )
                    async with lock:
                        await socket.send(
                            json.dumps(
                                {"type": "response", "id": ident, "status": status, "binary": True, "headers": kept}
                            )
                        )
                        await socket.send(uuid.UUID(hex=ident).bytes + content)

                async def download(message, body, content_type):
                    ident = message["id"]
                    credit = asyncio.Event()
                    download_credits[ident] = credit
                    started = False
                    try:
                        async with local.stream(
                            message["method"],
                            "/api/" + message["path"],
                            params=message.get("query", ""),
                            content=body or None,
                            headers={"Content-Type": content_type},
                        ) as response:
                            await send(
                                {
                                    "type": "response",
                                    "id": ident,
                                    "status": response.status_code,
                                    "chunked": True,
                                    "headers": {
                                        k: v
                                        for k, v in response.headers.items()
                                        if k in ("content-type", "content-disposition")
                                    },
                                }
                            )
                            started = True
                            async for chunk in response.aiter_bytes(CHUNK_BYTES):
                                credit.clear()
                                await send(uuid.UUID(hex=ident).bytes + chunk)
                                await asyncio.wait_for(credit.wait(), 60)
                            await send({"type": "download_end", "id": ident})
                    except Exception:
                        if started:
                            await send({"type": "download_error", "id": ident})
                        else:
                            raise
                    finally:
                        download_credits.pop(ident, None)

                async def request(message):
                    ident = message["id"]
                    try:
                        if not PATH.fullmatch(message["path"]) or message["method"] not in (
                            "GET",
                            "POST",
                            "PUT",
                            "DELETE",
                        ):
                            raise ValueError("接口不允许")
                        body = (
                            message["content"]
                            if "content" in message
                            else base64.b64decode(message.get("body", ""), validate=True)
                        )
                        if len(body) > 32 * 1024 * 1024:
                            raise ValueError("请求过大")
                        content_type = message.get("content_type", "application/json")
                        if content_type != "application/json" and not (
                            content_type.startswith("multipart/form-data;")
                            and re.fullmatch(r"invoice-imports/[a-f0-9]{32}/files", message["path"])
                        ):
                            raise ValueError("上传类型不允许")
                        if (
                            message.get("stream")
                            and message["method"] == "GET"
                            and message["path"].endswith(("/download", "/pdf", "/screenshot"))
                        ):
                            return await download(message, body, content_type)
                        response = await local.request(
                            message["method"],
                            "/api/" + message["path"],
                            params=message.get("query", ""),
                            content=body or None,
                            headers={"Content-Type": content_type},
                        )
                        await respond(
                            ident,
                            response.status_code,
                            response.content,
                            {k: v for k, v in response.headers.items() if k in ("content-type", "content-disposition")},
                        )
                    except Exception as error:
                        # Never log bodies/headers: transfer requests can contain passwords.
                        logger.warning(
                            "节点接口 %s %s 未确认（%s）",
                            message.get("method"),
                            message.get("path"),
                            type(error).__name__,
                        )
                        detail = (
                            "节点本地服务响应超时，操作可能仍在执行，请核对实际状态"
                            if isinstance(error, httpx.TimeoutException)
                            else "无法连接节点本地服务，请检查采集服务运行状态"
                            if isinstance(error, httpx.ConnectError)
                            else "节点请求未确认，请核对执行结果后重试"
                        )
                        await respond(
                            ident,
                            502,
                            json.dumps({"detail": detail}, ensure_ascii=False).encode(),
                            {"content-type": "application/json"},
                        )

                async def control(message):
                    ident = message["id"]
                    shop = int(message["shop"])
                    try:
                        async with websockets.connect(
                            f"ws://127.0.0.1:{local.base_url.port}/api/shops/{shop}/control",
                            additional_headers={"Authorization": "Bearer " + local_token},
                            max_size=16 * 1024 * 1024,
                            max_queue=1,
                            compression=None,
                            proxy=None,
                        ) as upstream:
                            streams[ident] = upstream
                            for data in inputs.pop(ident, []):
                                await upstream.send(data)
                            async for data in upstream:
                                await send(
                                    uuid.UUID(hex=ident).bytes + data
                                    if isinstance(data, bytes)
                                    else {"type": "stream", "id": ident, "data": data}
                                )
                    finally:
                        streams.pop(ident, None)
                        await send({"type": "closed", "id": ident})

                async def start(message):
                    ident = message["id"]
                    if len(tasks) >= (CONTROL_LIMIT if control_only else REQUEST_LIMIT):
                        if message["type"] == "request":
                            await respond(ident, 429, b'{"detail":"Node busy"}', {"content-type": "application/json"})
                        else:
                            await send({"type": "closed", "id": ident})
                        return
                    if message["type"] == "open":
                        inputs[ident] = []
                    task = asyncio.create_task(request(message) if message["type"] == "request" else control(message))
                    tasks[ident] = task

                    def done(t, key=ident):
                        tasks.pop(key, None)
                        if not t.cancelled():
                            t.exception()

                    task.add_done_callback(done)

                async for wire in socket:
                    if isinstance(wire, bytes):
                        # Body frame for a request announced just before it on this socket.
                        message = waiting.pop(uuid.UUID(bytes=wire[:16]).hex, None) if len(wire) >= 16 else None
                        if message:
                            message["content"] = wire[16:]
                            await start(message)
                        continue
                    message = json.loads(wire)
                    kind = message.get("type")
                    ident = message.get("id")
                    if kind == "hello":
                        binary = CAPABILITIES in message.get("capabilities", [])
                    elif kind == "job_available":
                        if job_wake is not None:
                            job_wake.set()
                    elif kind in ("request", "open"):
                        if control_only and kind == "request":
                            continue
                        if kind == "request" and message.get("binary"):
                            if len(waiting) < REQUEST_LIMIT:
                                waiting[ident] = message
                            else:
                                await respond(
                                    ident, 429, b'{"detail":"Node busy"}', {"content-type": "application/json"}
                                )
                            continue
                        await start(message)
                    elif kind == "chunk_ack":
                        if ident in download_credits:
                            download_credits[ident].set()
                    elif kind == "input":
                        if ident in streams:
                            await streams[ident].send(message["data"])
                        elif ident in inputs and len(inputs[ident]) < 16:
                            inputs[ident].append(message["data"])
                    elif kind == "close" and ident in tasks:
                        tasks[ident].cancel()
        except OSError, websockets.WebSocketException, ValueError:
            logger.warning("%s隧道断开，5 秒后重连；未确认操作不会自动重发", "远控" if control_only else "业务")
        except Exception:
            logger.exception("%s隧道异常，5 秒后重连；未确认操作不会自动重发", "远控" if control_only else "业务")
        finally:
            pending = list(tasks.values())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(5)
