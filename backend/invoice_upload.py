"""PDD upload adapter: explicit review, fresh preflight, single submission, status readback."""

import logging
import asyncio, base64, json, time
from urllib.parse import urlsplit
from . import invoice_import_store as ledger
from .invoice_pdf import conflicts
from .business_reader import ReadPage, checked_result
from .operations import read, normalize_operation, ENDPOINTS, STATUS


async def application(page, selection):
    for label in STATUS["invoices"]:
        result = await read(
            page,
            ENDPOINTS["invoices"],
            dict(
                invoice_mode_list=None,
                invalid_status="",
                letterhead="",
                order_sn=selection["order_number"],
                serial_no=selection["id"],
                invoice_display_status=STATUS["invoices"].index(label),
                order_status="",
                page_size=10,
                page_no=1,
                after_sales_status="",
                letterhead_type="",
                invoice_type="",
                invoice_kind="",
                invoice_way="",
                subsidy_type=0,
            ),
        )
        items = result.get("list")
        if items is None and result.get("total") == 0:
            continue
        if not isinstance(items, list):
            raise ValueError("平台开票列表结构变化")
        for raw in items:
            if str(raw.get("serial_no")) == selection["id"] and raw.get("order_sn") == selection["order_number"]:
                return raw, normalize_operation("invoices", raw, label)[0]
    raise ValueError("平台中未找到对应开票申请，请重新同步")


async def upload_pdf(page, content):
    # Same order_invoice bucket and file endpoint used by the platform's invoice form.
    encoded = base64.b64encode(content).decode("ascii")
    expression = """(async()=>{
      const r=await fetch('/galerie/business/get_signature',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({bucket_tag:'order_invoice'}),signal:AbortSignal.timeout(20000)});
      const s=await r.json();const signature=s.signature||s.result?.signature;
      if(!r.ok||!signature)throw Error('未取得发票文件上传许可');
      const bytes=Uint8Array.from(atob(ENCODED),c=>c.charCodeAt(0));const form=new FormData();
      form.append('sign',signature);form.append('file',new Blob([bytes],{type:'application/pdf'}),'invoice.pdf');form.append('forbid_override','false');
      const u=await fetch('https://file.pinduoduo.com/general_file',{method:'POST',body:form,signal:AbortSignal.timeout(45000)});
      const data=await u.json();if(!u.ok||!data.url)throw Error('发票文件上传失败');return data.url;
    })()""".replace("ENCODED", json.dumps(encoded))
    url = await page.evaluate(expression)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or not (parts.hostname.endswith(".pddpic.com") or parts.hostname.endswith(".pinduoduo.com"))
    ):
        raise ValueError("平台文件地址异常")
    return url


async def submit(page, selection, parsed, url):
    body = dict(
        invoice_code="",
        invoice_pdf_url=url,
        invoice_kind=0,
        business_type=1 if parsed["title_type"] == "企业" else 0,
        payer_register_no=parsed["buyer_tax"],
        payer_name=parsed["buyer_name"],
        serial_no=selection["id"],
        invoice_number=parsed["invoice_number"],
        order_sn=selection["order_number"],
        paper_shipping_id=None,
        special_kind="1",
        send_certificate_url=[],
    )
    expression = (
        '(async()=>{const r=await fetch("/omaisms/invoice/upload_invoice",{method:"POST",credentials:"include",headers:{"Content-Type":"application/json"},body:JSON.stringify('
        + json.dumps(body)
        + '),signal:AbortSignal.timeout(30000)});if(!r.ok)throw Error("提交返回异常，需核验平台结果");return await r.json();})()'
    )
    checked_result(await page.evaluate(expression))


def publish_verified(store, sid, row):
    from .table_index import write
    from .attention import reconcile

    now = time.time()
    with store.connect() as db:
        snapshots = db.execute(
            "SELECT shop_id,cache_key,payload,updated FROM business_cache WHERE shop_id=? AND cache_key LIKE 'invoices:%'",
            (sid,),
        ).fetchall()
        for old in snapshots:
            payload = json.loads(old["payload"])
            rows = payload.get("rows", [])
            if not any(r["id"] == row["id"] for r in rows):
                continue
            status = payload.get("_sync", {}).get("scope", {}).get("status")
            payload["rows"] = [
                ({**r, **row} if r["id"] == row["id"] else r)
                for r in rows
                if not (r["id"] == row["id"] and status and status != row["status"])
            ]
            if len(payload["rows"]) != len(rows):
                payload["source_total"] = max(0, (payload.get("source_total") or len(rows)) - 1)
            db.execute(
                "UPDATE business_cache SET payload=? WHERE shop_id=? AND cache_key=?",
                (json.dumps(payload, ensure_ascii=False), sid, old["cache_key"]),
            )
            write(db, sid, old["cache_key"], payload, old["updated"])
        store.invalidate_responses(sid, "invoices", db=db)
    reconcile(store, sid, "invoices", {"rows": [row], "complete": True, "_sync": {"snapshot_started_at": now}})


def release_claim(store, fid):
    with store.connect() as db:
        db.execute("DELETE FROM invoice_upload_claims WHERE file_id=?", (fid,))


