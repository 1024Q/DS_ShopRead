"""Signed code-only updates. Runtime, credentials and browser profiles are never replaced."""

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time
import threading
import uuid
import zipfile
import tempfile
import httpx
from cryptography.hazmat.primitives import serialization

MAX_ZIP = 32 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


_json_locks = {}
_json_locks_guard = threading.Lock()


def write_json(path, value):
    path = Path(path)
    key = os.path.normcase(str(path.resolve()))
    with _json_locks_guard:
        lock = _json_locks.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            # Each writer/process owns its temp file. Close it before Windows rename.
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
            ) as stream:
                temp = Path(stream.name)
                stream.write(canonical(value))
            for attempt in range(5):
                try:
                    temp.replace(path)
                    break
                except OSError as error:
                    if not isinstance(error, PermissionError) and getattr(error, "winerror", None) not in (5, 32, 33):
                        raise
                    if attempt == 4:
                        raise
                    time.sleep(0.05 * 2**attempt)
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass


def allowed(name):
    if not isinstance(name, str) or "\\" in name or ":" in name:
        return False
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(p in ("..", ".") for p in parts) or str(PurePosixPath(name)) != name:
        return False
    return (
        (name.startswith("backend/") and name.endswith(".py") and "tests" not in parts)
        or (name.startswith("config/") and name.endswith(".json"))
        or name
        in (
            "backend/trade_digits.json",
            "backend/trade_glyphs.js",
            "backend/requirements.txt",
            "backend/center/requirements.txt",
            "collector-launcher.py",
            "collector-version.json",
            "scripts/shop-window.ps1",
            "COLLECTOR-QUICKSTART.md",
        )
    )


def destination(root, name):
    if not allowed(name):
        raise ValueError("升级文件路径不允许")
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("升级文件越界")
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise ValueError("升级目录不能包含链接")
    return path


def verify(root, envelope):
    payload = envelope["release"]
    key = serialization.load_pem_public_key((root / "collector-update-public.pem").read_bytes())
    key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(payload))
    version = payload["version"]
    if not isinstance(version, str) or not version.replace("-", "").isalnum() or len(version) > 80:
        raise ValueError("升级版本无效")
    if payload.get("format") != 1 or not isinstance(payload.get("files"), dict) or not payload["files"]:
        raise ValueError("升级清单无效")
    for name, digest in payload["files"].items():
        destination(root, name)
        if len(digest) != 64:
            raise ValueError("升级摘要无效")
    return payload


def unpack(root, archive, release, stage):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != release["sha256"]:
        raise ValueError("升级包校验失败")
    with zipfile.ZipFile(archive) as zipped:
        infos = zipped.infolist()
        if len(infos) != len(release["files"]) or {i.filename for i in infos} != set(release["files"]):
            raise ValueError("升级文件与清单不符")
        if sum(i.file_size for i in infos) > 96 * 1024 * 1024:
            raise ValueError("升级包过大")
        for item in infos:
            destination(root, item.filename)
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("升级包不允许链接")
            content = zipped.read(item)
            if hashlib.sha256(content).hexdigest() != release["files"][item.filename]:
                raise ValueError("升级文件校验失败")
            if item.filename.endswith(".py"):
                compile(content, item.filename, "exec")
            path = stage / item.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def rollback(root):
    directory = root / ".data/updates"
    journal = read_json(directory / "journal.json")
    if not journal:
        return
    backup = directory / journal["backup"]
    if backup.parent != directory or not backup.name.startswith("backup-"):
        raise ValueError("升级恢复路径无效")
    # Merge all new shop writes before restoring a pre-sharding version.
    if not journal["files"].get("backend/shop_storage.py", False):
        from .shop_storage import rollback as rollback_storage

        rollback_storage(root / ".data")
    for name, existed in journal["files"].items():
        target = destination(root, name)
        if existed:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / name, target)
        elif target.exists():
            target.unlink()
    (directory / "journal.json").unlink()


def apply(root, stage, release):
    directory = root / ".data/updates"
    backup = directory / ("backup-" + uuid.uuid4().hex)
    old = read_json(root / "collector-version.json", {})
    names = set(release["files"]) | set(old.get("files", []))
    journal = {}
    for name in sorted(names):
        target = destination(root, name)
        journal[name] = target.exists()
        if target.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    write_json(directory / "journal.json", {"backup": backup.name, "files": journal})
    try:
        for name in sorted(names):
            target = destination(root, name)
            if name in release["files"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + ".update-tmp")
                shutil.copy2(stage / name, temp)
                temp.replace(target)
            elif target.exists():
                target.unlink()
    except Exception:
        rollback(root)
        raise


