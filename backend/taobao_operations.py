"""Qianniu package warnings and buyer invoice applications, read-only UI adapters."""

import asyncio
import json
from .taobao_page import TaobaoPage
from .taobao import number
from .sync_progress import report
from .read_errors import PageReadError

PACKAGE = "https://myseller.taobao.com/home.htm/package-center/packageMonitor"
INVOICE = "https://myseller.taobao.com/home.htm/merchant-invoice/"
LIST = "/h5/mtop.taobao.logistics.console.seller.exception.data.list/1.0/"
COUNT = "/h5/mtop.taobao.logistics.console.seller.exception.count/1.0/"
INVOICE_LIST = "/api/qianniu/invoice/list/all"
ALLOWED = {"h5api.m.taobao.com": {LIST, COUNT}, "einvoice.taobao.com": {INVOICE_LIST}}
SHIPPING = {
    "PAY_CONSIGN_TIMEOUT": "发货预警",
    "PAY_GOT_TIMEOUT_ILLEGAL": "揽收异常",
    "GOT_UPDATE_TIMEOUT": "揽收更新异常",
    "TRANSPORT_STAGNATION": "运输更新异常",
    "DELIVERY_SIGN_TIMEOUT": "派送更新异常",
}


def checked(body, invoice=False):
    if invoice:
        if body.get("code") != 200 or not isinstance(body.get("data"), list):
            raise ValueError("给买家开票数据未就绪或没有权限")
        return body
    if not any(str(r).startswith("SUCCESS::") for r in body.get("ret", [])):
        raise ValueError("包裹中心数据未就绪或没有权限")
    data = body.get("data", {}).get("data")
    if not isinstance(data, dict):
        raise ValueError("包裹中心数据结构发生变化")
    return data


def picture(value):
    value = str(value or "")
    return "https:" + value if value.startswith("//") else value if value.startswith("https://") else ""


def package(raw, label, reverse=False):
    key = raw.get("id") or raw.get("jointPrimaryKey")
    if not key:
        raise ValueError("包裹预警缺少唯一编号")
    items = raw.get("subTradeOrderList") or []
    item = items[0] if items else {}
    traces = raw.get("traceDetailList") or []
    latest = max(traces, key=lambda t: t.get("time") or "", default={})
    return {
        "id": str(key),
        "row_type": "package_warning",
        "order_number": str(raw.get("tradeId") or raw.get("subTradeId") or ""),
        "tracking_number": str(raw.get("mailNo") or ""),
        "return_tracking_number": str(raw.get("mailNo") or "") if reverse else "",
        "carrier": raw.get("cpName") or raw.get("cpCode") or "",
        "goods_id": str(item.get("itemId") or ""),
        "name": item.get("goodsName") or "",
        "thumbnail_url": picture(item.get("iconUrl")),
        "status": ("退货 · " if reverse else "") + label,
        "problem": raw.get("eventStatusName") or "",
        "progress": raw.get("sellerOverTimeText") or "",
        "logistics_status": latest.get("statusDesc") or "",
        "logistics_time": latest.get("time") or "",
        "created_at": raw.get("consignTime") or "",
        "warning_at": raw.get("gmtCreate") or "",
        "estimated_claim": number(raw.get("estimateClaimAmount")),
        "warning_level": 2 if raw.get("eventStatus") == 2 else 1,
        "amount": None,
    }


def invoice(raw, status):
    key = raw.get("serialNo")
    order = raw.get("tid")
    if not key or not order:
        raise ValueError("开票记录缺少申请编号或订单号")
    return {
        "id": str(key),
        "row_type": "buyer_invoice",
        "order_number": str(order),
        "goods_id": "",
        "name": "",
        "status": "待开票" if status.startswith(("待处理申请", "待录入开票")) else status,
        "progress": status,
        "created_at": raw.get("applyGmtCreate") or raw.get("startTime") or "",
        "amount": number(raw.get("amount")),
        "invoice_title": raw.get("payerName") or "",
        "invoice_number": raw.get("invoiceNo") or "",
        "deadline": raw.get("rightsDueDateTime") or "",
        "problem": raw.get("failedReason") or raw.get("bizErrorDesc") or "",
    }


async def identity(page):
    for _ in range(40):
        name = await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('[class*=shopName--]')].find(e=>e.offsetWidth);return e?[...e.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent).join('').trim():''})()"
        )
        if name:
            expected = page.session.shop.get("detected_name")
            if expected and name != expected:
                raise ValueError("千牛当前店铺与配置不一致，未保存数据")
            return
        await asyncio.sleep(0.2)
    raise PageReadError("千牛未显示已登录店铺身份，请重新确认登录")


