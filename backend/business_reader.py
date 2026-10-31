"""Read authenticated merchant pages; never submit merchant business actions."""

import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
import httpx
import websockets
from .read_errors import PageReadError
from .product_images import thumbnail
from .sync_progress import report
from .collection_metrics import measured

BEIJING = timezone(timedelta(hours=8))
PAGES = {
    "overview": "https://mms.pinduoduo.com/home/",
    "products": "https://mms.pinduoduo.com/goods/goods_list",
    "tickets": "https://mms.pinduoduo.com/aftersales/work_order/list",
    "reviews": "https://mms.pinduoduo.com/goods/evaluation/index",
}
PAGES.update(
    {
        key: "https://mms.pinduoduo.com" + path
        for key, path in {
            "packages": "/express/package",
            "invoices": "/invoice/center",
            "aftersales": "/aftersales/aftersale_list",
            "evidence": "/aftersales/merchant_appeal",
            "intercepts": "/orders/interceptExpress",
            "navigator": "/sycm/goods_quality/pilot_goods",
            "quality": "/pg/mall_quality",
            "brands": "/kit/brand-price-change?tool_full_channel=10323_97807",
        }.items()
    }
)
PAGES.update(
    {
        kind: "https://mms.pinduoduo.com/cashier/finance/payment-bills"
        for kind in ("reconciliation_day", "reconciliation_month")
    }
)
PAGES["entity"] = "https://mms.pinduoduo.com/mallcenter/mallinfo"
PAGES["transactions"] = "https://mms.pinduoduo.com/sycm/stores_data"
PAGES["goodsdata"] = "https://mms.pinduoduo.com/sycm/goods_effect"
PAGES["activities"] = "https://mms.pinduoduo.com/act/register_record"
PAGES["orders"] = "https://mms.pinduoduo.com/orders/list"
PAGES["ad_logs"] = "https://yingxiao.pinduoduo.com/tools/operate/log"
PAGES["ad_products"] = "https://yingxiao.pinduoduo.com/goods/promotion/list"
PAGES["ad_reports"] = "https://yingxiao.pinduoduo.com/goods/report/promotion/overView"
PRODUCT_API = "/vodka/v2/mms/query/display/mall/goodsList"
READ_APIS = {"tickets": "/strickland/sop/mms/todoList", "reviews": "/saturn/reviews/list"}
TICKET_STATUS = {0: "待处理", 1: "处理中", 2: "已关闭", 3: "已完结", 4: "2小时内将逾期", 5: "违规已处理"}


