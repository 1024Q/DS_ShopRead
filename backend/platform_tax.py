"""Official Tmall/JD tax reporting statements, observed from authenticated UI requests."""

import asyncio
import base64
import json
import re
import time
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlsplit, parse_qs
from .business_reader import ReadPage

PAGES = {
    "taobao": "https://myseller.taobao.com/home.htm/whale-accountant/bill/summary?billDirection=TaxInformation&billType=month",
    "jd": "https://shop.jd.com/jdm/finance/billManage/taxReporting",
}
TB_BILL = "mtop.taobao.finance.taxbill.query"
TB_ENTITY = "mtop.taobao.finance.tax.identifier.query"
JD_BILL = "dsm.jsp2.ledger.spi.bill.TaxFilingDataReadProvider.getTaxFilingData"
FIELDS = {
    "commission_base": "小红书计佣基数合计",
    "settlement_net": "小红书月度净收益",
    "goods_income": "货物收入总额",
    "goods_refund": "货物退款总额",
    "goods_net": "货物收入净额",
    "service_income": "服务收入总额",
    "service_refund": "服务退款总额",
    "service_net": "服务收入净额",
    "platform_commission": "平台佣金/服务费合计",
    "commission_no_ads": "佣金/服务费（不含广告）",
    "ads_non_union": "非京东联盟广告费",
    "ads_union": "京东联盟广告费",
}


def cents(value):
    if value is None or isinstance(value, bool):
        raise ValueError("涉税账单缺少金额，保留原数据")
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError()
        return int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        raise ValueError("涉税账单金额格式异常") from None


def statement(period, entity, amounts, orders=None):
    if not entity.get("company_name"):
        raise ValueError("涉税账单未返回主体名称")
    year = int(period[:4])
    quarter = re.fullmatch(r"\d{4}-Q([1-4])", period)
    month = (int(quarter[1]) - 1) * 3 + 1 if quarter else int(period[5:])
    first = date(year, month, 1)
    end_month = month + (3 if quarter else 1)
    end = date(year + 1, 1, 1) if end_month > 12 else date(year, end_month, 1)
    values = {k + "_cents": cents(v) for k, v in amounts.items()}
    for prefix in ("goods", "service"):
        if values[prefix + "_income_cents"] - values[prefix + "_refund_cents"] != values[prefix + "_net_cents"]:
            raise ValueError("涉税账单收入、退款与净额不一致，保留原数据")
    values["tax_amount_cents"] = values["goods_net_cents"] + values["service_net_cents"]
    return {
        "id": period + ":" + entity["company_name"],
        "period": period,
        "created_at": first.isoformat(),
        "summary_through": (end - timedelta(days=1)).isoformat(),
        "entity": entity,
        "name": entity["company_name"],
        "official_tax": True,
        "tax_basis": "平台涉税信息报送账单：货物收入净额＋服务收入净额；佣金单列，不再次扣减",
        "partial": False,
        "orders": orders,
        **values,
        **{k.removesuffix("_cents"): v / 100 for k, v in values.items()},
    }


def tmall_rows(data, period):
    if not isinstance(data.get("billItems"), list):
        raise ValueError("天猫涉税账单格式变化")
    entity = {
        "company_name": data.get("subjectName", ""),
        "subject_type": {"ENTERPRISE": "企业", "PERSONAL": "个人"}.get(
            data.get("authenticateType"), data.get("authenticateType", "")
        ),
    }
    rows = []
    for item in data["billItems"]:
        match = re.match(r"(\d{4})年第([1-4])季度", item.get("reportPeriod", ""))
        if not match:
            raise ValueError("天猫报送季度无法识别")
        key = match[1] + "-Q" + match[2]
        if key != period:
            continue
        amounts = {}
        for prefix, field in [("goods", "goodsIncome"), ("service", "serviceIncome")]:
            for dest, source in [("income", "totalAmount"), ("refund", "refundAmount"), ("net", "netAmount")]:
                amounts[prefix + "_" + dest] = (item.get(field) or {}).get(source)
        amounts["platform_commission"] = item.get("platformCommissionTotal")
        rows.append(statement(key, entity, amounts))
    return rows, entity


