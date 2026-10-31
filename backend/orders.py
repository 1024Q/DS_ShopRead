"""Read-only PDD order list. Request values verified against native order tabs."""

from .collection_metrics import measured
import asyncio, json, time
from .business_reader import ReadPage, checked_result, date_text
from .product_images import thumbnail
from .read_errors import PageReadError
from .sync_progress import report
from . import collection_checkpoint as checkpoint

STATUSES = ("", "待付款", "待发货", "待收货", "退款/售后", "即将逾期发货", "退款成功")
ENDPOINT = "/mangkhut/mms/recentOrderList"


class OrdersPage(ReadPage):
    """Use the page's own read-only search handler and authenticated transport."""

    def __init__(self, session):
        super().__init__(session)
        self.order_requests = {}
        self.order_responses = asyncio.Queue()

    def reset_buffers(self):
        super().reset_buffers()
        self.order_requests.clear()
        while not self.order_responses.empty():
            self.order_responses.get_nowait()

    def observe(self, message):
        params = message.get("params", {})
        method = message.get("method")
        if (
            method == "Network.requestWillBeSent"
            and params["request"]["url"].split("?")[0] == "https://mms.pinduoduo.com" + ENDPOINT
        ):
            try:
                body = json.loads(params["request"].get("postData") or "{}")
            except ValueError:
                return
            if body.get("pageSize", 0) > 1 and not body.get("consolidateTypeList"):
                self.order_requests[params["requestId"]] = body
        if method == "Network.loadingFinished" and params["requestId"] in self.order_requests:
            body = self.order_requests.pop(params["requestId"])
            task = asyncio.create_task(self.read_order_response(params["requestId"], body))
            self.body_tasks.add(task)
            task.add_done_callback(self.body_tasks.discard)

    async def read_order_response(self, request_id, body):
        try:
            response = await self.call("Network.getResponseBody", {"requestId": request_id})
            self.order_responses.put_nowait((body, json.loads(response["body"])))
        except Exception:
            self.order_responses.put_nowait((body, {"success": False, "errorMsg": "订单页面响应读取失败"}))

    @measured('query')
    async def search(self, body):
        # All order scopes share one request lane per shop, including retries.
        owner = getattr(self.session, "source", self.session)
        if not hasattr(owner, "order_read_lock"):
            owner.order_read_lock = asyncio.Lock()
        report(self, stage="等待同店订单接口串行名额")
        async with owner.order_read_lock:
            if getattr(owner, "order_cooldown", 0) > time.monotonic():
                raise ValueError("订单接口操作太过频繁，正在冷却，保留断点后自动继续")
            report(self, stage="等待订单接口请求间隔")
            await asyncio.sleep(max(0, getattr(owner, "order_next_request", 0) - time.monotonic()))
            try:
                result = await self._search(body)
                if any(
                    word in str(result.get("errorMsg") or result.get("error_msg") or "") for word in ("频繁", "限流")
                ):
                    owner.order_cooldown = time.monotonic() + 300
                return result
            finally:
                owner.order_next_request = time.monotonic() + 1.5

    async def _search(self, body):
        # The native pagination component exposes the same search callback as
        # the query form. Only call that read operation, never order actions.
        script = """(()=>{const seen=new Set();
          for(const e of document.querySelectorAll('[data-testid=beast-core-pagination],input,button')){
          let f=e[Object.keys(e).find(k=>k.startsWith('__reactFiber$')||k.startsWith('__reactInternalInstance$'))];
          for(let i=0;f&&i<32;i++,f=f.return){if(seen.has(f))continue;seen.add(f);const p=f.memoizedProps||{};
            if(typeof p.search==='function'&&('total' in p||'data' in p)){
              p.search(REQUEST,undefined,{action:'pageChange'});return true;}}}
          return false;})()""".replace("REQUEST", json.dumps(body))
        report(self, stage="等待订单查询控件就绪")
        # One wall-clock limit, not 30 potentially slow CDP calls. Do not wait
        # for the landing page's unsolicited response before issuing our query.
        try:
            async with asyncio.timeout(12):
                while not await self.evaluate(script):
                    await asyncio.sleep(0.4)
        except TimeoutError:
            raise PageReadError("订单查询控件准备超时；本采集页已弃用，下次重建页面重试") from None
        report(self, stage=f"等待订单列表接口响应 · 第 {body.get('pageNumber', 1)} 页")
        try:
            async with asyncio.timeout(20):
                while True:
                    request, response = await self.order_responses.get()
                    if all(
                        request.get(k) == body.get(k)
                        for k in (
                            "orderType",
                            "afterSaleType",
                            "groupStartTime",
                            "groupEndTime",
                            "pageNumber",
                            "pageSize",
                            "delayShippingStatus",
                        )
                    ):
                        return response
        except TimeoutError:
            raise PageReadError("订单列表接口响应超时；保留已提交断点，下次重建采集页重试") from None