async def foreground(page):
    """Keep readers active without activating Chrome, including in debug mode.

    Showing a debug window is separate from keyboard focus. Only an explicit
    user window action may activate it; borrowing/rebinding a reader may not.
    """
    await page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
    try:
        await page.call("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass  # optional; older Chrome versions may not support it


async def close_reader_target(socket, target_id):
    """Wait for Chrome's close acknowledgement on a dedicated owner socket."""
    async with asyncio.timeout(3):
        await socket.send(json.dumps({"id": 9999, "method": "Target.closeTarget", "params": {"targetId": target_id}}))
        while True:
            message = json.loads(await socket.recv())
            if message.get("id") != 9999:
                # Yield even for buffered event bursts (and synchronous test
                # transports), allowing the bounded close timeout to fire.
                await asyncio.sleep(0)
                continue
            if "error" in message or message.get("result", {}).get("success") is not True:
                raise PageReadError("采集页关闭尚未确认")
            return


def date_text(value):
    if not value:
        return ""
    return datetime.fromtimestamp(float(value) / (1000 if float(value) > 1e12 else 1), BEIJING).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def parse_metrics(text):
    names = {
        "sales": "成交金额",
        "orders": "成交订单数",
        "ad_spend": "推广花费",
        "visitors": "商品访客数",
        "views": "商品浏览量",
        "reviews_today": "商品评价数",
        "pending_payment": "待付款",
        "pending_shipping": "待发货",
        "pending_aftersales": "退款/售后",
        "pending_tickets": "待处理工单",
        "shipping_due": "即将逾期发货",
        "aftersales_due": "售后过期预警",
        "inquiry_conversion": "询单转化率",
    }
    values = {}
    for key, label in names.items():
        match = re.search(re.escape(label) + r"\s*\n\s*([\d,]+(?:\.\d+)?)", text)
        values[key] = float(match[1].replace(",", "")) if match else None
    source_time = re.search(r"实时数据更新时间\s*[:：]?\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
    if values["sales"] is None or values["orders"] is None:
        raise ValueError("未读取到经营数据，可能需要重新登录或页面尚未加载")
    values["source_time"] = source_time[1] if source_time else None
    return values


def normalize(kind, item):
    if kind == "products":
        prices = item.get("sku_group_price") or []
        return {
            "id": str(item["id"]),
            "goods_id": str(item["id"]),
            "name": item.get("goods_name", ""),
            "code": item.get("out_goods_sn") or "",
            "thumbnail_url": thumbnail(item),
            "status": "已售罄"
            if item.get("is_onsale") and item.get("quantity") == 0
            else ("在售中" if item.get("is_onsale") else "已下架"),
            "price_min": min(prices) / 100 if prices else None,
            "price_max": max(prices) / 100 if prices else None,
            "stock": item.get("quantity"),
            "sales": item.get("sold_quantity"),
            "sales_30d": item.get("sold_quantity_for_thirty_days"),
            "category": item.get("cat_name", ""),
            "created_at": date_text(item.get("created_at")),
        }
    if kind == "tickets":
        return {
            "id": str(item["instanceId"]),
            "order_number": item.get("orderSn", ""),
            "name": item.get("goodsName", ""),
            "goods_id": str(item.get("goodsId") or ""),
            "thumbnail_url": thumbnail(item),
            "problem": item.get("problemTitle", ""),
            "status": TICKET_STATUS.get(item.get("status"), "未知"),
            "progress": item.get("externalDisplayName", ""),
            "created_at": date_text(item.get("createdAt")),
            "deadline": date_text(item.get("deadline")),
        }
    return {
        "id": str(item["reviewId"]),
        "order_number": item.get("orderSn", ""),
        "goods_id": str(item.get("goodsId") or ""),
        "thumbnail_url": thumbnail(item),
        "name": item.get("goodsName", ""),
        "stars": item.get("descScore"),
        "content": item.get("comment") or "",
        "reply": item.get("reply") or "",
        "replied": bool(item.get("reply") or item.get("replyTime") or item.get("replyList")),
        "created_at": date_text(item.get("createTime")),
        "reply_time": date_text(item.get("replyTime")),
        "has_images": bool(item.get("pictures")),
        "has_video": bool(item.get("video")),
    }


# Collection tabs never need pictures or video; skipping them saves bandwidth and
# CDP events. Fonts stay allowed: trade digits are recognized from rendered glyphs.
# Set ECMP_READ_BLOCK_MEDIA=0 to disable. User-visible login windows are unaffected.
MEDIA_PATTERNS = [
    pattern
    for ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "mp4", "webm", "mp3")
    for pattern in (f"*.{ext}", f"*.{ext}?*")
]


# Third-party analytics beacons never carry merchant data. Platform fonts stay
# allowed (transaction amounts depend on them). Extra patterns can be added with
# ECMP_READ_BLOCK_EXTRA="*://host/path*,..." after checking a page still reads.
TRACKING_PATTERNS = [
    "*://hm.baidu.com/*",
    "*://*.google-analytics.com/*",
    "*://*.googletagmanager.com/*",
    "*://*.cnzz.com/*",
    "*://*.growingio.com/*",
    "*://*.sensorsdata.cn/*",
]


def blocked_patterns():
    import os

    extra = [p.strip() for p in os.environ.get("ECMP_READ_BLOCK_EXTRA", "").split(",") if p.strip()]
    return MEDIA_PATTERNS + TRACKING_PATTERNS + extra


async def block_media(page):
    import os

    if os.environ.get("ECMP_READ_BLOCK_MEDIA", "1").strip() in ("0", "false", "no"):
        return
    try:
        await page.call("Network.setBlockedURLs", {"urls": blocked_patterns()})
    except Exception:
        # Older/newer Chrome without this command: reading still works unblocked.
        import logging

        logging.getLogger(__name__).debug("采集页未能屏蔽图片资源，继续正常读取", exc_info=True)


# Modules whose reads are verified same-origin fetches (no DOM, no native
# controls). Override with ECMP_INPAGE_KINDS="kind,kind" ("" disables).
IN_PAGE_KINDS = ("packages", "invoices", "aftersales", "evidence", "intercepts", "tickets", "reviews")


def in_page_kinds():
    import os

    value = os.environ.get("ECMP_INPAGE_KINDS")
    if value is None:
        return IN_PAGE_KINDS
    return tuple(k.strip() for k in value.split(",") if k.strip())


async def soft_retry(page, kind, read):
    """Run a read; if the tab skipped its page load and the read fails, load once and retry."""
    try:
        return await read()
    except Exception as error:
        if not getattr(page, "soft_opened", False):
            raise
        from .collection_errors import classify

        if classify(error)[0] in ("rate_limit", "shop_cooldown", "login", "permission", "database_busy", "database_error"):
            raise
        report(page, stage="页面内读取未成功，重新加载模块页面后重试一次")
        page.soft_opened = False
        await page.open(kind)
        return await read()


class ReadPage:
    def __init__(self, session):
        self.session = session
        self.pending = {}
        self.page_changed = asyncio.Event()
        self.sequence = 0
        self.product_requests = {}
        self.product_bodies = {}
        self.product_epoch = 0
        self.products = asyncio.Queue()
        self.activity_requests = {}
        self.activities = asyncio.Queue()
        self.body_tasks = set()
        self.finance_headers = {}
        self.finance_ready = asyncio.Event()
        self.trade_requests = {}
        self.trade_responses = {}
        self.trade_changed = asyncio.Event()
        self.response_generation = 0
        self.trade_epochs = {}
        self.owner = None
        self.target = None
        self.ws = None
        self.reader = None

    @measured('page_load')
    async def __aenter__(self):
        from .request_reuse import owner

        manual = getattr(owner(self.session), "manual_visibility", None) is True
        self.hidden_target = not manual and not getattr(self, "requires_native_surface", False)
        self.target_record = None
        try:
            from .read_page_pool import release_idle

            if not getattr(self, "_pooled_reader", False):
                # Exclusive direct readers (exports, tax, logs) only release
                # idle tabs. They must never fence tabs other tasks are using.
                self._pool_epoch = await release_idle(self.session)
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                browser = (
                    (await client.get(f"http://127.0.0.1:{self.session.port}/json/version")).raise_for_status().json()
                )
                # Keep the browser-owner socket alive for hidden targets. Only
                # explicitly displayed tabs survive disconnect and need a ledger.
                self.owner = await websockets.connect(browser["webSocketDebuggerUrl"], close_timeout=2)
                params = {"url": "about:blank", "background": True, "hidden": self.hidden_target, "focus": False}
                if not self.hidden_target and not manual:
                    from .native_window import background_target_bounds

                    params.update(newWindow=True, **background_target_bounds())
                await self.owner.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Target.createTarget",
                            "params": params,
                        }
                    )
                )
                while True:
                    created = json.loads(await asyncio.wait_for(self.owner.recv(), 12))
                    if created.get("id") == 1:
                        break
                if "error" in created:
                    raise PageReadError("浏览器无法创建采集页，请检查浏览器后重试")
                self.target = created["result"]["targetId"]
                self.session.data_targets.add(self.target)
                from .reader_targets import register

                if not self.hidden_target:
                    self.target_record = register(self.session, browser["webSocketDebuggerUrl"], self.target)
                # Chrome may allocate another native window for a work target.
                # Protect it before any platform page or input can run.
                guard = getattr(owner(self.session), "guard_windows", None)
                if callable(guard):
                    await guard()
                targets = (
                    (await client.get(f"http://127.0.0.1:{self.session.port}/json/list")).raise_for_status().json()
                )
                result = next(target for target in targets if target["id"] == self.target)
            await self.connect_target(result["webSocketDebuggerUrl"])
            return self
        except BaseException:
            await self.__aexit__()
            raise

    async def connect_target(self, address):
        self.target_address = address
        self.ws = await websockets.connect(address, max_size=30_000_000, close_timeout=2)
        self.reader = asyncio.create_task(self.receive())
        # Initialize execution contexts before navigating the new target.
        # Without this some Chrome sessions leave evaluate waiting on the old context.
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False},
        )
        await self.call("Network.enable", {"maxTotalBufferSize": 30_000_000, "maxResourceBufferSize": 8_000_000})
        await block_media(self)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        await foreground(self)

    async def rebind(self, session, factory):
        """Keep the owned tab, but replace the complete adapter and event stream."""
        if self.pending or self.body_tasks or not self.target or not self.ws:
            raise PageReadError("工作标签仍有未完成读取，暂不能切换模块")
        replacement = factory(session)
        from .reader_document import DocumentGuard

        replacement.document_guard = DocumentGuard()
        replacement.response_generation = self.response_generation + 1
        # A fresh target websocket drops buffered old CDP events/command IDs.
        # Old document requests still arriving on the new socket are rejected
        # until Page.frameNavigated establishes the next document's loader.
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        await self.ws.close()
        for name in ("owner", "target", "target_record", "hidden_target", "_pool_epoch"):
            setattr(replacement, name, getattr(self, name, None))
        self.owner = self.target = self.ws = self.reader = None
        try:
            await replacement.connect_target(self.target_address)
            return replacement
        except BaseException:
            await replacement.__aexit__()
            raise

    def accept_message(self, message):
        guard = getattr(self, "document_guard", None)
        return guard is None or guard.accepts(message)

    async def __aexit__(self, *args):
        tasks = list(self.body_tasks) + ([self.reader] if self.reader else [])
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            try:
                if self.ws:
                    await self.ws.close()
            finally:
                try:
                    if self.owner:
                        try:
                            if self.target and not getattr(self, "hidden_target", True):
                                from .reader_targets import forget

                                try:
                                    await close_reader_target(self.owner, self.target)
                                    forget(self.session, getattr(self, "target_record", None))
                                except websockets.ConnectionClosed, OSError, TimeoutError, ValueError:
                                    # Keep its ownership record for the next
                                    # reconnect. Never hide the original error.
                                    import logging

                                    logging.getLogger(__name__).debug(
                                        "采集页关闭暂未确认，保留归属等待回收", exc_info=True
                                    )
                        finally:
                            await self.owner.close()
                finally:
                    if self.target:
                        self.session.data_targets.discard(self.target)

    async def receive(self):
        # Lazy once per connection: these adapters import this reader.
        from .transactions import ENDPOINTS
        from .goods_data import ENDPOINTS as GOODS_ENDPOINTS

        trade_endpoints = ENDPOINTS | GOODS_ENDPOINTS
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if not self.accept_message(message):
                    continue
                if message.get("id") in self.pending:
                    future = self.pending.pop(message["id"])
                    if not future.done():
                        future.set_result(message)
                params = message.get("params", {})
                self.observe(message)
                if message.get("method") in ("Page.loadEventFired", "Runtime.executionContextCreated"):
                    self.page_changed.set()
                if message.get("method") == "Network.requestWillBeSent":
                    request = params["request"]
                    path = request["url"].split("?")[0].rsplit("/", 1)[-1]
                    if path in trade_endpoints and request["url"].startswith("https://mms.pinduoduo.com/sydney/api/"):
                        self.trade_requests[params["requestId"]] = (
                            path,
                            self.response_generation,
                            self.trade_epochs.get(path, 0),
                        )
                    if request["url"].split("?")[0] == "https://mms.pinduoduo.com" + PRODUCT_API:
                        self.product_requests[params["requestId"]] = (self.response_generation, self.product_epoch)
                        try:
                            self.product_bodies[params["requestId"]] = json.loads(request.get("postData") or "{}")
                        except ValueError:
                            pass
                    if request["url"].split("?")[0] == "https://mms.pinduoduo.com/lakemms/activityGoods/list":
                        try:
                            self.activity_requests[params["requestId"]] = json.loads(
                                request.get("postData") or "{}"
                            ).get("page_number")
                        except ValueError, AttributeError:
                            self.activity_requests[params["requestId"]] = None
                    if request["url"].split("?")[0] in (
                        "https://cashier.pinduoduo.com/templar/api/bill/queryDailyBills",
                        "https://cashier.pinduoduo.com/templar/api/bill/queryMallBalanceMonthlySummary",
                    ):
                        headers = {
                            k: v
                            for k, v in request.get("headers", {}).items()
                            if k.lower() in ("anti-content", "x-fin-sherlock-passid", "etag")
                        }
                        if headers:
                            self.finance_headers = headers
                            self.finance_ready.set()
                if (
                    message.get("method") == "Network.responseReceived"
                    and params["requestId"] in self.product_requests
                    and params["response"].get("status", 200) >= 400
                ):
                    generation, epoch = self.product_requests.pop(params["requestId"])
                    self.product_bodies.pop(params["requestId"], None)
                    if (generation, epoch) == (self.response_generation, self.product_epoch):
                        self.products.put_nowait(
                            {
                                "success": False,
                                "errorMsg": f"商品接口 HTTP {int(params['response']['status'])}，请检查登录和数据权限",
                            }
                        )
                if message.get("method") == "Network.loadingFailed" and params["requestId"] in self.product_requests:
                    generation, epoch = self.product_requests.pop(params["requestId"])
                    self.product_bodies.pop(params["requestId"], None)
                    if (generation, epoch) == (self.response_generation, self.product_epoch):
                        self.products.put_nowait({"success": False, "errorMsg": "商品接口连接失败，保留断点后重试"})
                if message.get("method") == "Network.loadingFinished" and params["requestId"] in self.activity_requests:
                    number = self.activity_requests.pop(params["requestId"])
                    task = asyncio.create_task(self.read_activities(params["requestId"], number))
                    self.body_tasks.add(task)
                    task.add_done_callback(self.body_tasks.discard)
                if (
                    message.get("method") == "Network.responseReceived"
                    and params["requestId"] in self.trade_requests
                    and params["response"].get("status", 200) >= 400
                ):
                    path, generation, epoch = self.trade_requests.pop(params["requestId"])
                    self.trade_result(
                        path,
                        generation,
                        epoch,
                        {
                            "success": False,
                            "errorMsg": f"数据接口 HTTP {int(params['response']['status'])}，请检查登录和数据权限",
                        },
                    )
                if message.get("method") == "Network.loadingFailed" and params["requestId"] in self.trade_requests:
                    path, generation, epoch = self.trade_requests.pop(params["requestId"])
                    self.trade_result(
                        path, generation, epoch, {"success": False, "errorMsg": "数据接口连接失败，保留断点后重试"}
                    )
                if message.get("method") == "Network.loadingFinished" and params["requestId"] in self.trade_requests:
                    path, generation, epoch = self.trade_requests.pop(params["requestId"])
                    task = asyncio.create_task(self.read_trade(params["requestId"], path, generation, epoch))
                    self.body_tasks.add(task)
                    task.add_done_callback(self.body_tasks.discard)
                if message.get("method") == "Network.loadingFinished" and params["requestId"] in self.product_requests:
                    generation, epoch = self.product_requests.pop(params["requestId"])
                    body = self.product_bodies.pop(params["requestId"], {})
                    task = asyncio.create_task(self.read_products(params["requestId"], generation, body, epoch))
                    self.body_tasks.add(task)
                    task.add_done_callback(self.body_tasks.discard)
        except Exception:
            from .recovery_log import suppressed

            suppressed("采集页连接中断", warning=False)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(PageReadError("浏览器连接已关闭"))
            self.pending.clear()
            self.page_changed.set()

    async def read_products(self, request_id, generation=None, body=None, epoch=None):
        generation = self.response_generation if generation is None else generation
        epoch = self.product_epoch if epoch is None else epoch
        try:
            response = await self.call("Network.getResponseBody", {"requestId": request_id})
            data = json.loads(response["body"])
            data["_request"] = body or {}
        except Exception:
            data = {"success": False, "errorMsg": "商品页面响应读取失败"}
        if (generation, epoch) == (self.response_generation, self.product_epoch):
            self.products.put_nowait(data)

    async def read_activities(self, request_id, number):
        try:
            response = await self.call("Network.getResponseBody", {"requestId": request_id})
            self.activities.put_nowait({"page": number, "data": json.loads(response["body"])})
        except Exception:
            self.activities.put_nowait(
                {"page": number, "data": {"success": False, "errorMsg": "活动报名页面响应读取失败"}}
            )

    def reset_trade(self, names):
        for name in names:
            self.trade_responses.pop(name, None)
            self.trade_epochs[name] = self.trade_epochs.get(name, 0) + 1
        self.trade_changed.clear()

    def reset_buffers(self):
        """A reused tab must never satisfy a new read with a prior response."""
        self.response_generation += 1
        self.trade_requests.clear()
        self.trade_responses.clear()
        self.trade_changed.clear()
        self.product_requests.clear()
        self.product_bodies.clear()
        self.activity_requests.clear()
        for queue in (self.products, self.activities):
            while not queue.empty():
                queue.get_nowait()

    def reset_products(self):
        """Exclude the initial tab's in-flight responses after changing its filter."""
        self.product_epoch += 1
        self.product_requests.clear()
        self.product_bodies.clear()
        while not self.products.empty():
            self.products.get_nowait()

    def reuse_ready_expression(self, kind):
        from urllib.parse import urlsplit

        expected = urlsplit(PAGES[kind])
        route = (
            "location.hostname==="
            + json.dumps(expected.hostname)
            + "&&location.pathname==="
            + json.dumps(expected.path)
        )
        marker = "!!document.body" if kind == "products" else "!!document.querySelector('.mms-header__user-info')"
        return "!location.href.includes('/login')&&document.readyState!=='loading'&&" + route + "&&" + marker

    async def logged_in_merchant_page(self):
        try:
            return bool(
                await asyncio.wait_for(
                    self.evaluate(
                        "location.origin==='https://mms.pinduoduo.com'&&!location.href.includes('/login')"
                        "&&document.readyState!=='loading'&&!!document.querySelector('.mms-header__user-info')"
                    ),
                    3,
                )
            )
        except Exception:
            return False

    def trade_result(self, path, generation, epoch, data):
        if generation != self.response_generation or epoch != self.trade_epochs.get(path, 0):
            return
        self.trade_responses[path] = data
        self.trade_changed.set()

    async def read_trade(self, request_id, path, generation=None, epoch=None):
        generation = self.response_generation if generation is None else generation
        epoch = self.trade_epochs.get(path, 0) if epoch is None else epoch
        try:
            response = await self.call("Network.getResponseBody", {"requestId": request_id})
            data = json.loads(response["body"])
        except Exception:
            data = {"success": False, "errorMsg": "交易数据响应读取失败"}
        self.trade_result(path, generation, epoch, data)

    async def call(self, method, params=None):
        if method in ("Page.navigate", "Page.reload") and getattr(self, "document_guard", None):
            self.document_guard.navigate((params or {}).get("url"))
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        try:
            await self.ws.send(json.dumps({"id": number, "method": method, "params": params or {}}))
            message = await asyncio.wait_for(future, 20)
            if "error" in message:
                raise PageReadError("浏览器页面读取失败")
            return message.get("result", {})
        except TimeoutError:
            raise PageReadError(f"隐藏采集页响应超时（{method}），将重建该采集页后重试") from None
        finally:
            self.pending.pop(number, None)

    def observe(self, message):
        """Optional read-only adapter response observer."""
        pass

    async def evaluate(self, expression):
        result = await self.call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            description = str(detail.get("exception", {}).get("description", "")) + " " + str(detail.get("text", ""))
            if any(word in description for word in ("TimeoutError", "AbortError", "signal timed out")):
                raise PageReadError("平台接口等待超时，请求已终止；保留断点按计划重试")
            if "SyntaxError" in description:
                raise ValueError("采集脚本语法错误，请更新读取适配；不是店铺登录失效")
            raise PageReadError("页面读取失败，请检查登录状态后重试")
        return result.get("result", {}).get("value")

    async def prepare_reconciliation(self, kind):
        # The cashier creates its own authenticated request headers. Keep them
        # only in this short-lived page and never persist them with bill data.
        label = "日汇总" if kind == "reconciliation_day" else "月汇总"
        self.finance_headers = {}
        self.finance_ready.clear()
        clicked = await self.evaluate(
            "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.textContent.trim()==="
            + json.dumps(label)
            + "&&e.offsetWidth>0);if(e)e.click();return !!e;})()"
        )
        if not clicked:
            raise ValueError("未找到对账汇总入口，请检查资金账户权限")
        try:
            await asyncio.wait_for(self.finance_ready.wait(), 6)
        except TimeoutError:
            pass
        if self.finance_headers:
            return self.finance_headers
        raise PageReadError("资金页面鉴权尚未就绪，请稍后重试")

    @measured('page_load')
    async def open(self, kind):
        reload_origin = None
        self.soft_opened = False
        if getattr(self, "soft_switch", False):
            self.soft_switch = False
            if kind in in_page_kinds() and await self.logged_in_merchant_page():
                # Verified read-only endpoints are plain same-origin fetches:
                # switching modules inside the logged-in work tab needs no full
                # page load. A failure retries once after a normal load.
                self.soft_opened = True
                report(self, stage="复用已登录工作页，页面内读取接口（不重新加载）")
                return
        if getattr(self, "reused_kind", None) == kind:
            self.reused_kind = None
            report(self, stage="复用采集页面，准备查询")
            if kind not in ("overview", "transactions", "goodsdata", "products"):
                return
            # These landing pages fetch several independent widgets at mount.
            # Reload the same tab/connection instead of treating old widgets as
            # a fresh sample or inventing private React handlers.
            reload_origin = await self.evaluate("performance.timeOrigin")
            await self.call("Page.reload", {"ignoreCache": False})
        else:
            report(self, stage="加载采集页面")
            if getattr(self, "document_guard", None):
                reload_origin = await self.evaluate("performance.timeOrigin")
            await self.call("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Shanghai"})
            await self.call("Page.navigate", {"url": PAGES[kind]})
        for _ in range(35):
            if kind == "products":
                from .products import ensure_budget

                await ensure_budget(self)
            try:
                await asyncio.wait_for(self.page_changed.wait(), 0.3)
            except TimeoutError:
                pass
            self.page_changed.clear()
            if kind.startswith("reconciliation_"):
                ready = await self.evaluate(
                    "({ready:location.hostname==='cashier.pinduoduo.com'&&document.body.innerText.includes('日汇总')&&document.body.innerText.includes('月汇总'),login:location.href.includes('/login')})"
                )
            else:
                fresh = "true" if reload_origin is None else "performance.timeOrigin!==" + json.dumps(reload_origin)
                marker = self.reuse_ready_expression(kind)
                expression = (
                    "({ready:"
                    + fresh
                    + "&&document.readyState!=='loading'&&"
                    + marker
                    + ",login:location.href.includes('/login'),origin:performance.timeOrigin})"
                )
                if kind == "products":
                    from .products import step

                    ready = await step(self, lambda: self.evaluate(expression), "等待商品页面框架就绪")
                else:
                    ready = await self.evaluate(expression)
            if ready.get("login"):
                raise PageReadError("登录已失效，请显示该店铺浏览器重新登录")
            if ready.get("ready") and (not getattr(self, "document_guard", None) or self.document_guard.loader):
                if kind == "overview":
                    self.overview_origin = ready.get("origin")
                if kind == "products":
                    report(self, stage="商品页面框架就绪，等待商品列表响应")
                return
        raise PageReadError(
            "商品页面框架加载超时（尚未开始读取商品列表）" if kind == "products" else "商家页面加载超时"
        )


