from .. import recovery_log
import asyncio
import hashlib
import json
import re
import time
from urllib.parse import urlencode
from fastapi import Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, func, delete
from .database import snapshots
from .business import CentralBusiness, selected_node
from .agent_tunnel import PATH
from .tunnel import Hub, content as body_of


def install(app, database, admin, agent, token):
    import secrets

    hub = Hub()
    hub.job_changes = getattr(database, "job_changes", None)
    app.state.hub = hub
    from .shop_transfer import install as install_shop_transfer

    install_shop_transfer(app, database, hub, admin)
    from ..query_cache import QueryCache

    query_cache = QueryCache(ttl=5, max_bytes=32 * 1024 * 1024)
    service = CentralBusiness(database)
    from .overview_snapshot import install as install_overview_snapshot

    install_overview_snapshot(app, service, admin)
    from ..business import create_router

    app.include_router(create_router(service, admin))
    app.include_router(service.export_jobs.router(admin))
    from .insights import install as install_insights

    install_insights(app, database, service, admin)
    from .backup import install as install_backup

    install_backup(app, database, admin)
    from .cost_tables import install as install_cost_tables

    install_cost_tables(app, database, admin)
    from .report_exports import install as install_report_exports

    install_report_exports(app, database, hub, admin)
    from .home_collection_control import install as install_home_control

    install_home_control(app, database, hub, admin)
    install_cleanup(app, database)

    @app.middleware("http")
    async def node_context(request, call_next):
        match = re.match(r"^/api/nodes/(\d+)/proxy/(.+)$", request.url.path)
        node = None
        if match:
            node = await asyncio.to_thread(database.resolve_node, int(match[1]))
            if not node:
                return Response(status_code=404)
            request.scope["path"] = "/api/" + match[2]
            request.scope["raw_path"] = request.scope["path"].encode()
        context = selected_node.set(node)
        try:
            eligible = (
                request.method == "GET"
                and (
                    request.scope["path"].startswith("/api/data/")
                    or request.scope["path"] in ("/api/attention", "/api/todos", "/api/fleet")
                )
                and request.query_params.get("export", "false") not in ("true", "1")
                and secrets.compare_digest(
                    request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
                )
            )
            if not eligible:
                return await call_next(request)

            def latest_revision():
                with database.engine.connect() as db:
                    return (
                        db.execute(
                            select(func.max(snapshots.c.updated)).where(
                                ~snapshots.c.scope.in_(["shop_metadata", "collector_status"]),
                                ~snapshots.c.scope.like("response:%"),
                            )
                        ).scalar()
                        or 0
                    )

            revision = await asyncio.to_thread(latest_revision)
            cache_key = (node, request.scope["path"], request.scope["query_string"])
            saved = query_cache.get(cache_key, revision)
            if saved:
                return Response(
                    saved[0], media_type=saved[1], headers={"X-Query-Cache": "hit", "Cache-Control": "no-store"}
                )
            response = await call_next(request)
            if response.status_code != 200:
                return response
            content = b"".join([part async for part in response.body_iterator])
            kind = response.headers.get("content-type", "application/json")
            query_cache.put(cache_key, revision, content, kind)
            return Response(
                content, status_code=response.status_code, headers={**dict(response.headers), "X-Query-Cache": "miss"}
            )
        finally:
            selected_node.reset(context)

    @app.websocket("/agent/tunnel")
    @app.websocket("/agent/control-tunnel")
    async def node_tunnel(ws: WebSocket):
        node = ws.headers.get("x-node-id", "")
        credential = ws.headers.get("authorization", "").removeprefix("Bearer ")
        if not await asyncio.to_thread(database.authenticate, node, credential):
            await ws.close(code=4401)
            return
        try:
            await hub.attach(
                node,
                ws,
                control=ws.url.path.endswith("/control-tunnel"),
                check=lambda: database.authenticate(node, credential, fresh=True),
            )
        except Exception:
            recovery_log.suppressed("center/api.py:74 恢复步骤未完成", warning=True)

    @app.websocket("/api/shops/{shop_id}/control")
    @app.websocket("/api/nodes/{node_id}/proxy/shops/{shop_id}/control")
    async def browser(ws: WebSocket, shop_id: int, node_id: int | None = None):
        if not secrets.compare_digest(ws.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
            await ws.close(code=4401)
            return
        shop = next((s for s in await asyncio.to_thread(database.shops) if s["id"] == shop_id), None)
        node = await asyncio.to_thread(database.resolve_node, node_id) if node_id else None
        if not shop or node_id and shop["node_id"] != node:
            await ws.close(code=4404)
            return
        try:
            await hub.control(shop["node_id"], shop["local_id"], ws)
        except Exception:
            recovery_log.suppressed("center/api.py:84 恢复步骤未完成", warning=True)

    @app.get("/api/shops", dependencies=[Depends(admin)])
    def shops():
        return service.store.list()

    @app.get("/api/nodes", dependencies=[Depends(admin)])
    def nodes():
        return [{**n, "url": "主动连接", "online": n["node"] in hub.peers, "center": True} for n in database.registry()]

    @app.get("/api/events", dependencies=[Depends(admin)])
    async def events(request: Request):
        async def stream():
            from .business_events import heads, notification

            previous = None
            while not await request.is_disconnected():
                generation = database.changes.revision()
                current = await asyncio.to_thread(heads, database)
                if previous != current:
                    value = notification(previous, current, await asyncio.to_thread(database.shops))
                    yield "id: " + value["version"] + "\nevent: database\ndata: " + json.dumps(value) + "\n\n"
                    previous = current
                else:
                    yield ": heartbeat\n\n"
                await database.changes.wait(generation, 5)
                # Coalesce a batch of simultaneous shop uploads into one refresh.
                await asyncio.sleep(0.15)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    def remap(value, node, root_shops=False):
        # Resolve ownership once per response, never once per nested field/row.
        mapping = {s["local_id"]: s["id"] for s in database.shops() if s["node_id"] == node}

        def visit(item, shops=False):
            if isinstance(item, list):
                return [visit(v, shops) for v in item]
            if isinstance(item, dict):
                return {
                    k: mapping.get(v, v)
                    if (k == "shop_id" or shops and k == "id") and isinstance(v, int)
                    else visit(v, k == "shops")
                    for k, v in item.items()
                }
            return item

        return visit(value, root_shops)

    refreshing = {}
    refresh_errors = {}
    progress_versions = {}

    async def read_remote(node, path, query, cache_key):
        try:
            generation = database.changes.revision(node)
            result = await hub.request(node, "GET", path, query)
            if result["status"] == 200:
                payload = json.loads(body_of(result))
                await asyncio.to_thread(
                    database.ingest,
                    node,
                    [{"shop": "0", "scope": cache_key, "updated": time.time(), "payload": {"value": payload}}],
                )
                progress_versions[node, cache_key] = generation
                refresh_errors.pop((node, cache_key), None)
                return payload
            raise HTTPException(result["status"], "节点数据暂不可用")
        except Exception as error:
            refresh_errors[(node, cache_key)] = {
                "message": str(error.detail)
                if isinstance(error, HTTPException)
                else "节点进度刷新失败，请检查节点连接与本地服务",
                "at": time.time(),
            }
            if len(refresh_errors) > 512:
                refresh_errors.pop(next(iter(refresh_errors)))
            raise
        finally:
            refreshing.pop((node, cache_key), None)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE"], dependencies=[Depends(admin)])
    async def compatible(path: str, request: Request):
        if path.endswith("/transfer-credentials"):
            raise HTTPException(404, "接口不存在")
        if not PATH.fullmatch(path):
            raise HTTPException(404, "接口不存在")
        from ..file_upload import read_body

        content_type = request.headers.get("content-type", "application/json")
        is_upload = content_type.startswith("multipart/form-data;")
        if is_upload and not (request.method == "POST" and re.fullmatch(r"invoice-imports/[a-f0-9]{32}/files", path)):
            raise HTTPException(415, "此接口不接受文件上传")
        node = selected_node.get()
        records = database.shops()
        body = await read_body(request, 32 * 1024 * 1024)
        query = list(request.query_params.multi_items())
        match = re.match(r"^(shops|product|tax-review)/(\d+)(.*)$", path)
        if match:
            shop = next((s for s in records if s["id"] == int(match[2]) and (not node or s["node_id"] == node)), None)
            if not shop:
                raise HTTPException(404, "店铺不存在")
            if request.method != "GET":
                from .database import settings

                if path.endswith("/start"):
                    from .shop_transfer import release_revoked_transfer

                    release_revoked_transfer(database, shop)
                with database.engine.connect() as db:
                    moving = db.execute(
                        select(settings.c.key).where(settings.c.key == f"shop-transfer:{shop['id']}")
                    ).first()
                if moving:
                    raise HTTPException(409, "该店铺节点切换尚未完成，请先通过“切换节点”继续核对")
            node = shop["node_id"]
            path = f"{match[1]}/{shop['local_id']}{match[3]}"
        if not node:
            registry = database.registry()
            if len(registry) == 1:
                node = registry[0]["node"]
            elif request.method == "POST" and path == "system/clear-queue":

                async def clear_node(entry):
                    context = selected_node.set(entry["node"])
                    try:
                        result = await compatible(path, request)
                        data = json.loads(result.body) if isinstance(result, Response) else result
                        ok = not isinstance(result, Response) or result.status_code < 300
                        return {
                            "node": entry["name"],
                            "ok": ok,
                            "count": data.get("count", 0),
                            **(
                                {}
                                if ok
                                else {
                                    "reason": "节点尚未支持此操作，请等待自动更新"
                                    if result.status_code == 404
                                    else data.get("detail", "清空失败")
                                }
                            ),
                        }
                    except HTTPException as error:
                        return {"node": entry["name"], "ok": False, "count": 0, "reason": str(error.detail)}
                    finally:
                        selected_node.reset(context)

                results = await asyncio.gather(*(clear_node(entry) for entry in registry))
                ok = all(r["ok"] for r in results)
                return JSONResponse(
                    {
                        "ok": ok,
                        "count": sum(r["count"] for r in results),
                        "results": results,
                        **({} if ok else {"detail": "部分机器未完成，请查看逐台结果"}),
                    },
                    status_code=200 if ok else 502,
                )
            elif request.method == "GET" and path == "system/settings":

                async def read_configuration(entry):
                    context = selected_node.set(entry["node"])
                    try:
                        data = await compatible(path, request)
                        return {
                            "id": entry["id"],
                            "name": entry["name"],
                            "online": entry["node"] in hub.peers and not data.get("configuration_read_only", False),
                            "collection": data["collection"],
                            **{
                                key: data[key]
                                for key in (
                                    "configuration_source",
                                    "configuration_read_only",
                                    "configuration_updated_at",
                                    "configuration_notice",
                                )
                                if key in data
                            },
                            "data": data,
                        }
                    except HTTPException as error:
                        return {"id": entry["id"], "name": entry["name"], "online": False, "error": str(error.detail)}
                    finally:
                        selected_node.reset(context)

                configs = await asyncio.gather(*(read_configuration(entry) for entry in registry))
                available = [c for c in configs if "data" in c]
                if not available:
                    raise HTTPException(503, "所有节点的采集配置暂不可用")
                metrics = {}
                measured = [c["data"] for c in available if "step_metrics" in c["data"]]
                if measured:
                    from ..task_step_metrics import combine

                    metrics = {
                        "step_metrics": combine(c["step_metrics"] for c in measured),
                        "step_metrics_status": {
                            "complete": len(measured) == len(configs)
                            and all(c.get("step_metrics_status", {}).get("complete") for c in measured),
                            "unavailable_nodes": len(configs) - len(measured),
                            "unavailable_shops": sum(
                                c.get("step_metrics_status", {}).get("unavailable_shops", 0) for c in measured
                            ),
                            "updated_at": min(c.get("step_metrics_status", {}).get("updated_at", 0) for c in measured),
                        },
                    }
                return {
                    **available[0]["data"],
                    **metrics,
                    "scope": "all",
                    "scope_node_id": 0,
                    "node_configs": [{k: v for k, v in c.items() if k != "data"} for c in configs],
                }
            elif request.method == "PUT" and (
                path == "system/concurrency"
                or (
                    path == "system/settings"
                    and set(json.loads(body or b"{}"))
                    in (
                        {"queue_assignment"},
                        {"frequency_assignment"},
                        {"settings_patch"},
                        {"module_assignments"},
                        {"module_assignments", "settings_patch"},
                    )
                )
            ):
                # Only module moves fan out; never copy one node's whole policy over others.
                async def apply_assignment(entry):
                    context = selected_node.set(entry["node"])
                    try:
                        cloned = Request(dict(request.scope))
                        cloned._body = body
                        result = await compatible(path, cloned)
                        if isinstance(result, Response):
                            payload = json.loads(result.body)
                            if result.status_code >= 300:
                                return {"node": entry["name"], "ok": False, "reason": payload.get("detail", "保存失败")}
                        else:
                            payload = result
                        config = payload.get("collection", {})
                        patch = json.loads(body)
                        from .configuration import matches_patch

                        matched = matches_patch(path, patch, config)
                        return {
                            "node": entry["name"],
                            "ok": matched,
                            **({} if matched else {"reason": "节点未确认修改，请更新采集端后重试"}),
                        }
                    except HTTPException as error:
                        return {"node": entry["name"], "ok": False, "reason": str(error.detail)}
                    finally:
                        selected_node.reset(context)

                results = await asyncio.gather(*(apply_assignment(entry) for entry in registry))
                failures = [r for r in results if not r["ok"]]
                if failures:
                    return JSONResponse(
                        {
                            "detail": "部分节点未保存：" + "；".join(r["node"] + "：" + r["reason"] for r in failures),
                            "results": results,
                        },
                        status_code=502,
                    )
                return {"ok": True, "node_count": len(results), "results": results}
            elif request.method == "POST" and path in (
                "shops/operators",
                "shops/windows",
                "shops/tile",
                "operators",
                "attention/sync",
                "todos/sync",
            ):
                value = json.loads(body or b"{}")
                targets = value.get("shop_ids")

                async def execute(entry):
                    ids = [
                        s["id"]
                        for s in records
                        if s["node_id"] == entry["node"] and (targets is None or s["id"] in targets)
                    ]
                    if targets is not None and not ids:
                        return None
                    content = {**value, **({"shop_ids": ids} if targets is not None else {})}
                    cloned = Request(dict(request.scope))
                    cloned._body = json.dumps(content).encode()
                    context = selected_node.set(entry["node"])
                    try:
                        result = await compatible(path, cloned)
                        if isinstance(result, Response):
                            data = json.loads(result.body)
                            if result.status_code >= 300:
                                return {"error": entry["name"] + "：" + str(data.get("detail", "操作失败"))}
                            return data
                        return result
                    except HTTPException as error:
                        return {"error": entry["name"] + "：" + str(error.detail)}
                    finally:
                        selected_node.reset(context)

                outcomes = [r for r in await asyncio.gather(*(execute(n) for n in registry)) if r is not None]
                failures = [r["error"] for r in outcomes if isinstance(r, dict) and r.get("error")]
                if failures:
                    raise HTTPException(502, "部分机器未完成，请核对已完成结果后再操作：" + "；".join(failures))
                if path == "operators":
                    return sorted({name for result in outcomes for name in result})
                return {
                    "ok": True,
                    "count": sum(r.get("count", 0) for r in outcomes),
                    "total": sum(r.get("total", 0) for r in outcomes),
                    "results": [item for r in outcomes for item in r.get("results", [])],
                    "tasks": [item for r in outcomes for item in r.get("tasks", [])],
                }
            elif request.method == "GET" and path in (
                "system/summary",
                "system/progress",
                "system/status",
                "system/health",
                "system/sources",
                "system/performance",
                "logs",
                "operators",
                "browsers",
                "home/progress",
                "home/history",
                "attention/changes",
                "attention/history",
            ):

                async def per_machine(entry):
                    requested = set(request.query_params.get("shop_id", "").split(",")) - {""}
                    if requested and not any(
                        str(s["id"]) in requested and s["node_id"] == entry["node"] for s in records
                    ):
                        return None
                    context = selected_node.set(entry["node"])
                    scoped_request = request
                    if requested:
                        selected = ",".join(
                            str(s["id"]) for s in records if str(s["id"]) in requested and s["node_id"] == entry["node"]
                        )
                        params = [(k, selected if k == "shop_id" else v) for k, v in request.query_params.multi_items()]
                        scoped_request = Request({**request.scope, "query_string": urlencode(params).encode()})
                        scoped_request._body = b""
                    try:
                        return await compatible(path, scoped_request)
                    except HTTPException as error:
                        return {"machine_error": {"name": entry["name"], "reason": error.detail}}
                    finally:
                        selected_node.reset(context)

                results = await asyncio.gather(*(per_machine(entry) for entry in registry))
                available = [
                    r for r in results if r is not None and (not isinstance(r, dict) or "machine_error" not in r)
                ]
                errors = [r["machine_error"] for r in results if isinstance(r, dict) and "machine_error" in r]
                if not available:
                    raise HTTPException(503, "所有采集机器当前均无可用的已存数据")

                def merge(values, key=""):
                    first = values[0]
                    if isinstance(first, list):
                        joined = [item for v in values if isinstance(v, list) for item in v]
                        return list(dict.fromkeys(joined)) if all(isinstance(i, str) for i in joined) else joined
                    if isinstance(first, dict):
                        return {
                            k: merge([v[k] for v in values if isinstance(v, dict) and k in v], k)
                            for k in set().union(*(v.keys() for v in values if isinstance(v, dict)))
                        }
                    if key == "oldest_pending":
                        return min((v for v in values if isinstance(v, (int, float)) and v > 0), default=None)
                    if path == "home/progress" and key in ("last_updated", "last_collected"):
                        return max((v for v in values if v), default=None)
                    if path == "home/progress" and key in (
                        "updated_shops",
                        "total_shops",
                        "updated_modules",
                        "total_modules",
                        "daily_updated",
                        "daily_total",
                        "excluded_shops",
                        "overdue_modules",
                    ):
                        return sum(v for v in values if isinstance(v, (int, float)))
                    if all(type(v) in (float, int) for v in values):
                        return (
                            sum(values)
                            if key
                            in (
                                "total",
                                "running",
                                "queued",
                                "failed",
                                "errors",
                                "ready",
                                "missing",
                                "partial",
                                "complete",
                                "success",
                                "waiting_login",
                                "retrying",
                                "pending",
                                "stale",
                                "count",
                                "shops",
                                "online",
                                "offline",
                                "completed",
                            )
                            else max(values)
                        )
                    return first

                merged = merge(available)
                if path in ("system/progress", "system/status", "system/summary"):
                    # Runtime counters aggregate, editable configuration never does.
                    from .configuration import aggregate_status

                    merged = aggregate_status(merged, available)

                if path == "system/sources":
                    merged["snapshot_complete"] = bool(available) and all(r.get("snapshot_complete") for r in available)
                if isinstance(merged, dict):
                    merged["machine_errors"] = errors
                return merged
            else:
                raise HTTPException(409, "此操作需要先在顶部选择工作机器；经营数据支持跨机器查看")
        if request.method == "GET" and path == "system/settings":
            # Editing must use a live read, never stale-while-revalidate snapshots.
            metrics_query = urlencode(
                [
                    (key, value)
                    for key, value in request.query_params.multi_items()
                    if key in ("include_step_metrics", "metrics_platform")
                ]
            )
            try:
                reply = await hub.request(node, "GET", path, metrics_query)
                if reply["status"] != 200:
                    raise HTTPException(reply["status"], "节点配置读取失败，请确认节点在线后重试")
                data = json.loads(body_of(reply))
            except HTTPException:
                from .configuration import cached_preview

                data = await asyncio.to_thread(cached_preview, database, node)
                if data is None:
                    raise
            entry = next(n for n in database.registry() if n["node"] == node)
            return {
                **data,
                "scope": "node",
                "scope_node_id": entry["id"],
                "node_configs": [
                    {
                        "id": entry["id"],
                        "name": entry["name"],
                        "online": not data.get("configuration_read_only", False),
                        "collection": data["collection"],
                        **{
                            key: data[key]
                            for key in (
                                "configuration_source",
                                "configuration_read_only",
                                "configuration_updated_at",
                                "configuration_notice",
                            )
                            if key in data
                        },
                    }
                ],
            }
        mapping = {s["id"]: s["local_id"] for s in records if s["node_id"] == node}
        translated = []
        for key, value in query:
            if key == "shop_id" and value:
                try:
                    ids = [int(i) for i in value.split(",")]
                except ValueError:
                    raise HTTPException(400, "店铺参数无效") from None
                if any(i not in mapping for i in ids):
                    raise HTTPException(400, "店铺不属于当前机器")
                value = ",".join(str(mapping[i]) for i in ids)
            translated.append((key, value))
        if request.method == "GET" and path == "system/sources":
            from .source_ownership import scope_query, empty_sources

            if not mapping:
                return empty_sources()
            translated = scope_query(translated, mapping)
        query_string = urlencode(translated)
        if body and not is_upload:

            def local_ids(v):
                if isinstance(v, list):
                    return [local_ids(i) for i in v]
                if isinstance(v, dict):
                    result = {}
                    for k, item in v.items():
                        if k == "shop_id" and isinstance(item, int):
                            if item not in mapping:
                                raise HTTPException(400, "店铺不属于当前机器")
                            result[k] = mapping[item]
                        elif k == "shop_ids" and isinstance(item, list):
                            if any(i not in mapping for i in item):
                                raise HTTPException(400, "店铺不属于当前机器")
                            result[k] = [mapping[i] for i in item]
                        else:
                            result[k] = local_ids(item)
                    return result
                return v

            try:
                body = json.dumps(local_ids(json.loads(body))).encode()
            except json.JSONDecodeError:
                raise HTTPException(400, "请求格式错误")
        if request.method == "GET" and path == "logs":
            # Logs are owned by node files. Never persist their API responses centrally.
            result = await hub.request(node, "GET", path, query_string)
            payload = json.loads(body_of(result))
            if result["status"] >= 300:
                raise HTTPException(result["status"], payload.get("detail", "节点日志暂不可用"))
            return remap(payload, node)
        if request.method == "GET" and not path.endswith(("/screenshot", "/pdf", "/download")):

            def present(value, updated=None):
                if path == "home/progress":
                    from .coverage import project, freshness

                    result = project(value, database.shops(), node, request.query_params)
                    entry = next((n for n in database.registry() if n["node"] == node), {})
                    result["progress_freshness"] = [
                        freshness(
                            entry.get("name", node),
                            time.time() if updated is None else updated,
                            node in hub.peers,
                            refresh_errors.get((node, cache_key)),
                            (node, cache_key) in refreshing,
                        )
                    ]
                    return result
                return remap(value, node, path == "shops")

            cache_key = "response:" + hashlib.sha256((path + "?" + query_string).encode()).hexdigest()
            stored = database.list(snapshots, node=node, shop="0", scope=cache_key)
            changed = path in ("home/progress", "system/summary") and database.changes.revision(
                node
            ) > progress_versions.get((node, cache_key), 0)
            if (
                (node, cache_key) not in refreshing
                and (changed or not stored or time.time() - stored[0]["updated"] > 5)
                and node in hub.peers
            ):
                task = asyncio.create_task(read_remote(node, path, query_string, cache_key))
                refreshing[(node, cache_key)] = task
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            if stored:
                task = refreshing.get((node, cache_key))
                if changed and task:
                    try:
                        return present(await asyncio.wait_for(asyncio.shield(task), 3))
                    except TimeoutError, HTTPException:
                        pass
                return present(stored[0]["payload"]["value"], stored[0]["updated"])
            task = refreshing.get((node, cache_key))
            if task:
                return present(await asyncio.shield(task))
            raise HTTPException(503, "中心尚未保存此数据，等待采集节点连接")
        streaming = request.method == "GET" and path.endswith(("/download", "/pdf", "/screenshot"))
        result = await hub.request(
            node,
            request.method,
            path,
            query_string,
            body,
            **({"content_type": content_type} if is_upload else {}),
            **({"stream": True} if streaming else {}),
        )
        if "stream" in result:
            return StreamingResponse(
                result["stream"],
                status_code=result["status"],
                headers={**result.get("headers", {}), "Cache-Control": "no-store"},
            )
        content = body_of(result)
        if "json" in result.get("headers", {}).get("content-type", ""):
            value = json.loads(content)
            if path in ("system/settings", "system/concurrency") and result["status"] >= 300:
                entry = next(n for n in database.registry() if n["node"] == node)
                value["results"] = [{"node": entry["name"], "ok": False, "reason": value.get("detail", "节点保存失败")}]
            if result["status"] < 300:
                if path in ("system/settings", "system/concurrency"):
                    from .configuration import matches_patch

                    entry = next(n for n in database.registry() if n["node"] == node)
                    try:
                        verified = await hub.request(node, "GET", "system/settings")
                        actual = (
                            json.loads(body_of(verified)).get("collection", {}) if verified["status"] == 200 else {}
                        )
                        confirmed = matches_patch(path, json.loads(body), actual)
                    except Exception:
                        confirmed = False
                        actual = {}
                    value["results"] = [
                        {
                            "node": entry["name"],
                            "ok": confirmed,
                            **({} if confirmed else {"reason": "写入后未能核验实际配置，请重新读取后重试"}),
                        }
                    ]
                    if not confirmed:
                        return JSONResponse(
                            {"detail": "节点未确认修改生效", "results": value["results"]}, status_code=502
                        )
                    value["collection"] = actual
                    cache_key = "response:" + hashlib.sha256(b"system/settings?").hexdigest()
                    old = refreshing.pop((node, cache_key), None)
                    if old:
                        old.cancel()
                        await asyncio.gather(old, return_exceptions=True)
                    with database.engine.begin() as db:
                        db.execute(
                            delete(snapshots).where(
                                snapshots.c.node == node, snapshots.c.shop == "0", snapshots.c.scope == cache_key
                            )
                        )
                if path.startswith("shops"):
                    if request.method == "DELETE" and match:
                        database.ingest(
                            node,
                            [
                                {
                                    "shop": str(shop["local_id"]),
                                    "scope": "shop_metadata",
                                    "updated": time.time(),
                                    "payload": {**shop, "_deleted": True},
                                }
                            ],
                        )
                    else:
                        response = await hub.request(node, "GET", "shops")
                        if response["status"] == 200:
                            public = json.loads(body_of(response))
                            fields = (
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
                            batch = [
                                {
                                    "shop": str(s["id"]),
                                    "scope": "shop_metadata",
                                    "updated": time.time(),
                                    "payload": {k: s[k] for k in fields if k in s},
                                }
                                for s in public
                            ]
                            database.ingest(node, batch)
                # Reads after writes must revalidate the relevant operational state.
                with database.engine.begin() as db:
                    db.execute(
                        delete(snapshots).where(
                            snapshots.c.node == node, snapshots.c.shop == "0", snapshots.c.scope.like("response:%")
                        )
                    )
            content = json.dumps(remap(value, node, path == "shops")).encode()
        return Response(
            content, status_code=result["status"], headers={**result.get("headers", {}), "Cache-Control": "no-store"}
        )


JOB_RETENTION = 7 * 86400


def install_cleanup(app, database, interval=3600):
    """Finished jobs are only an audit trail; drop them after a week so center_jobs stays bounded."""
    import contextlib, logging

    async def loop():
        while True:
            try:
                await asyncio.to_thread(database.purge_jobs, JOB_RETENTION)
            except Exception:
                logging.getLogger("ecmp.center").warning("清理历史中心任务失败，稍后重试", exc_info=True)
            await asyncio.sleep(interval)

    previous = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            worker = asyncio.create_task(loop())
            try:
                yield
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    app.router.lifespan_context = lifespan