def request_body(options, boundary=None):
    status = options.get("status", "")
    if status not in STATUSES:
        raise ValueError("订单状态无效")
    end = min(options["end"], int(boundary or time.time()))
    body = {
        "orderType": {"待付款": 7, "待发货": 1, "待收货": 2, "即将逾期发货": 1}.get(status, 0),
        "afterSaleType": 1 if status in ("待发货", "待收货", "即将逾期发货") else 0,
        "remarkStatus": -1,
        "urgeShippingStatus": -1,
        "groupStartTime": options["start"],
        "groupEndTime": end,
        "pageNumber": 1,
        "pageSize": 50,
        "hideRegionBlackDelayShipping": False,
        "mobile": "",
    }
    sort = {"待付款": 1, "待发货": 10, "待收货": 11, "即将逾期发货": 10}.get(status)
    if sort is not None:
        body["sortType"] = sort
    if status == "即将逾期发货":
        body["delayShippingStatus"] = 1
    # Verified native 售后状态 selector: 4 = 退款成功 (not response status 5).
    if status == "退款成功":
        body["afterSaleType"] = 4
    return body


def pending_shipping(status):
    return "待发货" in str(status or "") and not any(s in str(status) for s in ("已发货", "已取消", "已关闭"))


def normalize(item, scope=""):
    if not item.get("order_sn"):
        raise PageReadError("订单列表缺少订单编号，请更新适配")

    def money(value):
        return round(value / 100, 2) if isinstance(value, (int, float)) else None

    return {
        "id": str(item["order_sn"]),
        "order_number": str(item["order_sn"]),
        "goods_id": str(item.get("goods_id") or ""),
        "name": item.get("goods_name") or "",
        "thumbnail_url": thumbnail(item),
        "spec": item.get("spec") or "",
        "status": item.get("order_status_str") or "未知",
        "attention_status": scope if scope != "即将逾期发货" or pending_shipping(item.get("order_status_str")) else "",
        "quantity": item.get("goods_number"),
        "amount": money(item.get("order_amount")),
        "aftersale_id": str(item.get("after_sales_id") or ""),
        "created_at": date_text(item.get("order_time") or item.get("created_at")),
        "paid_at": date_text(item.get("confirm_time")),
        "deadline": date_text(item.get("promise_shipping_time")),
        "tracking_number": item.get("tracking_number") or "",
    }


def range_error(error):
    return any(text in str(error) for text in ("超过可查询量", "缩小查询范围", "查询超出范围", "订单查询异常"))


