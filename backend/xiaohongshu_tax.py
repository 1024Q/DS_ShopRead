"""Monthly settlement commission bases, per the merchant portal's reporting note."""

import logging
import asyncio
import io
import json
import re
import zipfile
from datetime import date, timedelta
from urllib.parse import urlsplit

import httpx
from .business_reader import ReadPage
from .platform_tax import cents
from . import safe_xml

PAGE = "https://ark.xiaohongshu.com/app-finance/bill/settle/monthly"
BASIS = "小红书月度结算账单：订单结算页签计佣基数合计（含退款负数）；不采用月度净收益，不再次扣减佣金"
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def workbook_rows(content):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        if sum(i.file_size for i in z.infolist()) > 100_000_000:
            raise ValueError("小红书账单解压后过大，未覆盖原数据")
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = ["".join(e.itertext()) for e in safe_xml.fromstring(z.read("xl/sharedStrings.xml"))]
        rels = {e.attrib["Id"]: e.attrib["Target"] for e in safe_xml.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
        result = {}
        for sheet in safe_xml.fromstring(z.read("xl/workbook.xml")).findall("m:sheets/m:sheet", NS):
            name = sheet.attrib["name"]
            if name not in ("总览", "订单结算"):
                continue
            target = rels[sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]]
            path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            rows = []
            for row in safe_xml.fromstring(z.read(path)).findall("m:sheetData/m:row", NS):
                values = {}
                for c in row:
                    key = re.sub(r"\d+", "", c.attrib["r"])
                    value = c.findtext("m:v", "", NS)
                    if c.attrib.get("t") == "s":
                        value = strings[int(value)]
                    elif c.attrib.get("t") == "inlineStr":
                        value = "".join(c.find("m:is", NS).itertext())
                    values[key] = value
                rows.append(values)
            result[name] = rows
        return result


def normalize_bill(bill, content):
    if bill.get("processStatus") != "SUCCESS" or bill.get("currency") != "CNY":
        raise ValueError("小红书月账单尚未生成完成或币种不支持")
    start = date.fromisoformat(bill["startTime"][:10])
    end = date.fromisoformat(bill["endTime"][:10])
    expected_end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    if start.day != 1 or end != expected_end:
        raise ValueError("小红书账期不是完整自然月")
    sheets = workbook_rows(content)
    overview = {r.get("A"): r.get("B") for r in sheets.get("总览", [])}
    if not bill.get("companyName") or overview.get("公司名称：") != bill["companyName"]:
        raise ValueError("小红书下载账单主体与月账单不一致")
    if overview.get("结算期间：") != f"{start}~{end - timedelta(days=1)}":
        raise ValueError("小红书下载账单月份不一致")
    rows = sheets.get("订单结算", [])
    if not rows:
        raise ValueError("小红书账单缺少订单结算页签")
    headers = {v: k for k, v in rows[0].items()}
    if not {"订单号", "计佣基数", "动账金额", "结算时间"} <= headers.keys():
        raise ValueError("小红书订单结算字段变化，未覆盖原数据")
    detail = [r for r in rows[1:] if any(r.values())]
    if len(detail) != int(bill["settleOrderCount"]):
        raise ValueError("小红书订单结算明细笔数不完整")
    bases = []
    movements = []
    for row in detail:
        if not row.get(headers["订单号"]) or not str(start) <= row.get(headers["结算时间"], "")[:10] < str(end):
            raise ValueError("小红书结算明细存在错误月份或缺失订单号")
        bases.append(cents(row.get(headers["计佣基数"])))
        movements.append(cents(row.get(headers["动账金额"])))
    fees = {x["code"]: x for x in bill.get("feeDetailInfo", [])}
    if not {"STATEMENT_IN", "STATEMENT_REFUND"} <= fees.keys():
        raise ValueError("小红书月账单缺少订单销售/退款核对金额")
    if sum(movements) != sum(cents(fees[k]["displayValue"]) for k in ("STATEMENT_IN", "STATEMENT_REFUND")):
        raise ValueError("小红书订单结算金额与月账单不一致")
    values = {
        "commission_base": sum(bases),
        "tax_amount": sum(bases),
        "goods_income": sum(v for v in bases if v > 0),
        "goods_refund": -sum(v for v in bases if v < 0),
        "goods_net": sum(bases),
        "settlement_net": cents(bill["totalChangeAmount"]),
        "platform_commission": cents(bill["totalCommissionAmount"]),
    }
    entity = {
        "company_name": bill["companyName"],
        "credit_code": (bill.get("ext") or {}).get("uniformCode", ""),
        "subject_type": "账单主体",
    }
    return {
        "id": bill["statementNo"],
        "statement_number": bill["statementNo"],
        "period": str(start)[:7],
        "created_at": str(start),
        "summary_through": str(end - timedelta(days=1)),
        "entity": entity,
        "name": entity["company_name"],
        "official_tax": True,
        "tax_source": "小红书月度结算账单",
        "tax_basis": BASIS,
        "partial": False,
        "orders": len(detail),
        **{k + "_cents": v for k, v in values.items()},
        **{k: v / 100 for k, v in values.items()},
    }


