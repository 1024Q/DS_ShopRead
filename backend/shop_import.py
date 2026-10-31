"""Validate and atomically import CSV/TSV shop credentials without returning secrets."""

import csv
import io

MAX_ROWS = 500
MAX_BYTES = 1024 * 1024
HEADERS = {
    "账号": "username",
    "用户名": "username",
    "username": "username",
    "account": "username",
    "密码": "password",
    "password": "password",
    "店铺名称": "name",
    "店铺显示名称": "name",
    "名称": "name",
    "name": "name",
    "浏览器": "browser",
    "browser": "browser",
    "平台": "platform",
    "platform": "platform",
}
BROWSERS = {
    "system": "system",
    "系统": "system",
    "系统浏览器": "system",
    "系统 Chrome": "system",
    "系统 Google Chrome": "system",
    "bundled": "bundled",
    "项目": "bundled",
    "项目浏览器": "bundled",
    "项目 Chrome": "bundled",
    "项目 Google Chrome": "bundled",
    "项目自带 Google Chrome": "bundled",
}


def parse(content, default_browser="system", default_platform="pdd"):
    from .platforms import PLATFORMS, validate_platform

    validate_platform(default_platform)
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("文件内容不能超过 1 MB")
    if default_browser not in ("system", "bundled"):
        raise ValueError("默认浏览器无效")
    content = content.lstrip("\ufeff")
    first = next((line for line in content.splitlines() if line.strip()), "")
    delimiter = "\t" if "\t" in first else ","
    try:
        records = [
            (number, row)
            for number, row in enumerate(csv.reader(io.StringIO(content), delimiter=delimiter, strict=True), 1)
            if any(cell.strip() for cell in row)
        ]
    except csv.Error:
        raise ValueError("表格格式错误，请检查引号和分隔符") from None
    if not records:
        raise ValueError("请上传文件或粘贴店铺数据")
    fields = [HEADERS.get(cell.strip().lower()) for cell in records[0][1]]
    if "username" in fields and "password" in fields:
        if None in fields or len(set(fields)) != len(fields):
            raise ValueError("表头仅支持账号、密码、店铺名称、浏览器、平台，每列只能出现一次")
        records = records[1:]
    else:
        fields = ["username", "password", "name", "browser", "platform"]
    if not records or len(records) > MAX_ROWS:
        raise ValueError("每次需导入 1 至 500 家店铺")
    from .store import Store

    rows = []
    for line, cells in records:
        error = ""
        if len(cells) > len(fields) or len(cells) < 2:
            error = "列数不正确，请使用账号、密码、店铺名称、浏览器、平台五列（后三列可省略）"
        values = dict(zip(fields, cells))
        username = values.get("username", "").strip()
        password = values.get("password", "")  # Whitespace can be part of a password.
        name = values.get("name", "").strip()
        browser = BROWSERS.get(values.get("browser", "").strip() or default_browser)
        platform = values.get("platform", "").strip() or default_platform
        platform = next((k for k, v in PLATFORMS.items() if v == platform), platform)
        try:
            Store.validate(username, password, browser, name, platform)
        except ValueError as exc:
            error = error or str(exc)
        rows.append(
            {
                "line": line,
                "username": username,
                "password": password,
                "name": name,
                "browser": browser or "",
                "error": error,
                "platform": platform,
            }
        )
    return rows


def import_shops(store, content, default_browser="system", commit=False, default_platform="pdd"):
    parsed = parse(content, default_browser, default_platform)
    with store.connect() as db:
        # Reserve a single transaction so simultaneous imports cannot race or partially save.
        if commit:
            db.execute("BEGIN IMMEDIATE")
        existing = {
            (row["platform"], row["username"].casefold()) for row in db.execute("SELECT platform,username FROM shops")
        }
        seen = set()
        result = []
        pending = []
        for row in parsed:
            key = (row["platform"], row["username"].casefold())
            status = "invalid" if row["error"] else "duplicate" if key in existing or key in seen else "ready"
            message = row["error"] or (
                "账号已存在，跳过" if key in existing else "本次导入中账号重复，跳过" if key in seen else "校验通过"
            )
            result.append(
                {k: row[k] for k in ("line", "username", "name", "browser", "platform")}
                | {"status": status, "message": message}
            )
            if status == "ready":
                pending.append((row, result[-1]))
                seen.add(key)
        invalid = sum(row["status"] == "invalid" for row in result)
        imported = 0
        if commit and not invalid:
            for row, public in pending:
                cursor = db.execute(
                    "INSERT INTO shops(alias,username,password,browser,platform) VALUES(?,?,?,?,?)",
                    (
                        row["name"],
                        row["username"],
                        store.cipher.encrypt(row["password"].encode()),
                        row["browser"],
                        row["platform"],
                    ),
                )
                public.update(status="imported", message="导入成功", shop_id=cursor.lastrowid)
                imported += 1
        return {
            "rows": result,
            "total": len(result),
            "ready": len(pending),
            "invalid": invalid,
            "skipped": sum(r["status"] == "duplicate" for r in result),
            "imported": imported,
            "committed": bool(commit and not invalid),
        }