def recover_interrupted(store):
    with store.connect() as db:
        db.execute(
            "UPDATE invoice_import_files SET state='verify_pending',error='服务重启，先核验平台结果',revision=revision+1 WHERE state IN ('uploading','submitting')"
        )


def due_files(store):
    with store.connect() as db:
        return db.execute(
            "SELECT id,state FROM invoice_import_files WHERE state='queued' OR (state='verify_pending' AND updated<?) ORDER BY CASE state WHEN 'queued' THEN 0 ELSE 1 END,updated LIMIT 10",
            (time.time() - 60,),
        ).fetchall()


class UploadWorker:
    def __init__(self, store, session_getter):
        self.store = store
        self.session_getter = session_getter

    async def process(self, fid, verify_only=False):
        from . import update_gate

        if update_gate.active():
            return
        r = await asyncio.to_thread(ledger.get_file, self.store, fid, True)
        selection = json.loads(r["selected"])
        parsed = json.loads(r["parsed"])
        if r["state"] not in ("queued", "verify_pending", "submitting"):
            return
        item = self.session_getter(selection["shop_id"])
        if update_gate.active():
            return
        if item.lock.locked() or getattr(item, "control_until", 0) > time.time():
            return
        if (
            item.shop.get("platform", "pdd") != "pdd"
            or item.shop.get("profile_version", 0) != selection["profile_version"]
        ):
            await asyncio.to_thread(
                ledger.update,
                self.store,
                fid,
                "verify_pending" if verify_only else "exception",
                "店铺账号发生变化，已停止上传",
            )
            if not verify_only:
                await asyncio.to_thread(release_claim, self.store, fid)
            return
        if item.state != "logged_in":
            if not verify_only:
                await asyncio.to_thread(
                    ledger.update, self.store, fid, "exception", "店铺未登录，请登录后重新匹配并确认"
                )
                await asyncio.to_thread(release_claim, self.store, fid)
            return
        submitted = verify_only or r["state"] in ("submitting", "verify_pending")
        try:
            async with item.lock:
                async with ReadPage(item) as page:
                    await page.open("invoices")
                    raw, current = await application(page, selection)
                    if not submitted:
                        issues = conflicts(
                            parsed, current, await asyncio.to_thread(ledger.entity, self.store, selection["shop_id"])
                        )
                        if raw.get("invoice_kind") != 0 or raw.get("national_subsidy_order"):
                            issues.append("该申请票种或特殊订单暂不支持批量上传")
                        if issues:
                            raise ValueError("；".join(issues))
                        await asyncio.to_thread(ledger.update, self.store, fid, "uploading")
                        url = r["upload_url"] or await upload_pdf(page, r["content"])
                        # Persist intent before any non-idempotent submission. Restart never resubmits.
                        await asyncio.to_thread(ledger.update, self.store, fid, "submitting", upload_url=url)
                        submitted = True
                        await submit(page, selection, parsed, url)
                        await asyncio.to_thread(
                            ledger.update, self.store, fid, "verify_pending", "平台已接收，正在回查开票状态"
                        )
                        for delay in (2, 4, 8):
                            await asyncio.sleep(delay)
                            raw, current = await application(page, selection)
                            if current["status"] == "开票成功":
                                break
                    if current["status"] == "开票成功" and current.get("invoice_number") == parsed["invoice_number"]:
                        await asyncio.to_thread(publish_verified, self.store, selection["shop_id"], current)
                        await asyncio.to_thread(ledger.update, self.store, fid, "success")
                        await asyncio.to_thread(
                            self.store.log_event,
                            "info",
                            "invoices",
                            "pdf_upload_success",
                            "PDF 上传后已核验开票成功",
                            selection["shop_id"],
                        )
                    else:
                        await asyncio.to_thread(
                            ledger.update,
                            self.store,
                            fid,
                            "verify_pending",
                            "平台状态：" + current["status"] + "；等待同一发票号码的成功结果，不会重复提交",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state = "verify_pending" if submitted else "exception"
            message = str(error) if isinstance(error, ValueError) else "连接中断或平台页面未就绪"
            await asyncio.to_thread(
                ledger.update, self.store, fid, state, message + ("；请核验平台结果，不会自动重传" if submitted else "")
            )
            if not submitted:
                await asyncio.to_thread(release_claim, self.store, fid)
            await asyncio.to_thread(
                self.store.log_event, "warning", "invoices", "pdf_upload_check", message[:160], selection["shop_id"]
            )

    async def run(self):
        # uploading only stored a file; conservatively review on interruption as well.
        await asyncio.to_thread(recover_interrupted, self.store)
        while True:
            try:
                rows = await asyncio.to_thread(due_files, self.store)
            except asyncio.CancelledError:
                raise
            except Exception:
                rows = []
            for row in rows:
                try:
                    await self.process(row["id"], row["state"] == "verify_pending")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    try:
                        await asyncio.to_thread(
                            self.store.log_event,
                            "error",
                            "invoices",
                            "pdf_worker_error",
                            "发票上传队列异常，保留任务等待核验",
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logging.getLogger(__name__).warning("发票上传队列异常记录写入失败", exc_info=True)
            await asyncio.sleep(3)
