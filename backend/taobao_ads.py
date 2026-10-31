"""Alimama campaign and report reads from observed native UI requests."""

import asyncio
import json
from .taobao_page import TaobaoPage
from .taobao import number
from .taobao_operations import picture
from .sync_progress import report

PRODUCT_URL = "https://one.alimama.com/index.html#!/manage/onesite"
REPORT_URL = "https://one.alimama.com/index.html#!/report/account?rptType=account"
LIVE_URL = "https://one.alimama.com/index.html#!/report/real_time?rptType=real_time"
QUERY = "/report/query.json"
CAMPAIGNS = "/campaign/horizontal/findPage.json"
IDENTITY = "/member/checkAccess.json"
ALLOWED = {"one.alimama.com": {QUERY, CAMPAIGNS, IDENTITY}}
FIELDS = {
    "ad_spend": "charge",
    "ad_revenue": "alipayInshopAmt",
    "ad_orders": "alipayInshopNum",
    "impressions": "adPv",
    "clicks": "click",
    "net_revenue": "retainedAlipayInshopAmt",
    "net_orders": "retainedAlipayInshopNum",
    "ctr": "ctr",
    "cvr": "cvr",
    "cost_per_order": "alipayInshopCost",
}


def checked(body):
    if body.get("info", {}).get("ok") is not True or not isinstance(body.get("data"), (dict, list)):
        raise ValueError("阿里妈妈未返回有效数据，请检查登录和查看权限")
    return body["data"]


def fields(raw):
    result = {key: number(raw.get(source)) for key, source in FIELDS.items()}
    for key in ("ctr", "cvr"):
        if result[key] is not None:
            result[key] *= 100
    spend = result["ad_spend"]
    for key, revenue in [("roi", "ad_revenue"), ("net_roi", "net_revenue")]:
        result[key] = result[revenue] / spend if spend and spend > 0 and result[revenue] is not None else None
    # The account's total charge panel includes excluded scenes; do not substitute report charge.
    result.update(total_spend=None, settlement_revenue=None, settlement_roi=None)
    return result


def matches(q, options):
    return q.get("startTime") == options["date_from"] and q.get("endTime") == options["date_to"]


def campaign(raw, options, configuration_only=False):
    identifier = raw.get("campaignId")
    if not identifier:
        raise ValueError("推广计划缺少编号")
    reports = [r for r in raw.get("reportInfoList") or [] if matches(r.get("condition", {}), options)]
    if raw.get("reportInfoList") and len(reports) != 1 and not configuration_only:
        raise ValueError("推广计划统计日期不一致，保留原记录")
    groups = raw.get("adgroupList") or []
    material = (groups[0].get("material") or {}) if len(groups) == 1 else {}
    return {
        "id": str(identifier),
        "row_type": "campaign",
        "goods_id": str(material.get("materialId") or ""),
        "name": material.get("title") or raw.get("campaignName") or "",
        "campaign_name": raw.get("campaignName") or "",
        "thumbnail_url": picture(material.get("imageUrl") or material.get("imgUrl")),
        "status": {"start": "推广中", "pause": "已暂停"}.get(
            raw.get("displayStatus"), str(raw.get("displayStatus") or "平台未提供")
        ),
        "promotion_type": "货品全站推广 · " + ("全店模式" if raw.get("itemSelectedMode") == "shop" else "商品计划"),
        "target_roi": number(raw.get("constraintValue")),
        "target_roi_label": "净目标投产比" if raw.get("optimizeTarget") == "ad_strategy_retained_buy" else "目标投产比",
        "budget": number(raw.get("dayBudget")),
        "budget_text": "不限" if raw.get("dmcType") == "unlimit" else None,
        "created_at": options["date_to"],
        "period": options["date_from"] + " 至 " + options["date_to"],
        "note": "仅提供当前计划配置，未返回本期独立报表" if configuration_only or not reports else "",
        **fields(reports[0] if reports else {}),
    }


