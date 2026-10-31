"""Explicit small-order refund policy; isolated from read-only request replay.

Financial intents are durable files, not operational database logs. Once a
submission starts, an ambiguous result is never automatically submitted again.
"""

import asyncio
import json
import logging
import os
import time
import math
import threading
from collections import OrderedDict
from pathlib import Path

from .async_database import financial_file as durable, read as database_read
from .collector_update import read_json, write_json

PRECHECK = "/mercury/mms/afterSales/agreeRefundPreCheck"
SUBMIT = "/mercury/mms/afterSales/agreeRefund"
LIMIT_CENTS = 10000
SAFE_FIELDS = {
    "hasDepotPlatformOrder", "hasDepotPlatformOrderTips", "tipSellerCheckGoods",
    "tipSellerAccountAbnormal", "sellerAccountShortAmount", "isOneStoreMultipleLocationsOrder",
    "sellerAccountAbnormalTips", "hasReturnSceneRisk", "hasReturnSceneRiskTips",
    "returnSceneRiskTipsVO", "returnSceneRiskOrders", "existRepeatTrackingNumber",
    "autoRefundCustomizedGoodsTipsVO", "showAgreeRefundDoubleCheckBox",
}
RISK_FIELDS = {"hasRisk", "riskTips", "riskTypeCode", "repeatReverseTrackingNumberOrderSnList"}
logger = logging.getLogger(__name__)


def eligible(row, expected):
    """Only native orderAmount (integer cents), never the requested refund."""
    return not ineligible_reason(row, expected)


def ineligible_reason(row, expected):
    if (not expected.get('id') or str(row.get('id', '')) != str(expected['id'])
            or not row.get('orderSn') or row.get('orderSn') != expected.get('order_number')):
        return '原生售后单或订单号不匹配'
    # Status 2 and 3 were both observed with the platform-processing title.
    # A status number alone must never authorize a refund.
    if (type(row.get('afterSalesStatus')) is not int or row['afterSalesStatus'] not in (2, 3)
            or row.get('afterSalesTitle') not in ('退款，拼多多处理中', '退货退款，拼多多处理中')):
        return '原生状态不是可核验的平台处理中'
    if 1001 not in (row.get('actions') or []):
        return '平台未提供同意退款操作'
    if type(row.get('orderAmount')) is not int or row['orderAmount'] <= 0:
        return '原生订单金额缺失或格式无效'
    if row['orderAmount'] >= LIMIT_CENTS:
        return '原生订单金额不小于100元，超出自动退款规则'
    if type(row.get('refundAmount')) is not int or not 0 < row['refundAmount'] <= row['orderAmount']:
        return '退款金额无效或超过订单金额'
    if type(row.get('version')) is not int or row['version'] < 0:
        return '原生版本号缺失或无效'
    return ''


def precheck_safe(envelope):
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        return False
    result = envelope.get("result")
    if not isinstance(result, dict) or not SAFE_FIELDS.issuperset(result):
        return False
    # These fields were observed on the native confirmation page. Missing core
    # checks or any nonempty warning requires manual handling, not attestation.
    required = {"tipSellerCheckGoods", "tipSellerAccountAbnormal", "returnSceneRiskTipsVO"}
    if not required.issubset(result) or any(result[k] is not False for k in ("tipSellerCheckGoods", "tipSellerAccountAbnormal")):
        return False
    for key, value in result.items():
        if key == "returnSceneRiskTipsVO":
            if not isinstance(value, dict) or value.get("hasRisk") is not False or not RISK_FIELDS.issuperset(value):
                return False
            if any(value.values()):
                return False
        elif value:
            return False
    return True


