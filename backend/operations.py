"""Read-only adapters for merchant operations, using observed list endpoints."""

import asyncio
import hashlib
import html
import json
import re
import time
import math
from datetime import datetime
from .business_reader import BEIJING, date_text, checked_result
from .product_images import thumbnail
from .sync_progress import report

KINDS = {"packages", "invoices", "aftersales", "evidence", "intercepts", "navigator", "quality", "brands", "activities"}
STATUS = {
    "packages": ["发货未揽件", "揽件未中转", "中转未派件", "派件未签收", "已签收"],
    "aftersales": ["平台介入待商家处理", "平台处理中", "待商家处理", "待处理即将逾期"],
    "evidence": ["待举证", "超时未举证", "已举证", "已处理"],
    "intercepts": ["拦截中", "拦截失败", "拦截成功", "待拦截", "暂不支持拦截", "已撤销拦截"],
    "invoices": ["待开票", "开票成功", "开票失败", "暂不开票"],
    "quality": ["预警商品", "品质违规商品", "其他商品"],
    "brands": ["无黑标商品"],
}
ENDPOINTS = {
    "packages": "/express_base/new/pack/center/list/get",
    "invoices": "/omaisms/invoice/invoice_list",
    "aftersales": "/mercury/mms/afterSales/queryList",
    "evidence": "/mercury/merchant/proof/list",
    "intercepts": "/cambridge/api/express/intercept/queryInterceptOrderList",
    "navigator": "/sydney/api/goodsNavigator/queryGoodsNavigatorList",
    "quality": "/genji/warlock/mallQualityAnalysis/queryGoodsInfo",
    "brands": "/link/api/blacktag/goods/compare/price/goods/list",
}


def plain(value):
    return html.unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).strip()


def cents(value):
    return round(value / 100, 2) if isinstance(value, (int, float)) else None


def percent(value):
    return round(value * 100, 4) if isinstance(value, (int, float)) else None


def priority(kind, row, platform=None):
    from .priority_rules import KINDS as FOCUS_KINDS, reason

    if kind in FOCUS_KINDS:
        label = reason(kind, row, platform)
        return (3, label) if label else (0, "")
    if (
        kind == "quality"
        and (platform or row.get("platform") or "pdd") == "pdd"
        and row.get("effect_status") == "生效中"
    ):
        return 1, "生效中"
    return 0, ""


def decorate(kind, row, platform=None):
    if kind == "aftersales":
        from .aftersales_warning import actual_status

        row = actual_status(row)
    rank, reason = priority(kind, row, platform)
    if kind == "packages":
        from .package_timing import fields

        row = {**row, **fields(row, platform)}
    return {**row, "priority": rank, "priority_reason": reason}


def order_date(item):
    for key in ("orderTime", "orderCreateTime", "orderCreatedTime"):
        value = item.get(key)
        if not value:
            continue
        try:
            return date_text(value)
        except ValueError, TypeError, OverflowError, OSError:
            try:
                stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                return (stamp.astimezone(BEIJING) if stamp.tzinfo else stamp).strftime("%Y-%m-%d %H:%M:%S")
            except ValueError, TypeError:
                pass
    return ""