async def collect(page, options):
    ctx = checkpoint.current.get()
    if ctx and not ctx.data.get("orders_segmented_v1"):
        # Legacy whole-range cursors cannot resume inside the new date segments.
        # Only discard temporary cursors; the last published snapshot is retained.
        ctx.data["parts"] = {}
        ctx.data["orders_segmented_v1"] = True
        from .async_database import run

        await run(ctx.save)
    boundary = int(ctx.data["started_at"] if ctx else time.time())
    if (
        isinstance(page, OrdersPage)
        and options.get("status") != "退款/售后"
        and not getattr(page, "initial_order_checked", False)
    ):
        if not page.order_responses.empty():
            _, initial = page.order_responses.get_nowait()
            try:
                checked_result(initial)
            except ValueError as error:
                # Landing page defaults can exceed the native query range.
                if not range_error(error) and "下单时间超出查询范围" not in str(error):
                    raise
        page.initial_order_checked = True
    start, end = options["start"], min(options["end"], boundary)
    windows = None
    if ctx and options.get("_order_lane") == "open":
        from .order_storage import open_windows
        from .async_database import run

        # Pin the plan across yielded batches; a shrinking local set must not
        # change the unfinished snapshot's query ranges.
        if "order_open_windows" not in ctx.data:
            shop = next(s for s in ctx.store.list() if s["id"] == ctx.shop)
            ctx.data["order_open_windows"] = await run(open_windows, ctx.store, shop, options)
            await run(ctx.save)
        windows = ctx.data["order_open_windows"]

    async def read_range(first, last):
        key = f"orders-range-v1:{options.get('status', '')}:{first}:{last}"
        splits = ctx.data.setdefault("order_range_splits", {}) if ctx else {}

        async def divide():
            midpoint = (first + last) // 2
            left = await read_range(first, midpoint)
            right = await read_range(midpoint + 1, last)
            return merge_ranges([left, right])

        if key in splits:
            return await divide()
        try:
            return await collect_single(page, {**options, "start": first, "end": last})
        except ValueError as error:
            if not range_error(error):
                raise
            if last - first < 3600:
                raise ValueError(
                    "订单分段查询仍被平台拒绝："
                    + str(error)
                    + "；已缩小至一小时以内，请在官方订单查询核对该日期是否仍可查询，超出历史保留期需调整日期范围"
                ) from error
            splits[key] = True
            if ctx:
                from .async_database import run

                await run(ctx.save)
            return await divide()

    results = []
    # Adjacent, inclusive windows cover the requested range without dropping days.
    # Native status/alert tabs are already selective: do not turn a tiny result
    # into 90 empty day requests. Dense responses still split adaptively.
    spans = (
        windows
        if windows is not None
        else [(start, end)]
        if options.get("status", "") not in ("", "退款/售后")
        else [(s, min(end, s + 86400 - 1)) for s in range(start, end + 1, 86400)]
    )
    for index, (first, last) in enumerate(spans):
        results.append(await read_range(first, last))
        if ctx and index < len(spans) - 1:
            from .async_database import run

            await run(ctx.check_budget)
    result = merge_ranges(results)
    if windows is not None:
        result["scope_note"] = f"未完结精准回查：{len(windows)} 个下单小时段；保留每日状态查漏"
    if ctx and options.get("_order_lane") == "open" and windows is None and result["complete"]:
        from .order_plan import key as view_key

        def mark_scan():
            shop = next(s for s in ctx.store.list() if s["id"] == ctx.shop)
            with ctx.store.write_connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES(?,?)",
                    (f"order-open-scan:{ctx.shop}:{view_key(shop)}:{options['status']}", str(boundary)),
                )

        await run(mark_scan)
    if options.get("status") == "退款成功":
        result["metrics"] = refund_metrics(result["rows"], result["complete"], options["date_from"])
    return result


def merge_ranges(results):
    rows = {r["id"]: r for result in results for r in result["rows"]}
    return {
        "rows": list(rows.values()),
        "source_total": len(rows),
        "source_loaded": len(rows),
        "source_unit": "记录",
        "complete": all(result.get("complete") for result in results),
        "scope_note": "按原查询范围分段读取，订单去重合并",
    }