async def identity(page):
    body, _ = await page.wait(IDENTITY)
    meta = checked(body).get("meta") or {}
    nick = meta.get("nickName") or ""
    expected = page.session.shop.get("detected_name")
    if not nick or not meta.get("memberId"):
        raise ValueError("阿里妈妈未返回推广账户身份")
    if expected and nick != expected and not nick.startswith(expected + ":"):
        raise ValueError("阿里妈妈账户与店铺不一致，未保存数据")
    return str(meta["memberId"])


async def select_range(page, options, product=False):
    scope = options["trade_range"]
    if scope == "realtime":
        return
    if not product or scope in ("month", "quarter"):
        if product:
            await page.click("自定义")
        else:
            await page.next_page(".mxgc-calendar-rangepicker .mx-trigger")
        await asyncio.sleep(0.2)
    label = {"yesterday": "昨日", "week": "过去 7 天", "month": "过去 30 天"}.get(scope)
    if label:
        # Popup shortcut buttons take precedence over the closed trigger's same label.
        expression = (
            "(()=>{const es=[...document.querySelectorAll('button')].filter(e=>e.offsetWidth&&e.textContent.replace(/\\s/g,'')==="
            + json.dumps(label.replace(" ", ""))
            + ");if(!es.length)return false;es[es.length-1].click();return true})()"
        )
        if not await page.evaluate(expression):
            await page.click(label)
        # Some calendar modes stage selection until the user presses Confirm.
        await asyncio.sleep(0.15)
        await page.evaluate(
            "(()=>{const e=[...document.querySelectorAll('*')].find(e=>e.offsetWidth&&e.children.length===0&&e.textContent.trim()==='确定');e?.click()})()"
        )
    else:
        raise ValueError("淘宝推广暂支持今日、昨日、近 7 日、近 30 日；近 90 日请在阿里妈妈官方报表查询")


async def products(page, options):
    await page.navigate(PRODUCT_URL)
    account = await identity(page)
    await page.wait(CAMPAIGNS, lambda q: q.get("adgroupRequired") is True)
    await select_range(page, options, True)
    body, q = await page.wait(
        CAMPAIGNS, lambda q: q.get("adgroupRequired") is True and matches(q, options) and int(q.get("offset", 0)) == 0
    )
    if str(q.get("effectEqual")) != "15" or q.get("unifyType") != "last_click_by_effect_time":
        raise ValueError("货品全站推广归因设置已变化，请在官方页选择成交时间、15 天后重试")
    data = checked(body)
    batch = data.get("list")
    total = data.get("count")
    if not isinstance(batch, list) or not isinstance(total, int):
        raise ValueError("推广计划列表格式发生变化")
    rows = [campaign(r, options) for r in batch]
    loaded = len(batch)
    size = int(q.get("pageSize", 40))
    offset = 0
    while loaded < total and loaded < 2000:
        offset += size
        mark = len(page.events)
        await page.next_page('.mxgc-pagination [data-spm-click$="d410df3c5next"]')
        body, q = await page.wait(
            CAMPAIGNS,
            lambda q: q.get("adgroupRequired") is True and matches(q, options) and int(q.get("offset", -1)) == offset,
            after=mark,
        )
        batch = checked(body).get("list")
        if not isinstance(batch, list) or not batch:
            raise ValueError("推广列表分页未前进，保留原记录")
        existing = {r["id"] for r in rows}
        new = [campaign(r, options) for r in batch]
        if any(r["id"] in existing for r in new):
            raise ValueError("推广列表翻页出现重复计划，保留原记录")
        rows.extend(new)
        loaded += len(new)
        report(page, loaded, total, "读取淘宝货品全站推广")
    # Native full-shop campaigns are requested separately; retain their distinct plan IDs.
    for path, query, envelope in page.events:
        if path == CAMPAIGNS and query.get("itemSelectedMode") == "shop":
            for raw in checked(envelope).get("list", []):
                row = campaign(raw, options, configuration_only=True)
                if row["id"] not in {r["id"] for r in rows}:
                    rows.append(row)
    report(page, loaded, total, "读取淘宝货品全站推广")
    body, q = await page.wait(
        QUERY,
        lambda q: (
            q.get("bizCode") == "onebpSite"
            and q.get("queryDomains") == []
            and not q.get("strategyCampaignIdIn")
            and q.get("itemSelectedMode") == "all"
            and matches(q, options)
        ),
    )
    result = checked(body)
    aggregate = result if isinstance(result, list) else result.get("list", [])
    if len(aggregate) != 1:
        raise ValueError("货品全站推广概况未返回")
    metrics = {**fields(aggregate[0]), "daily": [], "range": options["trade_range"]}
    return {
        "rows": rows,
        "metrics": metrics,
        "source_total": total + len(rows) - loaded,
        "complete": loaded >= total,
        "source_unit": "推广计划",
        "source_shop_id": account,
        "source_name": "阿里妈妈 · 货品全站推广",
        "scope_note": "货品全站推广商品计划及全店模式；不包含其他推广场景的计划。成交时间口径，转化周期 15 天；预算与状态为当前配置，报表为所选周期。每条为计划汇总，计划含多商品时不重复分摊金额。未提供独立报表的全店计划金额显示 —。",
    }