def normalize_operation(kind, item, label="", stat_date=""):
    """Return a list: a single order can contain several intercepted parcels."""
    a = item
    row = {
        "id": "",
        "created_at": "",
        "name": a.get("goodsName") or a.get("goods_name") or "",
        "goods_id": str(a.get("goodsId") or a.get("goods_id") or ""),
        "thumbnail_url": thumbnail(a),
        "status": label,
    }
    if kind == "packages":
        row.update(
            id=f"{a['orderSn']}:{a['trackNo']}",
            order_number=str(a["orderSn"]),
            tracking_number=str(a["trackNo"]),
            carrier=a.get("shipName", ""),
            created_at=date_text(a.get("shippingTime")),
            status=a.get("_status_label")
            or {10: "发货未揽件", 20: "揽件未中转", 30: "中转未派件", 40: "派件未签收", 50: "已签收"}.get(
                a.get("packStatus"), f"包裹状态 {a.get('packStatus', '未知')}"
            ),
            problem=a.get("_exception_label", ""),
            progress=a.get("_process_label", ""),
        )
    elif kind == "invoices":
        from .invoice_fields import details

        row.update(details(a))
        row.update(
            id=str(a.get("serial_no") or a["order_sn"]),
            order_number=str(a["order_sn"]),
            name=a.get("goods_name", ""),
            created_at=date_text(a.get("apply_time")),
            deadline=date_text(a.get("promise_invoicing_time")),
            amount=cents(a.get("invoice_amount")),
            invoice_title=a.get("letterhead", ""),
            problem=a.get("reject_reason", ""),
            invoice_number=a.get("invoice_no") or "",
            overdue=bool(a.get("has_exceed")),
        )
    elif kind == "aftersales":
        title = a.get("afterSalesTitle") or ""
        if label == "待处理即将逾期":
            from .aftersales_warning import pending_merchant

            if not pending_merchant(title):
                raise ValueError("售后预警筛选状态不一致：平台返回的订单并非待商家处理，需核对快捷筛选适配")
        row.update(
            id=str(a["id"]),
            order_number=str(a["orderSn"]),
            name=a.get("goodsName", ""),
            status=label or ("平台处理中" if a.get("afterSalesStatus") in (2, 3, 7) else title or "未知"),
            progress=title,
            created_at=date_text(a.get("createdAt")),
            amount=cents(a.get("refundAmount")),
            problem=a.get("afterSalesReasonDesc", ""),
            tracking_number=a.get("orderTrackingNumber") or "",
            return_tracking_number=a.get("reverseTrackingNumber") or "",
            aftersale_type=a.get("afterSalesTypeName", ""),
            remark=a.get("mallRemark") or "",
            deadline=date_text(a.get("merchantProofExpireTime")),
        )
        if label == "待处理即将逾期":
            row.update(status=title, attention_status=label)
    elif kind == "evidence":
        # The same after-sales case can have multiple proof requests at different times.
        row.update(
            id=f"{a['afterSalesId']}:{a.get('createTime', 0)}",
            order_number=str(a["orderSn"]),
            goods_id=str(a.get("goodsId") or ""),
            status={0: "待举证", 1: "超时未举证", 2: "已举证", 3: "已处理"}.get(a.get("status"), "未知"),
            amount=cents(a.get("refundAmount")),
            order_amount=cents(a.get("orderAmount")),
            created_at=date_text(a.get("createTime")),
            deadline=date_text(a.get("expireTime")),
        )
    elif kind == "intercepts":
        rows = []
        for ship in a.get("shipList") or []:
            track = str(ship.get("trackingNumber") or "")
            logistics = ship.get("logisticsVOList") or []
            rows.append(
                decorate(
                    kind,
                    {
                        **row,
                        "id": f"{a['orderSn']}:{ship.get('shippingId')}:{track}",
                        "created_at": order_date(a),
                        "order_number": str(a["orderSn"]),
                        "goods_id": str(a.get("goodsId") or ""),
                        "name": a.get("goodsName", ""),
                        "tracking_number": track,
                        "carrier": ship.get("shippingName", ""),
                        "status": {
                            0: "待拦截",
                            1: "拦截中",
                            2: "拦截失败",
                            3: "拦截成功",
                            4: "暂不支持拦截",
                            5: "已撤销拦截",
                        }.get(ship.get("interceptStatus"), f"状态 {ship.get('interceptStatus', '未知')}"),
                        "progress": logistics[0].get("info", "") if logistics else "",
                        "logistics_time": date_text(logistics[0].get("time")) if logistics else "",
                        "problem": plain(ship.get("cannotInterceptReason") or ship.get("unblockableReason")),
                        "failure_code": ship.get("interceptFailReason") if ship.get("interceptStatus") == 2 else None,
                        "intercept_type": {1: "自动拦截"}.get(
                            ship.get("interceptType"), "平台类型 " + str(ship.get("interceptType", "未知"))
                        ),
                    },
                )
            )
        return rows
    elif kind == "navigator":
        d = a.get("goodsNavigatorDetailDTO") or a.get("noGFoodsNavigatorDetailDTO") or {}
        row.update(
            id=str(a["goodsId"]),
            goods_id=str(a["goodsId"]),
            name=a.get("goodsName", ""),
            status="在售中" if str(a.get("isOnsale")) == "1" else "已下架",
            stat_date=stat_date,
            rating_rank=d.get("goodsAvgAntiDescCtgrpRevScrPct1m"),
            refund_rate=d.get("qltyRfndOrdrCntRto1m"),
            help_rate=d.get("ptHelpPzRate30d"),
            sentiment_score=d.get("midRto30d"),
            rating_score=d.get("avgDescRevScr1m"),
            score_rank=d.get("rankComscoreStage"),
            stock=a.get("quantity"),
        )
    elif kind == "quality":
        negative = a.get("seriousInferiorData") or {}
        bad = a.get("badReviewData") or {}
        # The category and punishment are not the platform's effective status.
        # Only status=1 was verified as 生效中; do not infer it from dates.
        code = a.get("status") if label == "品质违规商品" else None
        row.update(
            id=f"{label}:{a['goodsId']}:{a.get('startTime', '')}",
            goods_id=str(a["goodsId"]),
            name=a.get("goodsName", ""),
            effect_status="生效中" if str(code) == "1" else ("非生效" if code is not None else "未提供"),
            effect_status_code=code,
            problem="；".join(p.get("problem", "") for p in a.get("warningProblems") or [])
            or a.get("problemType")
            or a.get("problems", ""),
            negative_count=negative.get("count"),
            negative_rate=percent(negative.get("ratio")),
            bad_count=bad.get("count"),
            bad_rate=percent(bad.get("ratio")),
            progress=a.get("punishType", ""),
            suggestion=plain(a.get("rectificationMeasure")),
            created_at=a.get("startTime", ""),
            deadline=a.get("endTime", ""),
            stat_date=a.get("indicatorPt") or stat_date,
        )
    elif kind == "brands":
        row.update(
            id=str(a["goodsId"]),
            goods_id=str(a["goodsId"]),
            name=a.get("goodsName", ""),
            status="无黑标商品",
            stock=a.get("inventory"),
            problem="；".join(a.get("nonBlackLabelReasons") or []),
            stat_date=stat_date,
        )
    if not row["id"]:
        raise ValueError("平台返回的记录缺少唯一编号")
    if kind == "packages" and label == "即将发货-揽收超时":
        row["attention_status"] = label
    if kind == "invoices" and label == "3天内需开票":
        row.update(status="待开票", attention_status=label)
    return [decorate(kind, row)]