def checked_result(data):
    if data.get("success") is not True:
        message = data.get("errorMsg") or data.get("error_msg") or "平台未返回有效数据"
        raise (PageReadError if re.search(r"未登录|登录.*(失效|过期)|请.*登录", str(message)) else ValueError)(
            str(message)[:160]
        )
    return data.get("result")


async def fetch_list(session, kind, options):
    from .request_reuse import complete

    return await complete(session, kind, options, lambda: _fetch_list(session, kind, options))


async def post_json(page, api, body):
    expression = (
        "(async()=>{const r=await fetch("
        + json.dumps(api)
        + ',{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify('
        + json.dumps(body)
        + '),signal:AbortSignal.timeout(12000)});if(!r.ok)throw Error("读取失败");return await r.text();})()'
    )
    return json.loads(await page.evaluate(expression))


async def _fetch_list(session, kind, options):
    """Read-only endpoints observed in the merchant UI, with its supported page size."""
    limit = 2000
    start, end = options["start"], options["end"]
    if kind == "tickets" and options.get("_attention"):
        result = await fetch_list(session, kind, {**options, "_attention": False, "status": "2小时内将逾期"})
        return result

    def normalized(item):
        row = normalize(kind, item)
        if kind == "tickets" and options.get("status") == "2小时内将逾期":
            row["attention_status"] = "2小时内将逾期"
        return row

    if kind == "tickets":
        status = [next(k for k, v in TICKET_STATUS.items() if v == options["status"])] if options["status"] else [None]
        body = {
            "serviceStatus": status,
            "problemType": [None],
            "createStartTime": start,
            "createEndTime": end,
            "pageNum": 1,
            "pageSize": 50,
        }
        page_key, list_key, total_key = "pageNum", "dataList", "total"
    else:
        body = {"startTime": start, "endTime": end, "pageNo": 1, "pageSize": 10, "orderSn": ""}
        if options["stars"]:
            body["descScore"] = [str(value) for value in options["stars"]]
        if options["reply"]:
            body["replyStatus"] = "2" if options["reply"] == "unreplied" else "1"
        page_key, list_key, total_key = "pageNo", "data", "totalRows"
    from . import collection_checkpoint as checkpoint

    if checkpoint.current.get():
        boundary = int(checkpoint.current.get().data["started_at"])
        for field in ("createEndTime", "endTime"):
            if field in body:
                body[field] = min(body[field], boundary)

        async def read_page(number):
            request = {**body, page_key: number}
            from .request_reuse import read as reuse

            async def fetch():
                data = checked_result(await post_json(session, READ_APIS[kind], request))
                if (
                    not isinstance(data, dict)
                    or not isinstance(data.get(list_key), list)
                    or data.get(total_key) is None
                ):
                    raise ValueError("列表字段发生变化，请更新读取适配")
                return data

            data = await reuse(session, READ_APIS[kind], request, fetch)
            if not isinstance(data, dict) or not isinstance(data.get(list_key), list) or data.get(total_key) is None:
                raise ValueError("列表字段发生变化，请更新读取适配")
            rows = [normalized(item) for item in data[list_key]]
            return {"rows": rows, "ids": [str(r["id"]) for r in rows], "total": int(data.get(total_key) or 0)}

        return await checkpoint.paginate(
            json.dumps([kind, {k: v for k, v in body.items() if k != page_key}], sort_keys=True),
            read_page,
            lambda n, total: report(session, n, total, "分批读取列表"),
        )
    rows, seen, total, exhausted = [], set(), 0, False
    while len(rows) < limit:
        data = await post_json(session, READ_APIS[kind], body)
        result = checked_result(data)
        if not isinstance(result, dict) or list_key not in result:
            raise ValueError("列表字段发生变化，请更新读取适配")
        total = int(result.get(total_key) or 0)
        batch = result[list_key] or []
        previous = len(rows)
        for item in batch:
            row = normalized(item)
            if row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
        if len(rows) >= total:
            exhausted = True
            break
        if not batch or len(rows) == previous:
            break
        body[page_key] += 1
        report(session, len(rows), total, "读取列表分页")
        await asyncio.sleep(0.35)
    return {"rows": rows[:limit], "source_total": total, "complete": exhausted, "limit": limit}


