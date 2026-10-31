from .shop_identity import shop_label

"""Small dependency-free OOXML writer; identifiers and user text stay literal."""
import io
import math
import re
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape
from urllib.parse import quote
from fastapi.responses import Response
from .export_labels import EXTRA, OVERRIDES, MODULES, VALUES, display

LABELS = dict(
    shop_name="店铺",
    id="记录 ID",
    goods_id="商品 ID",
    name="商品名称",
    thumbnail_url="主图地址",
    order_number="订单号",
    status="状态",
    priority="优先级",
    problem="问题",
    progress="处理进度",
    deadline="截止时间",
    created_at="创建时间",
    tracking_number="快递单号",
    return_tracking_number="退货单号",
    carrier="快递公司",
    content="评价内容",
    stars="评价星级",
    reply="商家回复",
    replied="是否回复",
    price_min="最低价格（元）",
    price_max="最高价格（元）",
    stock="库存",
    sales="成交金额 / 销量（见模块口径）",
    sales_30d="30 日销量",
    visitors="访客数",
    views="浏览量",
    quantity="成交件数",
    orders="订单数",
    buyers="成交买家数",
    conversion="成交转化率",
    favorites="收藏用户数",
    period="入账账期",
    summary_through="账单截至",
    income="收入（元）",
    income_count="收入笔数",
    outcome="支出（元）",
    outcome_count="支出笔数",
    net="收益（元）",
    trade_income="交易收入（元）",
    coupon_subsidy="优惠券补贴（元）",
    refund="退款（元）",
    coupon_refund="退款退优惠券（元）",
    tax_amount="报税金额（元）",
    current_month="当月持续更新",
    db_changed="存在待核对差异",
    db_reviewed="差异已确认",
    db_missing="平台缺失账期",
    company_name="主体名称",
    credit_code="统一社会信用代码",
    updated_at="系统读取时间",
    source_time="平台更新时间",
    loaded_count="已读取条数",
    source_total="平台条数",
    complete="数据完整",
    error="错误",
    data_status="数据状态",
    stale="使用上次数据",
    syncing="正在同步",
    category="类别",
    label="项目",
    amount="金额（元）",
    count="笔数",
    note="备注",
    confirmed="已确认",
    field="字段",
    before="原值",
    after="最新值",
)

LABELS.update(
    activity_id="活动 ID",
    activity_name="活动名称",
    activity_type="活动类型",
    activity_description="活动说明",
    activity_quantity="活动数量",
    outer_price_min="外场活动价下限（元）",
    start="排期开始",
    end="排期结束",
    sku_id="SKU ID",
    price="规格报名价（元）",
    event_id="活动场次 ID",
)


def column(n):
    text = ""
    while n:
        n, r = divmod(n - 1, 26)
        text = chr(65 + r) + text
    return text


def clean(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))[:32767]


def workbook(sheets):
    from .export_jobs import target, progress

    destination = target.get()
    stream = destination or io.BytesIO()
    written = 0
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(stream, "w", ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        )
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(sheets) + 1)
            )
            + "</Relationships>",
        )
        for i, (_, rows) in enumerate(sheets, 1):
            sheet = z.open(f"xl/worksheets/sheet{i}.xml", "w")
            sheet.write(
                (
                    f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>'
                ).encode()
            )
            for r, values in enumerate(rows, 1):
                cells = []
                for c, value in enumerate(values, 1):
                    ref = column(c) + str(r)
                    if (
                        isinstance(value, (float, int))
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                        and abs(value) < 1e15
                    ):
                        cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                    else:
                        cells.append(
                            f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(clean("" if value is None else "是" if value is True else "否" if value is False else value))}</t></is></c>'
                        )
                sheet.write((f'<row r="{r}">' + "".join(cells) + "</row>").encode())
                written += 1
                if written % 500 == 0 and progress.get():
                    progress.get()(written)
            sheet.write(b"</sheetData></worksheet>")
            sheet.close()
    if progress.get():
        progress.get()(written)
    return b"" if destination else stream.getvalue()


LABELS.update(
    ad_spend="成交花费（元）",
    total_spend="总花费（元）",
    ad_revenue="推广交易额（元）",
    net_revenue="净交易额（元）",
    settlement_revenue="结算交易额（元）",
    roi="实际投产比",
    net_roi="净实际投产比",
    settlement_roi="结算投产比",
    ad_orders="推广成交笔数",
    net_orders="净成交笔数",
    impressions="曝光量",
    clicks="点击量",
    ctr="点击率（%）",
    cvr="点击成交率（%）",
    cost_per_order="每笔成交花费（元）",
    target_roi="目标投产比",
    budget="日预算平台值（元）",
    budget_text="预算说明",
    promotion_type="推广类型",
    date="统计日期",
    hour="小时",
)

LABELS.update(EXTRA)
from .platform_tax import FIELDS as TAX_FIELDS

LABELS.update({k: v + "（元）" for k, v in TAX_FIELDS.items()})
LABELS.update(tax_basis="报税数据口径")
LABELS.update(
    estimated_claim="预估赔偿（元）",
    warning_at="预警时间",
    logistics_status="物流状态",
    logistics_time="物流更新时间",
    campaign_name="推广计划名称",
    target_roi_label="目标投产比口径",
    shop_visitors="店铺访客数",
    item_visitors="商品访客数",
    shop_views="店铺浏览量",
    item_views="商品浏览量",
    new_visitors="新访客",
    returning_visitors="老访客",
)