async def read(session, path, body):
    from .request_reuse import read as reuse

    return await reuse(session, path, body, lambda: uncached_read(session, path, body))


async def uncached_read(session, path, body):
    if path not in ENDPOINTS.values():
        raise ValueError("不支持的数据接口")
    expression = (
        "(async()=>{const r=await fetch("
        + json.dumps(path)
        + ',{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify('
        + json.dumps(body)
        + '),signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error("读取失败");return await r.text();})()'
    )
    result = checked_result(json.loads(await session.evaluate(expression)))
    if not isinstance(result, dict):
        raise ValueError("平台返回结构发生变化，请更新数据适配")
    if result.get("hasPermission") is False:
        raise ValueError("该店铺没有当前模块的查看权限")
    return result


async def paginate(
    session,
    kind,
    body,
    page_key,
    list_key,
    total_key="total",
    label="",
    limit=2000,
    required_ids=None,
    split_on_limit=False,
):
    from . import collection_checkpoint as checkpoint

    title = {
        "packages": "包裹中心",
        "aftersales": "售后工作台",
        "evidence": "商家举证",
        "intercepts": "拦截快递",
        "invoices": "订单开票",
    }.get(kind, kind)
    if checkpoint.current.get():
        original = dict(body)

        async def fetch(number):
            report(session, stage=f"等待{title}列表接口响应 · {label or '列表'} · 第 {number} 页")
            result = await read(session, ENDPOINTS[kind], {**original, page_key: number})
            if split_on_limit and int(result.get(total_key) or 0) > 10000:
                raise ValueError("超过可查询量，自动缩小查询范围")
            if kind == "navigator" and result.get(total_key) is not None:
                original["total"] = int(result[total_key])
            # Invoice categories with no records return an explicit null list and
            # total=0. Missing fields or null with a positive/unknown total are
            # still malformed, not an empty successful snapshot.
            batch = result.get(list_key)
            if (
                kind == "invoices"
                and list_key in result
                and batch is None
                and type(result.get(total_key)) is int
                and result[total_key] == 0
            ):
                batch = []
            if not isinstance(batch, list):
                raise ValueError("列表字段发生变化，请更新数据适配")
            rows = []
            ids = []
            for item in batch:
                normalized = normalize_operation(
                    kind, item, label, result.get("statDate") or result.get("dataTime") or ""
                )
                rows.extend(normalized)
                ids.append(str(item.get("orderSn")) if kind == "intercepts" else "|".join(r["id"] for r in normalized))
            return {
                "rows": rows,
                "ids": ids,
                "total": int(result[total_key]) if result.get(total_key) is not None else None,
                "has_more": result.get("hasMore"),
            }

        key = json.dumps(
            [
                kind,
                label,
                {k: v for k, v in original.items() if k not in (page_key, "total")},
                *([sorted(required_ids)] if required_ids else []),
            ],
            sort_keys=True,
        )
        if kind == "invoices":
            key += ":invoice-details-v2"
        result = await checkpoint.paginate(
            key,
            fetch,
            lambda n, total: report(session, n, total, "分批读取" + (label or "列表")),
            required_ids=required_ids,
        )
        if required_ids:
            result.update(
                complete=False, matched_scope=set(required_ids).issubset({str(r["id"]) for r in result["rows"]})
            )
        result["source_unit"] = "订单" if kind == "intercepts" else "记录"
        return result
    rows, seen, loaded, total = [], set(), 0, None
    complete = False
    for _ in range(220):
        report(session, stage=f"等待{title}列表接口响应 · {label or '列表'} · 第 {body.get(page_key, 1)} 页")
        result = await read(session, ENDPOINTS[kind], body)
        if split_on_limit and int(result.get(total_key) or 0) > 10000:
            raise ValueError("超过可查询量，自动缩小查询范围")
        if list_key not in result:
            raise ValueError("列表字段发生变化，请更新数据适配")
        batch = result[list_key] or []
        if not isinstance(batch, list):
            raise ValueError("平台返回的列表格式不正确")
        reported = result.get(total_key)
        if reported is not None:
            total = int(reported)
        # Mirror the native navigator pager's total field on subsequent requests.
        if kind == "navigator" and total is not None:
            body["total"] = total
        previous = len(seen)
        for item in batch:
            normalized = normalize_operation(kind, item, label, result.get("statDate") or result.get("dataTime") or "")
            identity = str(item.get("orderSn")) if kind == "intercepts" else "|".join(row["id"] for row in normalized)
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            loaded += 1
            rows.extend(normalized)
        report(session, loaded, total, "读取" + (label or "列表") + "分页")
        if result.get("hasMore") is False or (total is not None and loaded >= total):
            complete = True
            break
        if not batch:
            complete = total is None or loaded >= total
            break
        if loaded >= limit or len(seen) == previous:
            break
        body[page_key] += 1
        await asyncio.sleep(0.35)
    if total is None and complete:
        total = loaded
    return {
        "rows": rows,
        "source_total": total,
        "source_unit": "订单" if kind == "intercepts" else "记录",
        "source_loaded": loaded,
        "complete": complete,
        "limit": limit,
    }


