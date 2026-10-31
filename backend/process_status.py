"""Identity captured once by the serving process, never reread after installation."""

import json
import os
import time
import uuid
from pathlib import Path


def capture(root):
    try:
        version = json.loads((root / "collector-version.json").read_text(encoding="utf-8"))["version"]
    except OSError, ValueError, KeyError:
        version = "手动部署"
    return {"pid": os.getpid(), "started_at": time.time(), "boot_id": uuid.uuid4().hex, "version": version}


IDENTITY = capture(Path(__file__).resolve().parents[1])