DRAIN_SECONDS = 90
PROBATION = 300
PROBATION_GRACE = 600
MAX_BACKOFF = 1800
AUTO_SETTLE = 180
AUTO_COOLDOWN = 600


def automatic_ready(state, old, release, now=None):
    """Wait for a stable release, and collapse quick consecutive publishes."""
    now = time.time() if now is None else now
    saved = read_json(state, {})
    version = release["version"]
    if saved.get("pending_version") != version:
        saved.update(pending_version=version, pending_since=now)
    remaining = max(0, saved["pending_since"] + AUTO_SETTLE - now, saved.get("updated", 0) + AUTO_COOLDOWN - now)
    if remaining:
        write_json(
            state,
            {
                **saved,
                "version": old.get("version"),
                "available": version,
                "message": f"发现新版本，等待发布稳定后自动更新（约 {int(remaining) + 1} 秒），当前采集继续运行",
            },
        )
        return False
    return True


def collector_requirements(root):
    path = root / "backend/requirements.txt"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def compatible(root, old, release):
    # `requirements` is the legacy combined hash deployed collectors compare; newer releases add a collector-only hash.
    if old.get("requirements") and old.get("requirements") == release.get("requirements"):
        return True
    expected = release.get("collector_requirements")
    return bool(expected) and expected == (old.get("collector_requirements") or collector_requirements(root))


