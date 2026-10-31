"""Local extraction for the text electronic-invoice layout, with conservative matching."""

import io, re, unicodedata
from decimal import Decimal, InvalidOperation


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).upper()


def cents(value):
    try:
        number = Decimal(str(value).replace(",", "")) * 100
        return int(number) if number.is_finite() and number == number.to_integral_value() else None
    except ValueError, InvalidOperation, TypeError:
        return None


def extract(content):
    import pdfplumber

    if not content.startswith(b"%PDF-"):
        raise ValueError("文件不是有效 PDF")
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        if not 1 <= len(pdf.pages) <= 10:
            raise ValueError("仅支持 1–10 页的单张发票 PDF")
        texts = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(texts)
        compact = normalized(text)
        if len(compact) < 80:
            raise ValueError("扫描件或没有可识别文字，请提供原始电子发票 PDF")
        if "红字" in compact or "负数发票" in compact:
            raise ValueError("红字发票需单独处理")
        nums = set(re.findall(r"发票号码[:：]?(\d{8,20})(?!\d)", compact))
        if len(nums) != 1:
            raise ValueError("无法唯一识别发票号码；一个 PDF 只能包含一张发票")
        amounts = set(re.findall(r"小写[)）]?[:：]?[¥￥]?(-?\d[\d,]*\.\d{2})(?!\d)", compact))
        if len(amounts) != 1:
            raise ValueError("无法唯一识别价税合计金额")
        amount = cents(next(iter(amounts)))
        if not amount or amount < 0:
            raise ValueError("仅支持金额大于零的蓝字发票")
        if "普通发票" not in compact:
            raise ValueError("当前支持电子普通发票，其他票种请单独核对")
        page = pdf.pages[0]
        headers = page.search("项目名称|货物或应税劳务") or []
        if not headers:
            raise ValueError("发票版式尚未适配，无法可靠区分购销双方")
        bottom = min(x["top"] for x in headers)
        parties = []
        for left, right in ((0, page.width / 2), (page.width / 2, page.width)):
            section = page.crop((left, 0, right, bottom)).extract_text() or ""
            lines = [normalized(line) for line in section.splitlines()]
            names = [re.split(r"名称[:：]", line, maxsplit=1)[1] for line in lines if re.search(r"名称[:：]", line)]
            taxes = [
                re.split(r"(?:识别号|信用代码)[:：]", line, maxsplit=1)[1]
                for line in lines
                if re.search(r"(?:识别号|信用代码)[:：]", line)
            ]
            if len(names) != 1 or len(taxes) != 1:
                raise ValueError("购销双方名称或税号栏识别不完整")
            tax = taxes[0]
            if tax and not re.fullmatch("[A-Z0-9]{15,20}", tax):
                raise ValueError("税号格式无法确认")
            parties.append((names[0], tax))
        date = re.search(r"开票日期[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日", compact)
        if not date:
            raise ValueError("无法识别开票日期")
        from datetime import date as Date

        issued = Date(*map(int, date.groups())).isoformat()
        buyer, seller = parties
        if not seller[1]:
            raise ValueError("销售方税号缺失")
        return dict(
            invoice_number=next(iter(nums)),
            invoice_date=issued,
            amount_cents=amount,
            buyer_name=buyer[0],
            buyer_tax=buyer[1],
            seller_name=seller[0],
            seller_tax=seller[1],
            title_type="企业" if buyer[1] else "个人",
            invoice_kind=0,
            order_numbers=sorted(set(re.findall(r"(?<!\d)\d{6}-\d{12,18}(?!\d)", text))),
            pages=len(pdf.pages),
        )


def conflicts(parsed, row, entity):
    errors = []
    if row.get("status") != "待开票":
        errors.append("申请已不是待开票")
    if normalized(entity.get("company_name")) != parsed["seller_name"]:
        errors.append("销售方与店铺主体不一致或主体未读取")
    tax = entity.get("credit_code") or entity.get("taxpayer_number")
    if not tax or normalized(tax) != parsed["seller_tax"]:
        errors.append("销售方税号与店铺主体不一致或未读取")
    if normalized(row.get("invoice_title")) != parsed["buyer_name"]:
        errors.append("发票抬头不一致")
    if row.get("invoice_title_type") != parsed["title_type"]:
        errors.append("抬头类型不一致或未读取")
    if parsed["title_type"] == "企业" and normalized(row.get("buyer_taxpayer_number")) != parsed["buyer_tax"]:
        errors.append("购买方税号不一致")
    if cents(row.get("amount")) != parsed["amount_cents"]:
        errors.append("发票金额不一致")
    if parsed["order_numbers"] and (
        len(parsed["order_numbers"]) != 1 or row.get("order_number") != parsed["order_numbers"][0]
    ):
        errors.append("PDF 内订单号不一致或包含多个订单")
    return errors