def jd_rows(data, period):
    items = data.get("taxFilings")
    if not isinstance(items, list):
        raise ValueError("京东涉税账单格式变化")
    if data.get("total", len(items)) > len(items):
        raise ValueError("京东涉税账单返回分页数据，未完整读取")
    rows = []
    for item in items:
        key = str(item["year"]) + (
            "-Q" + str(item["quarter"]) if item.get("quarter") else "-" + str(item["month"]).zfill(2)
        )
        if key != period:
            continue
        mapping = {
            "goods_income": "goodIncomeTotal",
            "goods_refund": "goodRefund",
            "goods_net": "goodNet",
            "service_income": "serviceIncomeTotal",
            "service_refund": "serviceRefund",
            "service_net": "serviceNet",
            "commission_no_ads": "settleCommissionNotAdv",
            "ads_non_union": "advNotCps",
            "ads_union": "advCps",
        }
        amounts = {k: item.get(v) for k, v in mapping.items()}
        amounts["platform_commission"] = sum(
            Decimal(str(amounts[k])) for k in ("commission_no_ads", "ads_non_union", "ads_union")
        )
        rows.append(
            statement(
                key,
                {"company_name": item.get("companyName", ""), "subject_type": "平台报送主体"},
                amounts,
                item.get("orderNum"),
            )
        )
    entities = [
        {"company_name": name, "subject_type": "平台报送主体"} for name in (data.get("companyInfo") or {}).values()
    ]
    return rows, entities


class TaxPage(ReadPage):
    def __init__(self, session):
        super().__init__(session)
        self.requests = {}
        self.events = []
        self.hidden_target = session.shop.get("platform") != "jd"

    def observe(self, message):
        p = message.get("params", {})
        method = message.get("method")
        if method == "Network.requestWillBeSent":
            r = p["request"]
            u = urlsplit(r["url"])
            q = parse_qs(u.query)
            api = (q.get("api") or [""])[0]
            if u.hostname == "h5api.m.taobao.com":
                api = next((a for a in (TB_BILL, TB_ENTITY) if u.path == f"/h5/{a}/1.0/"), None)
            elif u.hostname != "sff.jd.com" or api != JD_BILL or r.get("method") != "POST":
                return
            if not api:
                return
            try:
                raw = json.loads((q.get("data") or [r.get("postData") or "{}"])[0])
                raw = raw.get("taxFilingQuery", raw)
            except ValueError:
                return
            self.requests[p["requestId"]] = (api, raw)
        elif method == "Network.loadingFinished" and p["requestId"] in self.requests:
            task = asyncio.create_task(self.body(p["requestId"]))
            self.body_tasks.add(task)
            task.add_done_callback(self.body_tasks.discard)

    async def body(self, request_id):
        api, query = self.requests.pop(request_id)
        try:
            result = await self.call("Network.getResponseBody", {"requestId": request_id})
            raw = base64.b64decode(result["body"]).decode() if result.get("base64Encoded") else result["body"]
            if api != JD_BILL:
                raw = re.sub(r"^\s*[\w$]+\(", "", raw).removesuffix(";").removesuffix(")")
            value = json.loads(raw)
            ok = (
                value.get("code") == 200
                if api == JD_BILL
                else any(x.startswith("SUCCESS::") for x in value.get("ret", []))
            )
            self.events.append((api, query, value.get("data") if ok else None))
        except ValueError, KeyError:
            self.events.append((api, query, None))

    async def wait(self, api, predicate=lambda q: True, after=0):
        end = time.monotonic() + 40
        while time.monotonic() < end:
            for a, q, data in reversed(self.events[after:]):
                if a == api and predicate(q):
                    if not isinstance(data, dict):
                        raise ValueError("官方涉税账单读取失败，请检查登录或财务权限")
                    return data
            await asyncio.sleep(0.2)
        raise ValueError("涉税账单页面未返回数据，请检查登录、财务权限后重试")

    async def click(self, label):
        for _ in range(40):
            if await self.evaluate(
                '(()=>{const e=[...document.querySelectorAll("*")].filter(e=>e.offsetWidth&&e.offsetHeight&&e.textContent.trim()==='
                + json.dumps(label)
                + ").sort((a,b)=>a.children.length-b.children.length)[0];e?.click();return !!e})()"
            ):
                return
            await asyncio.sleep(0.2)
        raise ValueError("涉税账单页面控件未找到：" + label)


