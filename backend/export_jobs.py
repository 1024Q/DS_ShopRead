"""Bounded background Excel jobs, authenticated polling and streamed downloads."""

import contextvars, threading, time, uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

target = contextvars.ContextVar("excel_target", default=None)
progress = contextvars.ContextVar("excel_progress", default=None)
RETENTION_SECONDS = 86400


class ExportJobs:
    def __init__(self, directory):
        self.directory = Path(directory) / "exports"
        self.jobs = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="excel-export")
        self.last_cleanup = 0
        self.cleanup()

    def cleanup(self, now=None):
        """Expire finished jobs and their files after RETENTION_SECONDS; orphaned files from earlier runs too."""
        now = time.time() if now is None else now
        self.last_cleanup = now
        cutoff = now - RETENTION_SECONDS
        with self.lock:
            expired = [
                k
                for k, j in self.jobs.items()
                if j["status"] not in ("queued", "running") and j.get("finished", j["created"]) < cutoff
            ]
            for key in expired:
                self.jobs.pop(key, None)
            active = set(self.jobs)
        for key in expired:
            try:
                (self.directory / (key + ".xlsx")).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            files = list(self.directory.glob("*.xlsx"))
        except OSError:
            return
        for path in files:
            try:
                if path.stem not in active and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def maybe_cleanup(self):
        if time.time() - self.last_cleanup > 3600:
            self.cleanup()

    def submit(self, work):
        self.maybe_cleanup()
        with self.lock:
            if sum(j["status"] in ("queued", "running") for j in self.jobs.values()) >= 4:
                raise HTTPException(429, "导出任务较多，请稍后重试")
            key = uuid.uuid4().hex
            job = {"id": key, "status": "queued", "rows_written": 0, "created": time.time()}
            self.jobs[key] = job

        def run():
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (key + ".xlsx")
            a = target.set(path)
            b = progress.set(lambda n: job.update(rows_written=n))
            try:
                job["status"] = "running"
                work()
                if not path.is_file():
                    raise ValueError("没有生成导出文件")
                job["status"] = "complete"
                job["finished"] = time.time()
            except Exception as error:
                job.update(
                    status="error",
                    error=str(getattr(error, "detail", "导出失败，请检查筛选条件后重试")),
                    finished=time.time(),
                )
                path.unlink(missing_ok=True)
            finally:
                target.reset(a)
                progress.reset(b)

        self.pool.submit(contextvars.copy_context().run, run)
        return dict(job)

    def get(self, key):
        self.maybe_cleanup()
        job = self.jobs.get(key)
        if not job:
            raise HTTPException(404, "导出任务不存在或服务已重启，请重新导出")
        return dict(job)

    def router(self, authorize):
        router = APIRouter(dependencies=[Depends(authorize)])

        @router.get("/api/exports/{key}")
        def status(key: str):
            return self.get(key)

        @router.get("/api/exports/{key}/download")
        def download(key: str):
            if self.get(key)["status"] != "complete":
                raise HTTPException(409, "导出尚未完成")
            return FileResponse(
                self.directory / (key + ".xlsx"),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename="筛选结果.xlsx",
            )

        return router