async def collect(session, kind, options):
    from .read_page_pool import borrow
    from .request_reuse import lookup

    if kind in ("packages", "invoices", "aftersales", "evidence", "intercepts", "tickets", "reviews"):
        shared = lookup(session, kind, options)
        if shared is not None:
            report(session, stage="复用同模块已完成查询")
            return shared
    if kind == "tax" and session.shop.get("platform") == "xiaohongshu":
        from .xiaohongshu_tax import collect as collect_tax

        return await collect_tax(session, options)
    if kind == "tax" and session.shop.get("platform") in ("taobao", "jd"):
        from .platform_tax import collect as collect_tax

        return await collect_tax(session, options)
    if session.shop.get("platform", "pdd") != "pdd":
        from .platform_support import supports

        if session.shop.get("platform") != "taobao" or not supports("taobao", kind):
            raise ValueError("该平台此模块的数据采集尚未接入")
        from .taobao import collect as collect_taobao

        return await collect_taobao(session, kind, options)
    if kind == "orders":
        from .orders import OrdersPage, collect as collect_orders

        async with borrow(
            session, "aftersales" if options.get("status") == "退款/售后" else "orders", OrdersPage
        ) as page:
            await page.open("aftersales" if options.get("status") == "退款/售后" else "orders")
            return await collect_orders(page, options)
    if kind == "ad_logs":
        from .promotion_logs import collect as collect_logs

        return await collect_logs(session, options)
    if kind in ("ad_products", "ad_reports"):
        from .promotions import collect as collect_promotions

        return await collect_promotions(session, kind, options)
    if kind == "activities":
        from .activities import collect as collect_activities

        async with borrow(session, kind, ReadPage) as page:
            await page.open(kind)
            return await collect_activities(page, options)
    if kind == "goodsdata":
        from .goods_data import collect as collect_goods, GoodsPage

        async with borrow(session, kind, GoodsPage) as page:
            await page.open(kind)
            return await collect_goods(page, options)
    if kind == "transactions":
        from .transactions import collect as collect_transactions

        async with borrow(session, kind, ReadPage) as page:
            await page.open(kind)
            return await collect_transactions(page, options)
    if kind == "tax":
        from .tax_info import collect as collect_tax

        return await collect_tax(session, options)
    if kind in ("reconciliation_day", "reconciliation_month"):
        from .reconciliation import collect as collect_reconciliation

        async with borrow(session, kind, ReadPage) as page:
            await page.open(kind)
            return await collect_reconciliation(page, kind, options)
    if kind in ("packages", "invoices", "aftersales", "evidence", "intercepts", "navigator", "quality", "brands"):
        from .operations import collect_operations

        async with borrow(session, kind, ReadPage) as page:
            await page.open(kind)
            return await soft_retry(page, kind, lambda: collect_operations(page, kind, options))
    if kind in ("tickets", "reviews"):
        async with borrow(session, kind, ReadPage) as page:
            await page.open(kind)
            return await soft_retry(page, kind, lambda: fetch_list(page, kind, options))
    async with borrow(session, kind, ReadPage) as page:
        await page.open(kind)
        if kind == "overview":
            from .overview_summary import collect as collect_overview

            return await collect_overview(page)
        from .products import collect as collect_products

        return await collect_products(page)