async def packages(page, kind, options):
    await page.navigate(PACKAGE)
    await identity(page)
    rows = []
    counts = []
    seen = set()
    complete = True
    for reverse in (False, True):
        mark = len(page.events)
        tab_start = mark if reverse else 0
        if reverse:
            await page.click("退货包裹预警")
        body, _ = await page.wait(
            COUNT,
            lambda q: (q.get("condition", {}).get("bizType") == "REVERSE") == reverse,
            after=mark if reverse else 0,
        )
        groups = checked(body).get("statisticDetails")
        if not isinstance(groups, list):
            raise ValueError("包裹中心预警分类结构发生变化")
        for group in groups:
            count = int(group["count"])
            code = group["value"]
            label = group["name"] if reverse else SHIPPING.get(code)
            counts.append({"name": ("退货 · " if reverse else "") + (label or group["name"]), "count": count})
            if not count:
                continue
            if not label:
                raise ValueError("包裹中心出现未适配预警分类：" + group["name"])
            # An already selected category emits no new request. Reuse only this tab's events.
            mark = tab_start
            await page.click(label, prefix=True)
            for current in range(1, 101):
                body, q = await page.wait(
                    LIST,
                    lambda q: (
                        int(q.get("pageIdx", 0)) == current
                        and q.get("featureMap", {}).get("flag") == code
                        and (q.get("condition", {}).get("bizType") == "REVERSE") == reverse
                    ),
                    after=mark,
                )
                data = checked(body)
                items = data.get("itemList")
                paging = data.get("paging", {})
                if not isinstance(items, list) or "totalNum" not in paging:
                    raise ValueError("包裹预警列表格式变化")
                for raw in items:
                    row = package(raw, label, reverse)
                    if row["id"] not in seen:
                        rows.append(row)
                        seen.add(row["id"])
                report(page, len(rows), None, "读取淘宝包裹预警")
                if current * int(paging.get("pageSize", 20)) >= int(paging["totalNum"]):
                    break
                if not items:
                    raise ValueError("包裹预警分页未完成")
                mark = len(page.events)
                await page.next_page(".paginationCss .next-next")
            else:
                complete = False
    return {
        "rows": rows,
        "source_total": len(rows) if complete else None,
        "complete": complete,
        "source_unit": "包裹预警",
        "source_name": "淘宝物流 · 包裹中心",
        "metrics": {"warning_counts": counts},
        "scope_note": "淘宝售后工作台按包裹中心展示发货及退货包裹预警；默认官方近 7 天预警，不代表全部退款售后。日期按发货时间在已读取记录中筛选。",
    }


async def invoices(page, options):
    await page.navigate(INVOICE)
    await identity(page)
    await page.click("全部申请")
    body, q = await page.wait(INVOICE_LIST)
    rows = []
    seen = set()
    coverage = (q.get("startTime"), q.get("endTime"))
    # The native default covers two months; use native date inputs for explicit dates.
    if options.get("explicit_dates"):
        await page.evaluate("document.querySelector('input[placeholder=起始日期]').click()")
        await asyncio.sleep(0.2)
        for value in (options["date_from"], options["date_to"]):
            for attempt in range(14):
                picked = await page.evaluate(
                    "(()=>{const date="
                    + json.dumps(value)
                    + ";const cells=[...document.querySelectorAll('.next-calendar2-cell[title]')].filter(e=>e.offsetWidth&&!e.className.includes('disabled'));const e=cells.find(e=>e.title===date);if(e){e.click();return true;}const dates=cells.map(e=>e.title).sort();const direction=date<dates[0]?'left':'right';const arrows=[...document.querySelectorAll('.next-calendar2-header-btn .next-icon-arrow-'+direction)].filter(e=>e.offsetWidth);arrows[direction==='left'?0:arrows.length-1]?.closest('button')?.click();return false})()"
                )
                if picked:
                    break
                await asyncio.sleep(0.15)
            else:
                raise ValueError("开票日期超出官方日历可选范围")
            await asyncio.sleep(0.2)
        mark = len(page.events)
        await page.next_page("button[type=submit]")
        body, q = await page.wait(
            INVOICE_LIST,
            lambda q: q.get("startTime") == options["date_from"] and q.get("endTime") == options["date_to"],
            after=mark,
        )
        coverage = (q["startTime"], q["endTime"])
    total = None
    complete = False
    for current in range(100):
        data = checked(body, True)
        batch = data["data"]
        total = int(data["total"])
        # Read displayed statuses instead of guessing undocumented invoice state codes.
        displayed = await page.evaluate(
            "[...document.querySelectorAll('tr')].filter(e=>e.offsetWidth).map(e=>e.innerText)"
        )
        for raw in batch:
            order = str(raw.get("tid") or "")
            serial = str(raw.get("serialNo") or "")
            text = next((t for t in displayed if serial and serial in t), None) or next(
                (t for t in displayed if order and order in t), ""
            )
            status = next(
                (
                    s
                    for s in [
                        "待处理申请-待同意",
                        "待处理申请-协商中",
                        "待录入开票",
                        "开票中",
                        "开票失败",
                        "开票成功",
                        "已拒绝",
                        "已取消",
                    ]
                    if s in text
                ),
                None,
            )
            if not status:
                status = (
                    "待处理申请" if raw.get("applyStatus") == 1 else "开票成功" if raw.get("applyStatus") == 6 else None
                )
            if not status:
                raise ValueError("开票状态无法与官方列表对应，保留原记录")
            row = invoice(raw, status)
            if row["id"] not in seen:
                seen.add(row["id"])
                rows.append(row)
        report(page, len(rows), total, "读取淘宝给买家开票")
        if len(rows) >= total:
            complete = True
            break
        if not batch:
            raise ValueError("开票分页未完成")
        mark = len(page.events)
        await page.next_page("[class*=TableView_pagination] .next-next")
        body, q = await page.wait(
            INVOICE_LIST,
            lambda q: int(q.get("pageNo", -1)) == current + 1 and (q.get("startTime"), q.get("endTime")) == coverage,
            after=mark,
        )
        await asyncio.sleep(0.15)
    return {
        "rows": rows,
        "source_total": total,
        "complete": complete,
        "source_name": "淘宝财务 · 给买家开票",
        "source_unit": "开票申请",
        "metrics": {},
        "scope_note": f"给买家开票申请，已读取官方日期范围 {coverage[0]} 至 {coverage[1]}；金额单位为元。默认显示待处理及待录入开票，支持状态、申请日期筛选。",
    }


async def collect(session, kind, options):
    async with TaobaoPage(session, ALLOWED) as page:
        return await invoices(page, options) if kind == "invoices" else await packages(page, kind, options)