async def collect_single(page, options):
    status = options.get("status", "")
    if status == "退款/售后":
        # The native order tab embeds the after-sales workbench. Reuse its
        # read-only adapter; do not pretend an order-status code means refund.
        from .operations import paginate

        result = await paginate(
            page,
            "aftersales",
            {
                "pageSize": 10,
                "pageNumber": 1,
                "orderByCreatedAtDesc": True,
                "startCreatedTime": options["start"],
                "endCreatedTime": options["end"],
            },
            "pageNumber",
            "list",
            "total",
            "",
            split_on_limit=True,
        )
        result["rows"] = [{**r, "attention_status": "退款/售后"} for r in result["rows"]]
        return result
    ctx = checkpoint.current.get()
    body = request_body(options, ctx.data["started_at"] if ctx else None)

    async def fetch(number):
        request = {**body, "pageNumber": number}
        data = checked_result(await page.search(request))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("pageItems"), list)
            or data.get("totalItemNum") is None
        ):
            raise PageReadError("订单列表字段变化，请检查页面与登录状态")
        if int(data["totalItemNum"]) > 10000:
            raise ValueError("超过可查询量，自动缩小查询范围")
        if int(data["totalItemNum"]) > 500 and options["end"] - options["start"] >= 3600:
            raise ValueError("缩小查询范围：订单密集，自动拆为更小时间段")
        rows = [normalize(item, status) for item in data["pageItems"]]
        if status == "即将逾期发货" and any(not pending_shipping(row["status"]) for row in rows):
            raise PageReadError("订单筛选状态不一致：即将逾期发货查询返回了非待发货订单，保留上次数据并检查平台筛选")
        if ctx and options.get("_order_lane") and rows:
            from .order_storage import publish
            from .async_database import run

            shop = next(s for s in ctx.store.list() if s["id"] == ctx.shop)
            await run(
                publish,
                ctx.store,
                shop,
                options,
                {"rows": rows, "_sync": {"snapshot_started_at": ctx.data["started_at"]}},
                True,
            )
        return {
            "rows": rows,
            "ids": [r["id"] for r in rows],
            "total": int(data["totalItemNum"]),
            "has_more": data.get("hasMore"),
        }

    if ctx:
        from datetime import datetime
        from .business_reader import BEIJING

        label = (
            "读取订单 · "
            + datetime.fromtimestamp(options["start"], BEIJING).strftime("%m-%d %H:%M")
            + " 至 "
            + datetime.fromtimestamp(options["end"], BEIJING).strftime("%m-%d %H:%M")
        )
        result = await checkpoint.paginate(
            json.dumps(["orders", body], sort_keys=True), fetch, lambda n, total: report(page, n, total, label)
        )
        return await with_refunds(page, result, options, body["groupEndTime"]) if status == "退款成功" else result
    # Bounded diagnostic reads outside the scheduled checkpoint context.
    rows = {}
    total = 0
    for number in range(1, 101):
        batch = await fetch(number)
        total = batch["total"]
        before = len(rows)
        rows.update({r["id"]: r for r in batch["rows"]})
        if len(rows) >= total:
            result = {"rows": list(rows.values()), "source_total": total, "complete": True}
            return await with_refunds(page, result, options, body["groupEndTime"]) if status == "退款成功" else result
        if len(rows) == before:
            raise PageReadError("订单分页未推进，保留原数据等待重试")
        await asyncio.sleep(0.2)
    return {"rows": list(rows.values()), "source_total": total, "complete": False}


def refund_metrics(rows, complete, date):
    from decimal import Decimal

    covered = all(isinstance(r.get("refund_amount"), (int, float)) for r in rows)
    return {
        "refund_orders": len(rows) if complete else None,
        "refund_amount": float(sum((Decimal(str(r["refund_amount"])) for r in rows), Decimal(0)))
        if complete and covered
        else None,
        "refund_date": date,
        "refund_basis": "order_created_success",
        "refund_complete": bool(complete and covered),
    }


async def with_refunds(page, result, options, boundary):
    from .operations import paginate

    fresh_ids = set()
    if result["rows"]:
        ctx = checkpoint.current.get()
        from .async_database import run
        from .refund_index import lookup

        by_id = await run(lookup, ctx.store, ctx.shop, result["rows"]) if ctx else {}
        missing = [row for row in result["rows"] if row["aftersale_id"] not in by_id]
        if missing:
            fresh_ids = {row["aftersale_id"] for row in missing}
            await page.open("aftersales")
            cases = await paginate(
                page,
                "aftersales",
                {
                    "pageSize": 10,
                    "pageNumber": 1,
                    "orderByCreatedAtDesc": True,
                    "startCreatedTime": options["start"],
                    "endCreatedTime": boundary,
                },
                "pageNumber",
                "list",
                "total",
                "",
                required_ids=fresh_ids,
            )
            by_id.update({str(r["id"]): r for r in cases["rows"]})
        for row in result["rows"]:
            case = by_id.get(row["aftersale_id"])
            if not case or case.get("order_number") != row["order_number"] or case.get("amount") is None:
                # Completed parts otherwise stay frozen across retries, so a
                # newly completed refund can never enter the saved join.
                ctx = checkpoint.current.get()
                if ctx:
                    from .async_database import run

                    await run(ctx.clear)
                raise PageReadError("退款成功订单的实际退款金额尚未关联完整，保留上次成功统计后重试")
            row["refund_amount"] = case["amount"]
    ctx = checkpoint.current.get()
    if ctx and result.get("complete") and fresh_ids:

        def persist():
            from .refund_index import save

            with ctx.store.connect() as db:
                save(
                    db,
                    ctx.shop,
                    [{**r, "status": "退款成功"} for r in result["rows"] if r["aftersale_id"] in fresh_ids],
                )

        from .async_database import run

        await run(persist)
    result["metrics"] = refund_metrics(result["rows"], result["complete"], options["date_from"])
    result["scope_note"] = "按下单日期筛选退款成功订单，金额关联对应售后单的实际退款金额（包含部分退款）"
    return result