async def collect_operations(session, kind, options):
    from .request_reuse import complete

    return await complete(session, kind, options, lambda: _collect_operations(session, kind, options))


async def _collect_operations(session, kind, options):
    start, end = options["start"], options["end"]
    status = options.get("status", "")
    from .collection_checkpoint import current

    checkpoint = current.get()
    now = checkpoint.data["started_at"] if checkpoint else time.time()
    if checkpoint and not options.get("_attention"):
        end = min(end, int(now))
    if options.get("_attention") and kind == "intercepts":
        status = "拦截失败"
    jobs = []
    if kind == "packages":
        body = {
            "packStatus": 0,
            "shipId": 0,
            "orderSn": None,
            "trackNo": None,
            "processStatus": 0,
            "pageNumber": 1,
            "exceptionStatus": 0,
            "electricTrack": 0,
            "queryType": 0,
            "pageSize": 20,
            "shippingTimeStart": start,
            "shippingTimeEnd": end,
        }
        if options.get("_attention"):
            # Native quick filter 4: queryType 3 and shipments 12–24 hours ago.
            body.update(
                packStatus=10,
                exceptionStatus=0,
                queryType=3,
                shippingTimeStart=math.ceil(now - 86400),
                shippingTimeEnd=math.ceil(now - 43200),
            )
        elif status:
            body["packStatus"] = (STATUS[kind].index(status) + 1) * 10
        jobs = [(body, "pageNumber", "orderListDTO", "count", "即将发货-揽收超时" if options.get("_attention") else "")]
    elif kind == "invoices":
        for label in ["待开票"] if options.get("_attention") else [status] if status else STATUS[kind]:
            body = {
                "invoice_mode_list": None,
                "invalid_status": "",
                "letterhead": "",
                "order_sn": "",
                "invoice_display_status": STATUS[kind].index(label),
                "order_status": "",
                "page_size": 10,
                "serial_no": "",
                "after_sales_status": "",
                "letterhead_type": "",
                "page_no": 1,
                "invoice_type": "",
                "invoice_kind": "",
                "invoice_way": "",
                "subsidy_type": 0,
            }
            if options.get("_attention"):
                # Matches the native Within3Days quick filter (now through +3 days).
                body.update(promise_time_begin=int(now), promise_time_end=int(now) + 3 * 86400)
            elif options.get("_exact_dates"):
                body.update(apply_time_start=start, apply_time_end=end)
            jobs.append((body, "page_no", "list", "total", "3天内需开票" if options.get("_attention") else label))
    elif kind == "aftersales":
        queries = {"平台介入待商家处理": 21, "平台处理中": 6, "待商家处理": 7, "待处理即将逾期": 4}
        for label in (
            ["平台处理中", "平台介入待商家处理"]
            if options.get("_attention")
            else [status]
            if status in queries
            else ["平台介入待商家处理", "平台处理中", ""]
        ):
            body = {"pageSize": 10, "pageNumber": 1, "orderByCreatedAtDesc": True}
            if label:
                body["quickSearchType"] = queries[label]
            # Keep urgent cases of any age when no explicit range is selected.
            # Only the ordinary history list defaults to the last seven days.
            if not label or options.get("explicit_dates"):
                body.update(startCreatedTime=start, endCreatedTime=end)
            jobs.append((body, "pageNumber", "list", "total", label))
    elif kind == "evidence":
        for label in ["待举证"] if options.get("_attention") else [status] if status else STATUS[kind]:
            body = {
                "status": STATUS[kind].index(label),
                "createStartTime": start * 1000,
                "createEndTime": end * 1000 + 999,
                "orderSns": [],
                "offset": 1,
                "pageSize": 10,
            }
            jobs.append((body, "offset", "merchantProofs", "total", label))
    elif kind == "intercepts":
        body = {"orderTimeStart": start, "orderTimeEnd": end, "pageNo": 1, "pageSize": 10}
        if status in STATUS[kind][:3]:
            body["interceptStatus"] = STATUS[kind].index(status) + 1
        jobs = [(body, "pageNo", "list", "total", "")]
    elif kind == "navigator":
        jobs = [
            (
                {"total": 0, "pageSize": 10, "pageNo": 1, "goodsIdList": [], "goodsNameList": []},
                "pageNo",
                "goodsList",
                "total",
                "",
            )
        ]
    elif kind == "quality":
        for label in [status] if status else STATUS[kind]:
            body = {"goodsType": STATUS[kind].index(label), "pageNum": 1, "pageSize": 10}
            if label == "品质违规商品":
                # This tab returns a separate unpaginated violation-record collection;
                # its "total" refers to goodsInfoList, which is empty on this tab.
                result = await read(session, ENDPOINTS[kind], body)
                if "goodsPunishRecordVOList" not in result:
                    raise ValueError("未读取到品质违规记录")
                batch = result["goodsPunishRecordVOList"] or []
                jobs.append(
                    {
                        "rows": [row for item in batch for row in normalize_operation(kind, item, label)],
                        "source_total": len(batch),
                        "source_loaded": len(batch),
                        "complete": True,
                    }
                )
            else:
                jobs.append((body, "pageNum", "goodsInfoList", "total", label))
    elif kind == "brands":
        jobs = [
            (
                {"pageNum": 1, "pageSize": 20, "source": "MMS", "queryType": 2},
                "pageNum",
                "highPriceGoodsList",
                "total",
                "无黑标商品",
            )
        ]
    parts = []
    for job in jobs:
        parts.append(job if isinstance(job, dict) else await paginate(session, kind, *job))
    # Priority queries overlap the normal after-sales list; retain the priority label.
    merged = {}
    for part in parts:
        for row in part["rows"]:
            if row["id"] not in merged or row.get("priority", 0) > merged[row["id"]].get("priority", 0):
                merged[row["id"]] = row
    complete = all(p["complete"] for p in parts)
    total = (
        parts[-1]["source_total"]
        if kind == "aftersales" and status not in STATUS["aftersales"]
        else sum(p["source_total"] or p["source_loaded"] for p in parts)
    )
    if kind == "aftersales" and status not in STATUS["aftersales"] and total is not None:
        total += len(set(merged) - {row["id"] for row in parts[-1]["rows"]})
    return {
        "rows": list(merged.values()),
        "source_total": total,
        "source_loaded": sum(p["source_loaded"] for p in parts),
        "complete": complete,
        "limit": 2000,
        "source_unit": "订单" if kind == "intercepts" else "记录",
    }
