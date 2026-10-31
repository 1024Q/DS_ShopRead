import asyncio, base64, json, sys
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from . import invoice_import_store as ledger


async def parse(content):
    # Isolate hostile or malformed PDF parsers; no unbounded work on the API event loop.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "backend.invoice_pdf_cli",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(content), 25)
        return json.loads(stdout)
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    except Exception:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        return {"error": "PDF 解析超时或文件损坏"}


def create_router(store, authorize):
    router = APIRouter(prefix="/api/invoice-imports", dependencies=[Depends(authorize)])

    @router.get("")
    def recent():
        with store.connect() as db:
            rows = db.execute(
                "SELECT b.id,b.created,count(f.id) count FROM invoice_import_batches b LEFT JOIN invoice_import_files f ON f.batch_id=b.id GROUP BY b.id ORDER BY b.created DESC LIMIT 20"
            ).fetchall()
        return {"batches": [dict(r) for r in rows]}

    @router.post("")
    def create():
        return ledger.batch(store, ledger.create_batch(store))

    @router.get("/{bid}")
    def get(bid: str):
        try:
            return ledger.batch(store, bid)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @router.post("/{bid}/files")
    async def add(bid: str, request: Request):
        from .file_upload import read_body, multipart

        raw = await read_body(request, 7_100_000)
        try:
            content_type = request.headers.get("content-type", "application/json")
            if content_type.startswith("multipart/form-data"):
                name, content, fields = multipart(content_type, raw, 5 * 1024 * 1024)
                if fields:
                    raise ValueError("上传字段无效")
            else:
                body = json.loads(raw)
                name = str(body["name"])
                content = base64.b64decode(body["content"], validate=True)
            if not name.lower().endswith(".pdf") or not 0 < len(content) <= 5 * 1024 * 1024:
                raise ValueError("仅支持不超过 5 MB 的 PDF")
            result = await parse(content)
            return await asyncio.to_thread(
                ledger.add_file, store, bid, name, content, result.get("parsed", {}), result.get("error", "")
            )
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))

    @router.post("/{bid}/confirm")
    def confirm(bid: str, body: dict):
        try:
            if body.get("confirmed") is not True:
                raise ValueError("请确认匹配结果后再上传")
            return ledger.confirm(store, bid, body.get("items", []))
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(409, str(e))

    @router.post("/{bid}/files/{fid}/match")
    def match(bid: str, fid: str, body: dict):
        try:
            if ledger.get_file(store, fid)["batch_id"] != bid:
                raise ValueError("文件不属于此批次")
            return ledger.match(store, fid, body.get("selection"))
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(409, str(e))

    @router.get("/{bid}/files/{fid}/pdf")
    def pdf(bid: str, fid: str):
        r = ledger.get_file(store, fid, True)
        if r["batch_id"] != bid:
            raise HTTPException(404, "文件不存在")
        return Response(
            r["content"],
            media_type="application/pdf",
            headers={
                "Content-Disposition": 'inline; filename="invoice.pdf"',
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.post("/{bid}/files/{fid}/cancel")
    def cancel(bid: str, fid: str):
        r = ledger.get_file(store, fid)
        if r["batch_id"] != bid or r["state"] not in ("ready", "exception"):
            raise HTTPException(409, "仅可移除尚未确认的文件")
        with store.connect() as db:
            changed = db.execute(
                "UPDATE invoice_import_files SET state='cancelled',revision=revision+1 WHERE id=? AND state IN ('ready','exception') AND revision=?",
                (fid, r["revision"]),
            ).rowcount
            if changed != 1:
                raise HTTPException(409, "文件状态已变化")
        return {"ok": True}

    return router