class XhsPage(ReadPage):
    hidden_target = False

    async def request(self, endpoint, payload):
        value = await self.evaluate(
            "fetch('/api/edith/settlebill/"
            + endpoint
            + "',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify("
            + json.dumps(payload)
            + ")}).then(r=>r.json())"
        )
        if not isinstance(value, dict) or value.get("success") is not True or value.get("code") != 0:
            raise ValueError("小红书账单读取失败，请检查登录和财务权限")
        return value["data"]


async def collect(session, options):
    start = date.fromisoformat(options["date_from"])
    end = date.fromisoformat(options["date_to"])
    # Both tax options and platform requests use an exclusive end date.
    wanted = []
    cursor = start
    while cursor < end:
        wanted.append(str(cursor)[:7])
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    rows = []
    async with XhsPage(session) as page:
        await page.call("Page.navigate", {"url": PAGE})
        for _ in range(60):
            try:
                if await page.evaluate(
                    "location.origin==='https://ark.xiaohongshu.com' && document.readyState==='complete'"
                ):
                    break
            except Exception:
                logging.getLogger(__name__).debug("等待小红书页面就绪", exc_info=True)
            await asyncio.sleep(0.2)
        else:
            raise ValueError("小红书账单页面未完成加载")
        bills = []
        number = 1
        while True:
            data = await page.request(
                "query_period_settle_bill_list",
                {
                    "periodType": "MONTH",
                    "timeType": "SETTLE_TIME",
                    "startTime": str(start) + " 00:00:00",
                    "endTime": str(end) + " 00:00:00",
                    "pageNum": number,
                    "pageSize": 10,
                },
            )
            items = data.get("list")
            if not isinstance(items, list):
                raise ValueError("小红书月账单格式变化")
            bills.extend(items)
            if len(bills) >= int(data["total"]):
                break
            if not items or number >= 100:
                raise ValueError("小红书月账单分页不完整")
            number += 1
        if sorted(b["startTime"][:7] for b in bills) != wanted:
            raise ValueError("小红书所选账期尚未全部出账，保留原数据；季度必须包含三个月")
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            for bill in bills:
                data = await page.request(
                    "get_bill_download_url",
                    {"periodSettleNo": bill["periodSettleNo"], "billType": "PERIOD_SETTLE_BILL"},
                )
                url = data.get("downloadUrl", "")
                parsed = urlsplit(url)
                if parsed.scheme != "https" or parsed.hostname != "rcsh-finance-file-ros.ros-cn-sh.xhscloud.com":
                    raise ValueError("小红书账单下载地址尚未就绪或来源发生变化")
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ValueError("小红书账单下载失败，请稍后重试")
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 30_000_000:
                            raise ValueError("小红书月账单文件过大")
                rows.append(await asyncio.to_thread(normalize_bill, bill, bytes(content)))
    entities = list({r["entity"]["company_name"]: r["entity"] for r in rows}.values())
    return {
        "rows": rows,
        "entities": entities,
        "entity": entities[0] if len(entities) == 1 else None,
        "complete": True,
        "source_total": len(rows),
        "source_unit": "月度账单",
        "source_name": "小红书月度结算账单",
        "scope_note": BASIS,
    }
