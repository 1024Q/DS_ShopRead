"""Restart the launcher once when its loaded UI differs from installed code."""

import hashlib
import subprocess
import httpx
from .collector_update import read_json, wait_ready, write_json
from .collector_runtime import executable

UI_FILES = (
    "collector-launcher.py",
    "backend/collector_launcher.py",
    "backend/collector_update.py",
    "backend/collector_logs.py",
    "backend/collector_overview.py",
    "backend/collector_concurrency.py",
    "backend/collector_restart.py",
    "backend/collector_desktop.py",
    "backend/collector_qt.py",
    "backend/collector_timing.py",
    "backend/log_summary.py",
)


def fingerprint(root):
    digest = hashlib.sha256()
    for name in UI_FILES:
        digest.update(name.encode())
        path = root / name
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
    return digest.hexdigest()


def needed(root, loaded):
    if (root / ".data/updates/journal.json").exists():
        return False
    version = read_json(root / "collector-version.json", {}).get("version")
    state = read_json(root / ".data/updates/status.json", {})
    return bool(
        version
        and state.get("version") == version
        and state.get("updated")
        and state.get("ui_restart_version") != version
        and fingerprint(root) != loaded
    )


def prepare(runner):
    root = runner.root
    state = root / ".data/updates/status.json"
    executable(root, True)
    saved = read_json(state, {})
    installed = read_json(root / "collector-version.json", {})
    headers = {"Authorization": "Bearer " + (root / ".data/api.key").read_text().strip()}
    ready = False
    try:
        ready = wait_ready(runner, headers, state, installed, installed.get("version", ""))
        if not ready:
            raise ValueError("界面更新已安装，等待当前操作结束后自动重启界面")
    finally:
        write_json(
            state,
            {
                **saved,
                **({"ui_restart_version": installed.get("version")} if ready else {}),
                "message": "界面更新完成，正在自动重启" if ready else "界面更新已安装，等待下次自动重启检查",
            },
        )
        if not ready:
            try:
                httpx.post(
                    f"http://127.0.0.1:{runner.port}/api/system/update-cancel",
                    headers=headers,
                    timeout=5,
                    trust_env=False,
                )
            except httpx.HTTPError:
                pass


def launch(root, minimized=False):
    args = [str(executable(root, True)), str(root / "collector-launcher.py"), "--auto"]
    if minimized:
        args.append("--minimized")
    return subprocess.Popen(
        args,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
