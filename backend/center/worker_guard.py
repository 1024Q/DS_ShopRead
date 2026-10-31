"""In-memory authenticated tunnels require one center worker."""

import os
import sys


def require_single_worker():
    values = [os.environ.get("WEB_CONCURRENCY", "1"), os.environ.get("UVICORN_WORKERS", "1")]
    for i, value in enumerate(sys.argv):
        if value == "--workers" and i + 1 < len(sys.argv):
            values.append(sys.argv[i + 1])
        elif value.startswith("--workers="):
            values.append(value.partition("=")[2])
    if any(int(value) != 1 for value in values):
        raise RuntimeError("中心隧道仅支持单 worker，请移除 --workers 配置")