async def collect(session, options):
    platform = session.shop["platform"]
    period = options["period"]
    if platform == "taobao" and not re.fullmatch(r"\d{4}-Q[1-4]", period):
        raise ValueError("天猫涉税报送账单按季度发布，请选择按季度查询；不将季度金额拆分为月份")
    async with TaxPage(session) as page:
        await page.call("Page.navigate", {"url": PAGES[platform]})
        if platform == "jd":
            data = await page.wait(
                JD_BILL, lambda q: q.get("queryType") is None and not q.get("dateS") and not q.get("dateE")
            )
            rows, entities = jd_rows(data, period)
        else:
            identity = await page.wait(TB_ENTITY)
            entities = identity.get("entityList", [])
            await page.wait(TB_BILL)
            current = await page.evaluate('document.querySelector("input[placeholder=请选择年]")?.value')
            # Changing the year clears the UI's persisted quarter bounds. Otherwise
            # a default Q4 filter can silently hide Q3 even when querying the year.
            years = (
                [str(int(current) + 1 if int(current) == 2025 else int(current) - 1)] if current == period[:4] else []
            ) + [period[:4]]
            for year in years:
                await page.evaluate('document.querySelector("input[placeholder=请选择年]")?.click()')
                await asyncio.sleep(0.2)
                selected = await page.evaluate(
                    '(()=>{const e=[...document.querySelectorAll("td.next-calendar2-cell")].find(e=>e.offsetWidth&&e.title==='
                    + json.dumps(year)
                    + ");e?.click();return !!e})()"
                )
                if not selected:
                    raise ValueError("天猫报送年份尚不可选择")
                await asyncio.sleep(0.2)
                if await page.evaluate('document.querySelector("input[placeholder=请选择年]")?.value') != year:
                    raise ValueError("天猫报送年份未切换成功")
            rows = []
            subjects = []
            for item in entities:
                if len(entities) > 1:
                    await page.evaluate('document.querySelector("#identifier")?.click()')
                    await page.click(item["name"])
                after = len(page.events)
                await page.click("搜索")
                data = await page.wait(TB_BILL, lambda q: str(q.get("reportYear")) == period[:4], after)
                if data.get("subjectName") != item.get("name"):
                    raise ValueError("天猫账单主体与选择项不一致，保留原数据")
                selected, entity = tmall_rows(data, period)
                rows.extend(selected)
                subjects.append(entity)
            entities = subjects
        if len({r["id"] for r in rows}) != len(rows):
            raise ValueError("涉税账单存在重复主体账期")
        if not rows:
            raise ValueError("平台尚未发布或未返回所选账期的涉税账单；不会以零覆盖原数据")
        return {
            "rows": rows,
            "entity": entities[0] if len(entities) == 1 else None,
            "entities": entities,
            "complete": True,
            "source_total": len(rows),
            "source_unit": "涉税报送账单",
            "source_name": "平台涉税信息报送账单",
            "scope_note": "天猫按季度；京东按月或季度。收入、退款、佣金采用平台报送口径，主体按账单保留。",
        }
