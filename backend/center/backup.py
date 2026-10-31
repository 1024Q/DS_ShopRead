"""PostgreSQL backups never call the collector's SQLite maintenance endpoints."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from datetime import datetime
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select, update
from .database import settings
from ..business_reader import BEIJING


def retention():
    try:
        return min(365, max(1, int(os.environ.get("ECMP_CENTER_BACKUP_KEEP", "14"))))
    except ValueError:
        return 14


def install(app, database, admin):
    folder = Path(os.environ.get("ECMP_CENTER_BACKUP_DIR", ".data/center/backups"))
    running = False
    worker = None

    def configuration():
        with database.engine.connect() as db:
            value = db.execute(select(settings.c.value).where(settings.c.key == "backup")).scalar()
        return {"enabled": False, "time": "03:30", **(value or {})}

    def save(value):
        with database.engine.begin() as db:
            if db.execute(select(settings.c.key).where(settings.c.key == "backup")).first():
                db.execute(update(settings).where(settings.c.key == "backup").values(value=value))
            else:
                db.execute(settings.insert().values(key="backup", value=value))

    def executable(name):
        found = shutil.which(name)
        if not found:
            found = next(
                (
                    str(p)
                    for p in Path(os.environ.get("ProgramFiles", "C:/Program Files")).glob(
                        f"PostgreSQL/*/bin/{name}.exe"
                    )
                ),
                None,
            )
        if not found:
            raise RuntimeError(f"中心未安装 {name}，请安装 PostgreSQL Command Line Tools")
        return found

    def listing():
        result = []
        if folder.exists():
            for path in folder.glob("*.json"):
                try:
                    result.append(json.loads(path.read_text(encoding="utf-8")))
                except ValueError, OSError:
                    pass
        return sorted(result, key=lambda b: b["created_at"], reverse=True)

    def prune(keep):
        """Keep the newest verified archives; files not following the backup naming scheme are never touched."""
        removed = []
        for record in listing()[max(1, int(keep)) :]:
            name = record.get("name", "")
            if not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{6}\.dump", name):
                continue
            for path in (folder / name, (folder / name).with_suffix(".json")):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            removed.append(name)
        return removed

    def dump():
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex() + ".dump"
        path = folder / name
        url = database.engine.url
        env = {**os.environ, "PGPASSWORD": url.password or ""}
        arguments = [
            executable("pg_dump"),
            "-h",
            url.host or "localhost",
            "-p",
            str(url.port or 5432),
            "-U",
            url.username,
            "-d",
            url.database,
            "-Fc",
            "--no-password",
            "-f",
            str(path),
        ]
        result = subprocess.run(
            arguments,
            env=env,
            capture_output=True,
            timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError("PostgreSQL 备份失败，请检查数据库连接与磁盘空间")
        check = subprocess.run(
            [executable("pg_restore"), "--list", str(path)],
            capture_output=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if check.returncode:
            raise RuntimeError("备份目录校验失败")
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        record = {
            "name": name,
            "created_at": time.time(),
            "size": path.stat().st_size,
            "sha256": digest,
            "verified": True,
            "verification": "目录读取及 SHA-256 校验；未执行恢复演练",
        }
        path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        prune(configuration().get("keep") or retention())
        return record

    @app.get("/api/system/maintenance", dependencies=[Depends(admin)])
    def maintenance():
        return {
            "engine": "postgresql",
            "running": running,
            "backups": listing(),
            "settings": {"keep": retention(), **configuration()},
            "automatic": configuration()["enabled"],
            "deletion_enabled": False,
            "error": configuration().get("last_error", ""),
        }

    @app.put("/api/system/maintenance", dependencies=[Depends(admin)])
    async def configure(request: Request):
        body = await request.json()
        at = body.get("time", "03:30")
        if (
            not isinstance(at, str)
            or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at)
            or type(body.get("enabled")) is not bool
        ):
            raise HTTPException(400, "备份时间或开关无效")
        keep = body.get("keep")
        if keep is not None and (type(keep) is not int or not 1 <= keep <= 365):
            raise HTTPException(400, "备份保留份数需在 1 到 365 之间")
        value = {
            **configuration(),
            "enabled": body["enabled"],
            "time": at,
            **({"keep": keep} if keep is not None else {}),
        }
        save(value)
        return {"settings": {"keep": retention(), **value}}

    @app.post("/api/system/backup", dependencies=[Depends(admin)])
    async def backup():
        nonlocal running
        if running:
            raise HTTPException(409, "中心备份正在执行")
        running = True
        try:
            return await asyncio.to_thread(dump)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            raise HTTPException(
                503, str(error) if isinstance(error, RuntimeError) else "备份执行失败，请检查服务器状态"
            )
        finally:
            running = False

    @app.post("/api/system/backup-verify", dependencies=[Depends(admin)])
    async def verify(name: str):
        if not re.fullmatch(r"\d{8}-\d{6}-[a-f0-9]{6}\.dump", name):
            raise HTTPException(400, "备份文件名无效")
        record = next((r for r in listing() if r["name"] == name), None)
        if not record:
            raise HTTPException(404, "备份不存在")

        def check():
            with (folder / name).open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest() == record["sha256"]

        if not await asyncio.to_thread(check):
            raise HTTPException(409, "备份校验失败，文件内容发生变化")
        return {"engine": "postgresql", "verified": True, "message": "SHA-256 校验通过；未执行恢复演练"}

    async def loop():
        while True:
            config = configuration()
            now = datetime.now(BEIJING)
            if (
                config["enabled"]
                and now.strftime("%H:%M") >= config["time"]
                and config.get("last_day") != now.date().isoformat()
                and time.time() >= config.get("retry_at", 0)
                and not running
            ):
                try:
                    await backup()
                    save({**configuration(), "last_day": now.date().isoformat(), "last_error": "", "retry_at": 0})
                except Exception:
                    save(
                        {
                            **configuration(),
                            "last_error": "自动备份失败，请检查数据库连接和磁盘空间，5 分钟后重试",
                            "retry_at": time.time() + 300,
                        }
                    )
            await asyncio.sleep(30)

    async def start():
        nonlocal worker
        worker = asyncio.create_task(loop())

    async def stop():
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    from contextlib import asynccontextmanager

    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            await start()
            try:
                yield
            finally:
                await stop()

    app.router.lifespan_context = lifespan