async def reports(page, options):
    live = options["trade_range"] == "realtime"
    await page.navigate(LIVE_URL if live else REPORT_URL)
    account = await identity(page)
    await page.wait(QUERY, lambda q: q.get("bizCode") == "universalBP")
    if not live:
        await select_range(page, options)

    def match(q, dimension):
        return (
            q.get("bizCode") == "universalBP"
            and q.get("queryDomains") == [dimension]
            and matches(q, options)
            and bool(q.get("fromRealTime")) == live
        )

    body, q = await page.wait(QUERY, lambda q: match(q, "account"))
    if q.get("unifyType") != "zhai" or (not live and str(q.get("effectEqual")) != "15"):
        raise ValueError("推广报表归因设置已变化，请在官方页选择末次点击归因、历史 15 天后重试")
    aggregate = checked(body).get("list", [])
    if len(aggregate) != 1:
        raise ValueError("推广报表概况未返回")
    summary = fields(aggregate[0])
    body, _ = await page.wait(QUERY, lambda q: match(q, "date"))
    daily = []
    for raw in checked(body).get("list", []):
        date = str(raw.get("thedate") or "")
        if not options["date_from"] <= date[:10] <= options["date_to"]:
            raise ValueError("推广趋势日期不匹配")
        daily.append({"date": date, **fields(raw)})
    body, _ = await page.wait(QUERY, lambda q: match(q, "scene"))
    data = checked(body)
    rows = []
    for raw in data.get("list", []):
        if raw.get("sceneId") is None:
            raise ValueError("推广场景缺少编号")
        rows.append(
            {
                "id": str(raw["sceneId"]),
                "row_type": "promotion_scene",
                "name": raw.get("scene1Name") or "",
                "goods_id": "",
                "status": "报表记录",
                "promotion_type": "营销场景",
                "created_at": options["date_to"],
                **fields(raw),
            }
        )
    note = (
        "实时报表不包括货品全站推广、控全站费比、超播券、超播全站推；可能延迟 1–2 小时。"
        if live
        else "营销场景报表不包括控全站费比、超播券、超播全站推；转化周期 15 天。"
    )
    note += "末次点击归因。花费、交易额及 ROI 来自同一报表口径，不能视为全账户现金扣费；未提供的净额与总花费显示 —。"
    return {
        "rows": rows,
        "metrics": {**summary, "daily": daily, "range": options["trade_range"], "scope_note": note},
        "source_total": data.get("count", len(rows)),
        "complete": len(rows) >= int(data.get("count", len(rows))),
        "source_name": "阿里妈妈 · " + ("实时报表" if live else "营销场景报表"),
        "source_shop_id": account,
        "source_unit": "推广场景",
        "scope_note": note,
    }


async def collect(session, kind, options):
    async with TaobaoPage(session, ALLOWED) as page:
        return await products(page, options) if kind == "ad_products" else await reports(page, options)