def prune(directory):
    """Drop download folders and every backup except the one rollback still needs (or the newest)."""
    if not directory.is_dir():
        return
    journal = read_json(directory / "journal.json")
    backups = sorted((p for p in directory.glob("backup-*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
    keep = journal["backup"] if journal else (backups[-1].name if backups else None)
    for path in [*directory.glob("download-*"), *backups]:
        if path.is_dir() and path.name != keep:
            shutil.rmtree(path, ignore_errors=True)


def retry_delay(root):
    try:
        return max(60, min(MAX_BACKOFF, int(read_json(root / ".data/updates/status.json", {}).get("retry_after", 60))))
    except TypeError, ValueError:
        return 60


def probe(runner, headers):
    """Read blockers without engaging the update gate; None when the service predates the probe."""
    try:
        response = httpx.get(
            f"http://127.0.0.1:{runner.port}/api/system/update-blockers", headers=headers, timeout=5, trust_env=False
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        value = response.json().get("blockers")
    except ValueError, AttributeError:
        return None
    return value if isinstance(value, dict) else None


def wait_ready(runner, headers, state, old, version):
    base = {**read_json(state, {}), "version": old.get("version"), "available": version}
    remote = "远程控制进行中，暂不暂停采集；远控结束后自动重试升级"
    blockers = probe(runner, headers)
    if blockers and blockers.get("remote_controls"):
        write_json(state, {**base, "message": remote, "phase": "waiting", "blockers": blockers})
        return False
    deadline = time.monotonic() + DRAIN_SECONDS
    while True:
        response = httpx.post(
            f"http://127.0.0.1:{runner.port}/api/system/update-prepare", headers=headers, timeout=8, trust_env=False
        )
        if response.status_code != 409:
            response.raise_for_status()
            return True
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        draining = isinstance(detail, dict) and detail.get("phase") == "draining"
        blockers = detail.get("blockers", {}) if draining and isinstance(detail.get("blockers"), dict) else {}
        if blockers.get("remote_controls"):
            write_json(state, {**base, "message": remote, "phase": "waiting", "blockers": blockers})
            return False  # Caller releases the gate at once.
        message = "已暂停派发新任务，等待当前操作结束后升级" if draining else "旧版采集服务仍在执行任务，等待空闲后升级"
        write_json(
            state, {**base, "message": message, "phase": "draining" if draining else "waiting", "blockers": blockers}
        )
        if not draining:
            return False  # Older service does not understand the lease.
        if time.monotonic() + 2 >= deadline:
            break
        time.sleep(2)
    write_json(state, {**base, "message": "当前操作长时间未结束，本轮升级已让出；请结束远控或检查任务，稍后自动重试"})
    return False


def start_service(runner, config, attempts=3, delay=5):
    for attempt in range(attempts):
        try:
            runner.start(config)
            return
        except Exception:
            runner.stop()
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def revert(runner, config, version, message):
    """Restore the previous code and bring the service back; never leave it silently stopped."""
    root = runner.root
    state = root / ".data/updates/status.json"
    runner.stop()
    try:
        rollback(root)
        start_service(runner, config)
    except Exception:
        hard = "新版已停止，但回退或重新启动采集服务失败；请查看 collector.log，必要时点击“保存并启动”或联系管理员"
        write_json(
            state,
            {
                "version": read_json(root / "collector-version.json", {}).get("version"),
                "failed_version": version,
                "stopped": True,
                "message": hard,
            },
        )
        raise ValueError(hard) from None
    write_json(
        state,
        {
            "version": read_json(root / "collector-version.json", {}).get("version"),
            "failed_version": version,
            "message": message,
        },
    )
    return message


def recover(runner):
    """Retry starting a service that an earlier rollback left stopped."""
    state = runner.root / ".data/updates/status.json"
    saved = read_json(state, {})
    if not saved.get("stopped") or runner.running or not runner.config:
        return None
    start_service(runner, runner.config)
    saved.pop("stopped", None)
    saved["message"] = "采集服务已重新启动（旧版本）"
    write_json(state, saved)
    return saved["message"]


def startup(root):
    """Launcher start: an unfinished install or probation is always rolled back before anything runs."""
    directory = root / ".data/updates"
    state = directory / "status.json"
    saved = read_json(state, {})
    if not (directory / "journal.json").exists():
        prune(directory)
        return
    handoff = directory / "handoff.json"
    transfer = read_json(handoff, {})
    if (
        transfer.get("version") == read_json(root / "collector-version.json", {}).get("version")
        and transfer.get("expires", 0) > time.time()
    ):
        handoff.unlink(missing_ok=True)
        write_json(state, {**saved, "resume_probation": True})
        return
    handoff.unlink(missing_ok=True)
    rollback(root)
    prune(directory)
    if isinstance(saved.get("probation"), dict):
        write_json(
            state,
            {
                "version": read_json(root / "collector-version.json", {}).get("version"),
                "failed_version": saved["probation"].get("version"),
                "message": "新版试运行期间控制面板被关闭或重启，已回退；可点击检查更新重试",
            },
        )


def healthy(runner):
    runner.health_issue = ""
    if not runner.running:
        runner.health_issue = "采集服务未运行"
        return False
    for kind, child in list(runner.children.items()):
        code = child.poll()
        if code is not None:
            runner.health_issue = f"{kind} 进程退出，退出码 {code}"
            return False
    try:
        response = httpx.get(f"http://127.0.0.1:{runner.port}/health", timeout=8, trust_env=False)
        if not response.is_success:
            runner.health_issue = f"健康检查返回 HTTP {response.status_code}"
        return response.is_success
    except httpx.TimeoutException:
        runner.health_issue = "健康检查响应超时（8 秒），进程仍在运行"
    except httpx.HTTPError as error:
        runner.health_issue = f"健康检查连接失败（{type(error).__name__}）"
    return False


def probation(runner, center=None, observed=0):
    """Keep the rollback journal until the new version stays healthy and, when known, reconnects to the center."""
    root = runner.root
    directory = root / ".data/updates"
    state = directory / "status.json"
    saved = read_json(state, {})
    trial = saved.get("probation")
    if not (directory / "journal.json").exists() or not isinstance(trial, dict):
        return None
    now = time.time()
    version = trial.get("version")
    if center is not None and observed >= trial.get("started", 0):
        trial["center"] = bool(center)
        if center:
            trial["connected"] = True
    crashed = runner.restarts > trial.get("restarts", 0)
    ok = not crashed and healthy(runner)
    trial["failures"] = 0 if ok else trial.get("failures", 0) + 1
    reason = getattr(runner, "health_issue", "")
    reason = reason if isinstance(reason, str) and reason else "健康检查失败"
    exited = any(child.poll() is not None for child in runner.children.values())
    if ok:
        trial.pop("unhealthy_since", None)
    else:
        trial.setdefault("unhealthy_since", now)
    trial["health_issue"] = "" if ok else reason
    if crashed or exited or (trial["failures"] >= 3 and now - trial.get("unhealthy_since", now) >= 90):
        reason = "试运行期间进程发生自动重启" if crashed else reason
        return revert(
            runner, runner.config, version, f"新版试运行失败：{reason}；已自动回退，旧版继续运行；可点击检查更新重试"
        )
    if ok and now >= trial.get("until", 0):
        if trial.get("connected") or trial.get("center") is not False:
            (directory / "journal.json").unlink(missing_ok=True)
            prune(directory)
            write_json(
                state,
                {
                    "version": version,
                    "message": "自动升级成功",
                    "updated": now,
                    **({"ui_restart_version": version} if saved.get("ui_restart_version") == version else {}),
                },
            )
            return "自动升级成功 · " + version
        if now >= trial["until"] + PROBATION_GRACE:
            return revert(runner, runner.config, version, "新版未能连接中心，已自动回退；可点击检查更新重试")
    write_json(state, {**saved, "probation": trial})
    return "新版试运行中，确认稳定后完成升级"


_update_lock = threading.Lock()


def update(runner, force=False):
    if not _update_lock.acquire(blocking=False):
        return None
    try:
        return _update(runner, force)
    finally:
        _update_lock.release()


def resume_probation(runner):
    state = runner.root / ".data/updates/status.json"
    saved = read_json(state, {})
    if not saved.pop("resume_probation", False):
        return
    now = time.time()
    trial = saved.get("probation", {})
    trial.update(started=now, until=now + PROBATION, restarts=runner.restarts, failures=0)
    write_json(state, {**saved, "probation": trial})


def _update(runner, force=False):
    root = runner.root
    directory = root / ".data/updates"
    state = directory / "status.json"
    config = runner.config
    headers = {"Authorization": "Bearer " + config["token"], "X-Node-ID": config["id"]}
    old = read_json(root / "collector-version.json", {})
    version = ""
    local_headers = None
    work = None
    phase = "检查更新版本"
    if (directory / "journal.json").exists():
        return read_json(state, {}).get("message") or "新版试运行中"
    prune(directory)
    try:
        with httpx.Client(
            base_url=config["url"], headers=headers, timeout=20, trust_env=False, follow_redirects=False
        ) as client:
            response = client.get("/agent/update/latest")
            if response.status_code in (204, 404):
                return "中心暂无采集端更新"
            response.raise_for_status()
            release = verify(root, response.json())
            version = release["version"]
            if version == old.get("version"):
                return "采集端已是最新版本 · " + version if force else None
            if old.get("version") and version < old["version"]:
                return "中心版本早于当前版本，已跳过降级"
            if not compatible(root, old, release):
                raise ValueError("此版本需要更新运行环境，已保留当前版本，请联系中心管理员")
            previous = read_json(state, {})
            if previous.get("failed_version") == version and not force:
                return None
            if not force and not automatic_ready(state, old, release):
                return None
            deferrals = previous.get("deferrals", 0) if previous.get("available") == version and not force else 0
            phase = "下载更新包"
            work = directory / ("download-" + version)
            work.mkdir(parents=True, exist_ok=True)
            archive = work / "code.zip"
            size = 0
            with client.stream("GET", "/agent/update/packages/" + version) as result:
                result.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in result.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ZIP:
                            raise ValueError("升级包过大")
                        output.write(chunk)
            stage = work / "stage"
            unpack(root, archive, release, stage)
        local_headers = {"Authorization": "Bearer " + (root / ".data/api.key").read_text().strip()}
        phase = "等待采集任务收尾"
        if not wait_ready(runner, local_headers, state, old, version):
            saved = read_json(state, {})
            saved.update(deferrals=deferrals + 1, retry_after=min(MAX_BACKOFF, 60 * 2 ** min(deferrals, 5)))
            write_json(state, saved)
            return saved["message"]
        write_json(state, {"version": old.get("version"), "available": version, "message": "正在安装更新"})
        runner.stop()
        try:
            apply(root, stage, release)
            if getattr(runner, "ui_handoff", False) is True:
                started = time.time()
                write_json(
                    state,
                    {
                        "version": version,
                        "ui_restart_version": version,
                        "message": "更新已安装，正在重启控制台并验证",
                        "probation": {
                            "version": version,
                            "started": started,
                            "until": started + PROBATION,
                            "restarts": 0,
                            "failures": 0,
                        },
                    },
                )
                write_json(directory / "handoff.json", {"version": version, "expires": started + 180})
                runner.restart_requested = True
                return "更新已安装，正在一次性重启控制台和采集服务"
            runner.start(config)
        except Exception:
            return revert(runner, config, version, "新版启动失败，已自动回退；可点击检查更新重试")
        started = time.time()
        write_json(
            state,
            {
                "version": version,
                "message": "新版试运行中，确认稳定后完成升级",
                "probation": {
                    "version": version,
                    "started": started,
                    "until": started + PROBATION,
                    "restarts": runner.restarts,
                    "failures": 0,
                },
            },
        )
        return "新版已启动，试运行中 · " + version
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else f"{phase}超时，保留当前版本，稍后重试"
            if isinstance(error, httpx.TimeoutException)
            else f"{phase}连接失败（{type(error).__name__}），保留当前版本，稍后重试"
            if isinstance(error, httpx.HTTPError)
            else f"更新失败（{type(error).__name__}），保留当前版本，请查看日志"
        )
        if read_json(state, {}).get("stopped") and not runner.running:
            raise  # Service is down: surface as a hard error, keep the recovery marker.
        write_json(state, {**read_json(state, {}), "version": old.get("version"), "message": message})
        return message
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        if local_headers:
            try:
                httpx.post(
                    f"http://127.0.0.1:{runner.port}/api/system/update-cancel",
                    headers=local_headers,
                    timeout=5,
                    trust_env=False,
                )
            except httpx.HTTPError:
                pass  # A crashed updater's lease also expires automatically.