def precheck_reason(envelope):
    """Explain a failed safety check without exposing the full platform response."""
    if not isinstance(envelope, dict) or envelope.get('success') is not True:
        return '平台退款前置检查未返回成功确认，未提交退款'
    result = envelope.get('result')
    if not isinstance(result, dict):
        return '平台退款前置检查结构无效，需人工核对，未提交退款'
    if result.get('tipSellerCheckGoods') is True:
        return '平台要求商家先核验退回商品，需人工确认，未提交退款'
    if result.get('tipSellerAccountAbnormal') or result.get('sellerAccountAbnormalTips'):
        return '平台提示商家账户异常，需人工核对，未提交退款'
    if result.get('sellerAccountShortAmount'):
        return '平台提示退款账户余额不足，需人工核对，未提交退款'
    risk = result.get('returnSceneRiskTipsVO')
    if result.get('hasReturnSceneRisk') or (isinstance(risk, dict) and risk.get('hasRisk')):
        return '平台提示退货场景风险，需人工核对，未提交退款'
    return '平台退款前置检查包含未通过或未识别的确认项，需人工核对，未提交退款'


async def request(page, path, body):
    if path not in (PRECHECK, SUBMIT):
        raise ValueError("不支持的退款操作")
    from .request_reuse import owner, check_cooldown, enter_cooldown, rate_limited

    root = owner(page)
    check_cooldown(root, path)
    expression = (
        '(async()=>{if(location.origin!=="https://mms.pinduoduo.com")throw Error("退款页面域名不正确");const r=await fetch(' + json.dumps(path)
        + ',{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify('
        + json.dumps(body) + '),signal:AbortSignal.timeout(12000)});'
        + 'if(r.status===429)return {success:false,errorMsg:"请求频繁"};if(!r.ok)throw Error("退款接口响应异常");return await r.json()})()'
    )
    response = await page.evaluate(expression)
    if isinstance(response, dict) and rate_limited(response.get('errorMsg') or response.get('error_msg')):
        enter_cooldown(root, path)
        raise ValueError('平台退款接口限流，保留记录并在冷却后继续核验')
    return response


def record_path(store, shop_id, case_id):
    if not str(case_id).isdigit() or not str(shop_id).isdigit():
        raise ValueError("售后单标识无效")
    return Path(store.directory) / "refund-actions" / str(shop_id) / (str(case_id) + ".json")