def table(rows, kind=""):
    labels = {**LABELS, **OVERRIDES.get(kind, {})}
    # Only documented, human-facing fields are exported; internal keys never become headings.
    keys = [k for k in labels if any(k in r and not isinstance(r[k], (dict, list)) for r in rows)]
    if kind == "invoices":
        from .invoice_fields import EXPORT_FIELDS

        keys = list(EXPORT_FIELDS) + [k for k in keys if k not in EXPORT_FIELDS and k != "thumbnail_url"]

    def values():
        yield [labels[k] for k in keys] if keys else ["无已读取记录"]
        for r in rows:
            yield [shop_label(r) if k == "shop_name" else display(k, r.get(k)) for k in keys]

    from .export_jobs import target

    return values() if target.get() else list(values())


def response(kind, data, conditions):
    rows = data.get("rows") or data.get("metrics", [])
    sheets = [
        ("筛选结果", table(rows, kind)),
        ("数据完整性", table(data.get("sources", []))),
        (
            "查询条件",
            [
                ["条件", "值"],
                ["说明", "导出当前筛选的全部已读取结果，不受分页影响；是否完整请查看数据完整性表。"],
                ["模块", MODULES.get(kind, "数据")],
                ["时区", "北京时间"],
            ]
            + [
                [
                    k,
                    ",".join(map(str, v))
                    if isinstance(v, list)
                    else VALUES["range"].get(v, v)
                    if k == "统计范围"
                    else LABELS.get(v, v)
                    if k == "排序指标"
                    else {"asc": "升序", "desc": "降序"}.get(v, v)
                    if k == "排序方向"
                    else v,
                ]
                for k, v in conditions.items()
                if v not in ("", None)
            ],
        ),
    ]
    if kind in ("ad_products", "ad_reports"):
        daily = [
            {**r, "shop_name": m["shop_name"], "shop_label": shop_label(m)}
            for m in data.get("metrics", [])
            for r in m.get("daily", [])
        ]
        sheets.extend([("推广概况", table(data.get("metrics", []))), ("分日报表", table(daily))])
    if kind == "goodsdata":
        for title, key in [
            ("淘宝流量概况", "summary"),
            ("淘宝流量趋势", "trend"),
            ("淘宝流量来源", "channels"),
            ("淘宝商品流量榜", "items"),
        ]:
            records = []
            for m in data.get("metrics", []):
                traffic = m.get("traffic") or {}
                items = traffic.get(key, [])
                if isinstance(items, dict):
                    items = [items]
                records.extend(
                    {
                        **r,
                        "shop_name": m["shop_name"],
                        "shop_label": shop_label(m),
                        "source_time": traffic.get("source_time"),
                        "note": traffic.get("note"),
                    }
                    for r in items
                )
            if records:
                sheets.append((title, table(records)))
    if kind == "activities":
        schedules = []
        skus = []
        for row in rows:
            base = {
                k: row.get(k)
                for k in (
                    "shop_label",
                    "platform",
                    "shop_name",
                    "id",
                    "goods_id",
                    "name",
                    "activity_id",
                    "activity_name",
                    "status",
                )
            }
            for slot in row.get("schedule", []):
                schedules.append(
                    {
                        **base,
                        **{k: v for k, v in slot.items() if k != "quantity"},
                        "activity_quantity": slot.get("quantity"),
                    }
                )
            for sku in row.get("skus", []):
                skus.append({**base, "sku_id": sku.get("sku_id"), "label": sku.get("name"), "price": sku.get("price")})
        sheets.extend([("活动排期", table(schedules)), ("报名规格", table(skus))])
    if kind == "tax":
        detail = []
        diffs = []
        for row in rows:
            for shop in row.get("shop_breakdown", []):
                comparison = shop.get("db_comparison", {})
                review = comparison.get("review", {})
                detail.append(
                    {
                        **shop,
                        **(shop.get("entity") or {}),
                        "period": row.get("period"),
                        "confirmed": review.get("confirmed", False),
                        "note": review.get("note", ""),
                    }
                )
                for diff in comparison.get("differences", []):
                    diffs.append(
                        {
                            "shop_name": shop.get("shop_name"),
                            "shop_label": shop_label(shop),
                            "period": row.get("period"),
                            "label": diff["label"],
                            "before": diff.get("before") / 100
                            if diff.get("money") and diff.get("before") is not None
                            else diff.get("before"),
                            "after": diff.get("after") / 100
                            if diff.get("money") and diff.get("after") is not None
                            else diff.get("after"),
                        }
                    )
        sheets.extend([("店铺及主体", table(detail)), ("差异明细", table(diffs))])
    if kind.startswith("reconciliation_"):
        details = []
        for row in rows:
            for side, items in (row.get("breakdown") or {}).items():
                for item in items:
                    details.append(
                        {
                            "shop_name": row.get("shop_name"),
                            "shop_label": shop_label(row),
                            "period": row.get("period"),
                            "category": "收入" if side == "income" else "支出",
                            **item,
                        }
                    )
        sheets.append(("收支明细", table(details)))
    return Response(
        workbook(sheets),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(MODULES.get(kind, "数据") + "-筛选结果.xlsx")
        },
    )
