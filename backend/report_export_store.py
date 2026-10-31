"""Durable, file-only pilot export jobs. No order collection subscriptions."""

import json, re, threading, time, uuid, zipfile, shutil
from pathlib import Path
from datetime import datetime, timedelta
from .business_reader import BEIJING

LEGACY_FIELDS = [
    "商品",
    "订单号",
    "订单状态",
    "商品数量(件)",
    "商家编码-规格维度",
    "商家编码-商品维度",
    "商家实收金额(元)",
    "快递单号",
]
FIELDS = ["商品", "商品ID", "商品规格", *LEGACY_FIELDS[1:]]
ACTIVE = {"queued", "querying", "configuring", "submitting", "generating", "downloading", "unpacking"}
MAX_BYTES = 512 * 1024 * 1024


def dates(start, end, now=None):
    try:
        a = datetime.fromisoformat(start)
        b = datetime.fromisoformat(end)
        if a.tzinfo or b.tzinfo:
            raise ValueError()
        a = a.replace(tzinfo=BEIJING)
        b = b.replace(tzinfo=BEIJING)
    except ValueError, TypeError:
        raise ValueError("请选择完整的开始和结束日期时间") from None
    current = now or datetime.now(BEIJING)
    if not a <= b <= current or b - a > timedelta(days=90) or a < current - timedelta(days=90):
        raise ValueError("仅支持近 90 天内的时间范围，结束时间不能晚于现在")
    return a.strftime("%Y-%m-%d %H:%M:%S"), b.strftime("%Y-%m-%d %H:%M:%S")


def filename(name, start, end):
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:70] or "店铺"
    return safe + "_" + re.sub(r"[- :T]", "", start) + "至" + re.sub(r"[- :T]", "", end)


def pack_download(source, target, stem):
    """Preserve the original and extract only bounded spreadsheet members."""
    source = Path(source)
    target = Path(target)
    if not 0 < source.stat().st_size <= MAX_BYTES:
        raise ValueError("报表为空或超过 512 MB 限制")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        iszip = zipfile.is_zipfile(source)
        if iszip:
            with zipfile.ZipFile(source) as z:
                # XLSX itself is a ZIP container, not an archive to unpack.
                if "[Content_Types].xml" in z.namelist():
                    out.write(source, stem + ".xlsx")
                    return
                infos = [i for i in z.infolist() if not i.is_dir()]
                if not infos or len(infos) > 100 or sum(i.file_size for i in infos) > MAX_BYTES:
                    raise ValueError("压缩包文件数量或解压大小超限")
                for info in infos:
                    p = info.filename.replace("\\", "/")
                    if p.startswith("/") or ".." in p.split("/") or ":" in p or info.flag_bits & 1:
                        raise ValueError("压缩包路径异常或需要解压密码，请人工处理")
                    if Path(p).suffix.lower() not in (".xlsx", ".xls", ".csv"):
                        raise ValueError("压缩包含非表格文件，请人工检查")
                out.write(source, "原始文件/" + stem + ".zip")
                for n, info in enumerate(infos, 1):
                    name = stem + (f"_{n}" if len(infos) > 1 else "") + Path(info.filename).suffix.lower()
                    with z.open(info) as src, out.open(name, "w") as dest:
                        shutil.copyfileobj(src, dest, 1024 * 1024)
        else:
            with source.open("rb") as stream:
                header = stream.read(512)
            if b"<html" in header.lower() or b"<!doctype" in header.lower():
                raise ValueError("下载结果是网页，可能登录失效或链接已过期")
            ext = ".xls" if header.startswith(bytes.fromhex("d0cf11e0")) else ".csv"
            out.write(source, stem + ext)


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def path(self, jid):
        if not re.fullmatch("[a-f0-9]{32}", jid):
            raise ValueError("任务编号无效")
        return self.root / jid

    def get(self, jid):
        return json.loads((self.path(jid) / "state.json").read_text(encoding="utf-8"))

    def file_bytes(self, jid):
        folder = self.path(jid)
        if not folder.exists():
            return 0
        total = 0
        try:
            paths = list(folder.iterdir())
        except FileNotFoundError:
            return 0
        for path in paths:
            try:
                if path.is_file() and not path.is_symlink() and path.name not in ("state.json", "state.tmp"):
                    total += path.stat().st_size
            except FileNotFoundError:
                pass  # A download or deletion may finish during this read.
        return total

    def all(self):
        rows = []
        for p in self.root.glob("*/state.json"):
            try:
                rows.append(json.loads(p.read_text(encoding="utf-8")))
            except OSError, ValueError:
                continue
        return sorted(rows, key=lambda r: r["created"], reverse=True)

    def save(self, row):
        folder = self.path(row["id"])
        folder.mkdir(exist_ok=True)
        temp = folder / "state.tmp"
        temp.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        temp.replace(folder / "state.json")
        return row

    def update(self, jid, **values):
        with self.lock:
            return self.save({**self.get(jid), **values, "updated": time.time()})

    def remove(self, jid):
        with self.lock:
            folder = self.path(jid)
            if folder.is_symlink() or folder.resolve().parent != self.root.resolve():
                raise ValueError("报表目录不安全，已停止删除")
            if folder.exists():
                shutil.rmtree(folder)

    def create(self, shop, start, end, jid=None, fields=None, kind="orders"):
        if kind not in ("orders", "promotion"):
            raise ValueError("报表类型无效")
        if kind == "orders":
            start, end = dates(start, end)
        elif (start, end) != ("all", "all"):
            try:
                a = datetime.fromisoformat(start)
                b = datetime.fromisoformat(end)
                expected = datetime(a.year + 1, 1, 1) if a.month == 12 else datetime(a.year, a.month + 1, 1)
                valid = (
                    a == datetime(a.year, a.month, 1)
                    and b == expected
                    and b.replace(tzinfo=BEIJING) <= datetime.now(BEIJING)
                )
            except ValueError, TypeError:
                valid = False
            if not valid:
                raise ValueError("请选择已结束的完整月份")
        fields = list(FIELDS if fields is None else fields)
        if kind == "promotion":
            fields = ["多多推广月账单"]
        if kind == "orders" and fields not in (FIELDS, LEGACY_FIELDS):
            raise ValueError("导出字段版本无效，请更新采集节点")
        with self.lock:
            if jid and (self.path(jid) / "state.json").exists():
                row = self.get(jid)
                if (row["shop_id"], row["start"], row["end"], row.get("kind", "orders")) != (
                    shop["id"],
                    start,
                    end,
                    kind,
                ):
                    raise ValueError("任务编号已用于其他导出范围")
                return row
            for row in self.all():
                if (
                    (row["shop_id"], row["start"], row["end"]) == (shop["id"], start, end)
                    and row.get("fields") == fields
                    and row["state"] in ACTIVE
                ):
                    return row
            now = time.time()
            return self.save(
                {
                    "id": jid or uuid.uuid4().hex,
                    "shop_id": shop["id"],
                    "shop_name": shop.get("name") or shop.get("username") or str(shop["id"]),
                    "kind": kind,
                    "profile_version": shop.get("profile_version", 0),
                    "start": start,
                    "end": end,
                    "fields": fields,
                    "created": now,
                    "updated": now,
                    "state": "queued",
                    "message": "等待执行",
                    "attempts": 0,
                    "retry_at": 0,
                }
            )

    def cleanup(self, days=7):
        with self.lock:
            for row in self.all():
                if row["state"] not in ACTIVE and row["updated"] < time.time() - days * 86400:
                    shutil.rmtree(self.path(row["id"]))