def claim(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        return True
    except FileExistsError:
        return False


OPEN_STATES = ("submitting", "unknown")
VERIFY_AFTER = 120  # seconds after the intent before the platform state is checked
VERIFY_INTERVAL = 600
VERIFY_PER_RUN = 3
OPERATION_SECONDS = 45  # refund work budget, independent of the read budget
PREPARE_SECONDS = 45  # Page preparation must not consume the refund deadline.


class Budget:
    """Deadline for refund work only; reading already finished and was saved."""

    def __init__(self, seconds=OPERATION_SECONDS):
        self.deadline = time.monotonic() + seconds

    def remaining(self):
        return max(0.0, self.deadline - time.monotonic())


def quarantine(path, raw=b""):
    """Keep the damaged bytes for audit and leave a claim marker in place.

    The marker keeps the case claimed, so a damaged intent can never turn into
    a second submission; the case waits for a person to check the platform.
    """
    folder = path.parent / "quarantine"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = folder / f"{path.stem}.{stamp}.{time.time_ns()}.bad"
    target.write_bytes(raw)
    marker = {
        "shop_id": int(path.parent.name) if path.parent.name.isdigit() else path.parent.name,
        "case_id": path.stem,
        "order_number": "",
        "state": "review",
        "reason": "退款记录文件为空或损坏，原文件已隔离，待人工核对平台状态；不会自动重提",
        "quarantined": target.name,
        "created_at": path.stat().st_mtime if path.exists() else time.time(),
        "updated_at": time.time(),
    }
    write_json(path, marker)
    logger.warning("退款记录损坏已隔离 · %s → %s", path.name, target.name)
    return marker


def load(path):
    """Read one intent. Empty/corrupt files are isolated, never treated as absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return {"case_id": path.stem, "state": "review", "created_at": 0,
                "reason": "退款记录暂时无法读取，待人工核对；不会自动重提"}
    try:
        record = json.loads(raw.decode("utf-8-sig"))
        if isinstance(record, dict) and isinstance(record.get("state"), str):
            created = record.get("created_at", path.stat().st_mtime)
            case_id = str(record.get("case_id", path.stem))
            if (case_id == path.stem and type(created) in (int, float)
                    and math.isfinite(created) and created >= 0):
                return {**record, "case_id": case_id, "created_at": created}
    except UnicodeDecodeError, ValueError:
        pass
    try:
        return quarantine(path, raw)
    except OSError:
        # Even if isolation fails, the claim file still exists: report review.
        return {"case_id": path.stem, "state": "review", "reason": "退款记录文件损坏，待人工核对；不会自动重提"}


_parsed = OrderedDict()  # bounded path -> (mtime_ns, size, record)
_parsed_lock = threading.Lock()


def cached_load(path):
    try:
        info = path.stat()
    except OSError:
        return None
    with _parsed_lock:
        entry = _parsed.get(str(path))
        if entry and entry[0] == info.st_mtime_ns and entry[1] == info.st_size:
            _parsed.move_to_end(str(path))
            return entry[2]
    record = load(path)
    try:
        info = path.stat()  # load() may have replaced a damaged file
        with _parsed_lock:
            _parsed[str(path)] = (info.st_mtime_ns, info.st_size, record)
            _parsed.move_to_end(str(path))
            while len(_parsed) > 2048:
                _parsed.popitem(last=False)
    except OSError:
        pass
    return record


def records(store, shop_id, limit=100, before=0, state="", before_case_id=""):
    """Newest first; `before` is the created_at cursor of the previous page."""
    folder = Path(store.directory) / "refund-actions" / str(int(shop_id))
    by_case = {}
    for path in folder.glob("*.json"):
        try:
            record = cached_load(path)
        except OSError:
            continue  # One unreadable file never hides every other record.
        if not record:
            continue
        by_case[record['case_id']] = {**record, 'record_type': 'financial'}
    # Verification receipts are not financial claims. A failed page load must
    # be visible without preventing the next safe verification attempt.
    from .refund_priority import receipt

    for case, decision in receipt(store, shop_id).get('results', {}).items():
        if case in by_case:
            continue  # Financial intents always override verification notices.
        by_case[case] = {
            **decision, 'case_id': case, 'shop_id': int(shop_id),
            'created_at': decision.get('checked_at', 0), 'record_type': 'verification',
        }
    rows = [r for r in by_case.values() if not state or r.get('state') == state]
    rows.sort(key=lambda r: (r.get("created_at", 0), r["case_id"]), reverse=True)
    pending = sum(1 for r in rows if r.get("state") in OPEN_STATES + ("review", "manual"))
    if before or before_case_id:
        rows = [r for r in rows if (r["created_at"], r["case_id"]) < (before, before_case_id)] if before_case_id else [
            r for r in rows if r["created_at"] < before
        ]
    page = rows[:limit]
    return {
        "rows": page,
        "has_more": len(rows) > limit,
        "next_before": page[-1].get("created_at", 0) if len(rows) > limit and page else 0,
        "next_case_id": page[-1]["case_id"] if len(rows) > limit and page else "",
        "pending_review": pending,
    }


async def verify(page, store, shop, path, record):
    """Read-only reconciliation of an ambiguous submission. Never resubmits."""
    from .operations import ENDPOINTS, uncached_read

    now = time.time()
    record = {**record, "verify_attempts": record.get("verify_attempts", 0) + 1, "verified_at": now}
    try:
        result = await uncached_read(
            page,
            ENDPOINTS["aftersales"],
            {
                "id": str(record["case_id"]),
                "orderSn": record["order_number"],
                "pageNumber": 1,
                "pageSize": 10,
            },
        )
    except Exception as error:
        record["verify_error"] = type(error).__name__
        await durable(write_json, path, {**record, "updated_at": time.time()})
        return record
    if not isinstance(result, dict) or not isinstance(result.get("list"), list):
        record["verify_error"] = "InvalidPlatformResponse"
        await durable(write_json, path, {**record, "updated_at": time.time()})
        return record
    rows = [r for r in result["list"] if str(r.get("id")) == str(record["case_id"])
            and r.get("orderSn") == record["order_number"]]
    record.pop("verify_error", None)
    if len(rows) != 1:
        record.update(
            verified=False,
            reason="精确查询未取得唯一售后记录，尚不能确认退款结果；需人工核对，不会自动重提",
        )
    else:
        row = rows[0]
        record.update(platform_title=row.get("afterSalesTitle") or "", platform_status=row.get("afterSalesStatus"))
        expected = {"id": record["case_id"], "order_number": record["order_number"]}
        if row.get("afterSalesTitle") in ("退款成功", "退款，退款成功", "退货退款，退款成功"):
            record.update(state="accepted", verified=True, reason="核对：平台售后记录明确显示退款成功")
        elif eligible(row, expected) and row.get("version") == record.get("version"):
            record.update(
                state="not_applied",
                verified=True,
                reason="核对：平台仍显示待处理，暂未显示提交生效；需人工核对，不会自动重提",
            )
        else:
            record.update(
                state="changed",
                verified=True,
                reason=f"核对：平台状态已变为「{row.get('afterSalesTitle') or '未知'}」，需人工核对；不会自动重提",
            )
    await durable(write_json, path, {**record, "updated_at": time.time()})
    logger.info("退款结果核对 · shop=%s · case=%s · %s", shop["id"], record["case_id"], record["state"])
    return record


async def reconcile(page, store, shop, budget=None):
    """Check a few submitting/unknown intents against the platform, read-only."""
    folder = Path(store.directory) / "refund-actions" / str(int(shop["id"]))
    now = time.time()
    checked = []
    for path in sorted(folder.glob("*.json")):
        if len(checked) >= VERIFY_PER_RUN or (budget and budget.remaining() < 8):
            break
        record = await durable(load, path)
        if not record or record.get("state") not in OPEN_STATES or not record.get("order_number"):
            continue
        if now - record.get("created_at", now) < VERIFY_AFTER:
            continue
        if now - record.get("verified_at", 0) < VERIFY_INTERVAL:
            continue
        checked.append(await verify(page, store, shop, path, record))
    return checked


async def process(page, store, shop, candidates, budget=None, attempted=None, outcomes=None):
    """Run after the aftersales read is saved, with fresh native verification."""
    from .operations import ENDPOINTS, uncached_read

    changed, records = False, []
    for expected in candidates:
        if expected.get("status") != "平台处理中" or expected.get("attention_excluded"):
            continue
        path = record_path(store, shop["id"], expected["id"])
        old = await durable(load, path)
        if old:
            records.append(old)
            continue  # Accepted, failed, interrupted, unknown or damaged: never blind replay.
        if budget and budget.remaining() < 18:
            break  # Next scheduled read retains this demand; no new timer or slot.

        def decision(state, reason, row=None, complete=True):
            value = {'state': state, 'reason': reason, 'checked_at': time.time(),
                     'retry_at': time.time() + (30 if state == 'retry' else 300)}
            if row:
                value.update(platform_status=row.get('afterSalesStatus'), platform_title=row.get('afterSalesTitle'),
                             order_amount_cents=row.get('orderAmount'), refund_amount_cents=row.get('refundAmount'))
            if outcomes is not None:
                outcomes[str(expected['id'])] = value
            if complete and attempted is not None:
                attempted.add(str(expected['id']))
            logger.info('退款核验结果 · shop=%s · case=%s · %s · %s', shop['id'], expected['id'], state, reason)

        try:
            result = await uncached_read(page, ENDPOINTS["aftersales"], {
                "id": str(expected["id"]), "orderSn": expected["order_number"],
                "quickSearchType": 6, "pageNumber": 1, "pageSize": 10,
            })
            if not isinstance(result.get('list'), list):
                raise ValueError('平台未返回有效售后列表')
        except BaseException as error:
            decision('retry', f'原生查询未完成（{type(error).__name__}），未提交退款，将重新核验', complete=False)
            raise
        matches = [r for r in result['list'] if str(r.get('id')) == str(expected['id'])
                   and r.get('orderSn') == expected['order_number']]
        if len(matches) != 1:
            decision('skipped', '精确查询未返回唯一对应售后单，未提交退款')
            continue
        row = matches[0]
        reason = ineligible_reason(row, expected)
        if reason:
            decision('skipped', reason, row)
            continue
        try:
            check = await request(page, PRECHECK, {"items": [{
                "uid": row.get("uid"), "afterSalesId": row["id"], "orderSn": row["orderSn"],
            }]})
        except BaseException as error:
            decision('retry', f'退款前置检查未完成（{type(error).__name__}），未提交退款，将重新核验', row, complete=False)
            raise
        if not precheck_safe(check):
            reason = precheck_reason(check)
            decision('manual', reason, row)
            records.append({"case_id": str(row["id"]), "state": "manual", "reason": reason})
            continue
        record = {
            "shop_id": shop["id"], "shop_name": shop.get("name", ""),
            "case_id": str(row["id"]), "order_number": row["orderSn"],
            "order_amount_cents": row["orderAmount"], "refund_amount_cents": row["refundAmount"],
            "version": row["version"], "state": "submitting", "created_at": time.time(),
            "rule": "平台处理中且订单金额小于100元", "reason": "已记录提交意图，等待平台响应",
        }
        if not await durable(claim, path, record):
            decision('claimed', '已有财务提交意图，禁止重复退款', row)
            continue
        try:
            response = await request(page, SUBMIT, {
                "operateDesc": "商家同意退款", "uid": row.get("uid"),
                "orderSn": row["orderSn"], "id": row["id"], "version": row["version"],
            })
            if (isinstance(response, dict) and response.get("success") is True
                    and response.get("result") is not False and not response.get("errorMsg")):
                record.update(state="accepted", reason="平台已受理同意退款，实际到账以平台为准")
                changed = True
            else:
                record.update(state="rejected", reason="平台未接受操作，需人工核对；不会自动重提")
        except BaseException as error:
            record.update(state="unknown", reason="提交响应未确认，需人工核对；不会自动重提", error_type=type(error).__name__)
            # Persist even when the read budget expires or a user pauses collection.
            await durable(write_json, path, {**record, "updated_at": time.time()})
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                decision(record['state'], record['reason'], row)
                raise
        await durable(write_json, path, {**record, "updated_at": time.time()})
        records.append(record)
        decision(record['state'], record['reason'], row)
        logger.info("自动退款 · shop=%s · case=%s · order=%.2f · refund=%.2f · %s",
                    shop["id"], row["id"], row["orderAmount"] / 100, row["refundAmount"] / 100, record["reason"])
    return changed, records


async def prioritize(session, store, kind, options, progress=None, seconds=OPERATION_SECONDS, direct=False):
    if getattr(store, 'direct_refunds', None) and not direct:
        return set()
    from .request_reuse import owner

    root = owner(session)
    lock = getattr(root, '_refund_operation_lock', None)
    if lock is None:
        root._refund_operation_lock = lock = asyncio.Lock()
    if lock.locked():
        return set()
    async with lock:
        return await _prioritize(session, store, kind, options, progress, seconds)


async def _prioritize(session, store, kind, options, progress=None, seconds=OPERATION_SECONDS):
    """Verify already-durable platform cases before this task's ordinary read."""
    if kind != "aftersales" or not options.get("_attention") or session.shop.get("platform", "pdd") != "pdd":
        return set()
    from .refund_priority import pending, ordered
    from .business_reader import ReadPage
    from .read_page_pool import borrow
    from .read_budget import current
    from contextlib import nullcontext, AsyncExitStack

    try:
        all_candidates = await database_read(pending, store, session.shop['id'])
        candidates = await durable(ordered, store, session.shop['id'], all_candidates)
    except Exception as error:
        logger.warning("插队待办读取延后 · shop=%s · %s", session.shop["id"], type(error).__name__)
        return set()
    if not candidates:
        return set()
    if progress is not None:
        progress["stage"] = "平台处理中直接核验（无需采集排队）" if getattr(session, 'direct_refund_active', False) else "平台处理中售后插队核验（原金额及安全规则不变）"
    checked = set()
    outcomes = {str(row['id']): {
        'state': 'checking', 'reason': '直接核验中，不经过采集队列',
        'checked_at': time.time(), 'retry_at': time.time() + PREPARE_SECONDS + seconds + 30,
    } for row in candidates}
    from .refund_priority import checked as save_checked
    await durable(save_checked, store, session.shop['id'], all_candidates, checked, outcomes)
    reading = current.get()
    started = time.monotonic()
    stage = '准备退款核验页面'
    unfinished = '核验未完成，未提交的订单将重新核验'
    completed_batch = False
    try:
        with reading.operation() if reading else nullcontext():
            async with AsyncExitStack() as pages:
                async with asyncio.timeout(PREPARE_SECONDS):
                    page = await pages.enter_async_context(borrow(session, kind, ReadPage))
                    await page.open(kind)
                stage = '原生核验与退款'
                # The page can be slow to load. Start the operation budget only
                # after it is ready, so preparation cannot cancel a submission.
                async with asyncio.timeout(seconds):
                    submitted, _ = await process(page, store, session.shop, candidates, Budget(seconds), attempted=checked, outcomes=outcomes)
                    completed_batch = True
                    if submitted:
                        from .request_reuse import owner

                        root = owner(session)
                        from .request_reuse import invalidate
                        invalidate(root)
    except asyncio.CancelledError:
        unfinished = '核验已取消，未提交的订单将在恢复后重新核验'
        raise
    except Exception as error:
        detail = str(error)[:160] or type(error).__name__
        unfinished = f'{stage}未完成：{detail}；未提交的订单将重新核验'
        logger.warning("自动退款核验暂缓 · shop=%s · %s", session.shop["id"], type(error).__name__)
    finally:
        for case, outcome in list(outcomes.items()):
            if outcome['state'] == 'checking':
                outcomes[case] = {'state': 'retry',
                                  'reason': '本批未执行到此单，直接继续核验' if completed_batch else unfinished,
                                  'checked_at': time.time(), 'retry_at': time.time() + (0 if completed_batch else 30)}
        if checked or outcomes:
            await durable(save_checked, store, session.shop["id"], all_candidates, checked, outcomes)
        if progress is not None:
            progress["refund_priority_seconds"] = time.monotonic() - started
    return checked


async def apply(session, store, kind, options, payload, seconds=OPERATION_SECONDS, skip_ids=()):
    from .request_reuse import owner

    root = owner(session)
    lock = getattr(root, '_refund_operation_lock', None)
    if lock is None:
        root._refund_operation_lock = lock = asyncio.Lock()
    if lock.locked():
        return payload, False
    async with lock:
        return await _apply(session, store, kind, options, payload, seconds, skip_ids)


async def _apply(session, store, kind, options, payload, seconds=OPERATION_SECONDS, skip_ids=()):
    """Do not turn ordinary list browsing or other platforms into refund triggers.

    Called after the read payload is already saved. Refund work has its own
    budget, so a slow precheck or submission never delays or fails the read.
    Returns (payload, changed); a changed payload must be published again.
    """
    if kind != "aftersales" or not options.get("_attention") or session.shop.get("platform", "pdd") != "pdd":
        return payload, False
    candidates = [r for r in payload.get("rows", []) if r.get("status") == "平台处理中" and str(r.get("id")) not in skip_ids]
    if not payload.get("complete"):
        return payload, False
    from .business_reader import ReadPage
    from .read_page_pool import borrow
    from .request_reuse import fresh, owner
    from .operations import collect_operations

    budget = Budget(seconds)
    changed = False  # True only once a refreshed payload replaced the saved one
    try:
        async with asyncio.timeout(seconds):
            async with borrow(session, kind, ReadPage) as page:
                await page.open(kind)
                submitted, records = (
                    await process(page, store, session.shop, candidates, budget) if candidates and not getattr(store, 'direct_refunds', None) else (False, [])
                )
                await reconcile(page, store, session.shop, budget)
                if submitted:
                    root = owner(session)
                    from .request_reuse import invalidate
                    invalidate(root)
                    with fresh():
                        refreshed = await collect_operations(page, kind, options)
                    if refreshed.get("complete"):
                        payload = {**refreshed, "_sync": payload.get("_sync", {})}
                        changed = True
                if records:
                    payload["auto_refund_actions"] = records
    except Exception as error:
        # A financial check must not discard successfully read business data.
        logger.warning("自动退款核验延后 · shop=%s · %s", session.shop["id"], type(error).__name__)
    return payload, changed
