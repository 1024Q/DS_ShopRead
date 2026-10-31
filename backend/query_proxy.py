import os
import httpx
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask


def local_read(method, path):
    return method == "GET" and (
        path.startswith("/api/exports/")
        or path
        in (
            "/api/fleet",
            "/api/fleet/export",
            "/health",
            "/api/events",
            "/api/attention",
            "/api/attention/history",
            "/api/attention/changes",
            "/api/todos",
            "/api/shops",
            "/api/browsers",
            "/api/operators",
            "/api/logs",
            "/api/home/progress",
            "/api/home/history",
            "/api/system/summary",
            "/api/system/performance",
        )
        or path.startswith("/api/data/")
        and not path.endswith("/sync")
        or path.startswith("/api/product/")
    )


_client = None


async def close_client():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def forward(request):
    global _client
    # Fixed trusted loopback destination; never derive the upstream from user input.
    if _client is None:
        _client = httpx.AsyncClient(
            base_url="http://127.0.0.1:" + str(int(os.environ.get("PDD_COLLECTOR_PORT", "8001"))),
            timeout=httpx.Timeout(300, connect=3),
            trust_env=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
    client = _client

    async def close(response):
        await response.aclose()

    try:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection", "content-length")
        }
        target = request.url.path + ("?" + request.url.query if request.url.query else "")
        response = await client.send(
            client.build_request(request.method, target, headers=headers, content=await request.body()), stream=True
        )
        outgoing = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in ("connection", "transfer-encoding", "content-encoding", "content-length")
        }
        return StreamingResponse(
            response.aiter_bytes(),
            status_code=response.status_code,
            headers=outgoing,
            background=BackgroundTask(close, response),
        )
    except httpx.HTTPError:
        return JSONResponse({"detail": "采集服务暂未连接；已存业务数据仍可查看"}, status_code=503)
