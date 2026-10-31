"""Run after validation to publish a signed code update, never a full runtime package."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import argparse, base64, hashlib, json, os, subprocess, zipfile
from datetime import datetime, timezone
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from backend.collector_update import canonical, allowed, write_json

EXTRA = ("collector-launcher.py", "scripts/shop-window.ps1", "COLLECTOR-QUICKSTART.md")
PASSPHRASE = "ECMP_SIGNING_PASSPHRASE"


class PublishError(Exception):
    pass


def load_key(private, init_key=False):
    """Never silently create a key: every deployed collector trusts only the existing public key."""
    secret = os.environ.get(PASSPHRASE, "").encode() or None
    if init_key:
        if private.exists():
            raise PublishError(
                f"{private} already exists; --init-key never replaces the signing key that deployed collectors trust."
            )
        key = Ed25519PrivateKey.generate()
        private.parent.mkdir(parents=True, exist_ok=True)
        encryption = serialization.BestAvailableEncryption(secret) if secret else serialization.NoEncryption()
        private.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
        )
        print(
            "Created new signing key",
            private,
            "(encrypted)" if secret else "(unencrypted; set " + PASSPHRASE + " to encrypt)",
        )
        return key
    if not private.exists():
        raise PublishError(
            f"Signing key {private} is missing. Restore it from backup; collectors only accept releases signed by it. Use --init-key only for a brand-new center with no deployed collectors."
        )
    data = private.read_bytes()
    try:
        return serialization.load_pem_private_key(data, password=secret)
    except TypeError:
        if secret:
            return serialization.load_pem_private_key(
                data, password=None
            )  # Legacy unencrypted key still works when a passphrase is set.
        raise PublishError(f"Signing key is encrypted; set {PASSPHRASE}.") from None
    except ValueError:
        raise PublishError("Signing key could not be decrypted; check " + PASSPHRASE + ".") from None


def collect():
    files = {}
    for folder in ("backend", "config"):
        for path in (ROOT / folder).rglob("*"):
            name = path.relative_to(ROOT).as_posix()
            if path.is_file() and allowed(name):
                files[name] = path.read_bytes()
    for name in EXTRA:
        files[name] = (ROOT / name).read_bytes()
    return files


def packaged(name):
    return allowed(name) or name in EXTRA


def dirty_files():
    """Packaged paths with uncommitted changes, including untracked files that would be shipped."""
    output = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", "backend", "config", *EXTRA],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "replace")
    entries = output.split("\0")
    names = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        names.append(entry[3:])
        if entry[0] in "RC":
            index += 1  # Rename/copy entries carry the original path next.
    return sorted(name for name in names if packaged(name))


def run_tests():
    from tempfile import TemporaryDirectory

    # Importing backend.main opens the default node store. Release validation
    # must never create keys or migrate the operator's live node database.
    with TemporaryDirectory(prefix="ecmp-release-tests-") as directory:
        environment = {**os.environ, "PDD_DATA_DIR": directory}
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "backend/tests"], cwd=ROOT, env=environment
        )
    if result.returncode:
        raise PublishError("Backend tests failed; fix them before publishing (or pass --skip-tests deliberately).")


def requirement_hashes():
    collector = (ROOT / "backend/requirements.txt").read_bytes()
    center = ROOT / "backend/center/requirements.txt"
    # `requirements` keeps the legacy combined formula: deployed collectors compare it verbatim. Newer collectors compare collector_requirements only.
    legacy = hashlib.sha256(collector + (center.read_bytes() if center.exists() else b"")).hexdigest()
    return legacy, hashlib.sha256(collector).hexdigest()


def publish(bootstrap=False, init_key=False, allow_dirty=False, skip_tests=False, node_id=None):
    if node_id:
        import uuid

        node_id = str(uuid.UUID(node_id))
        if bootstrap:
            raise PublishError("灰度发布不能生成全量引导包")
    directory = ROOT / ".data/center/releases"
    key = load_key(directory.parent / "update-signing.local.pem", init_key)
    if not allow_dirty and (dirty := dirty_files()):
        raise PublishError(
            "Uncommitted changes in packaged files (commit them, or pass --allow-dirty):\n  " + "\n  ".join(dirty)
        )
    if not skip_tests:
        run_tests()
    directory.mkdir(parents=True, exist_ok=True)
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    files = collect()
    requirements, collector_requirements = requirement_hashes()
    content_hash = hashlib.sha256(canonical({n: hashlib.sha256(v).hexdigest() for n, v in files.items()})).hexdigest()
    latest = directory / (f"latest-{node_id}.json" if node_id else "latest.json")
    if (
        latest.exists()
        and json.loads(latest.read_text(encoding="utf-8"))["release"].get("content_hash") == content_hash
        and not bootstrap
    ):
        print("No collector code changes; existing release retained.")
        return
    version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + content_hash[:10]
    files["collector-version.json"] = canonical(
        {
            "version": version,
            "requirements": requirements,
            "collector_requirements": collector_requirements,
            "files": sorted([*files, "collector-version.json"]),
        }
    )
    path = directory / (version + ".zip")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    release = {
        "format": 1,
        "version": version,
        "requirements": requirements,
        "collector_requirements": collector_requirements,
        "content_hash": content_hash,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "files": {n: hashlib.sha256(v).hexdigest() for n, v in files.items()},
    }
    envelope = {"release": release, "signature": base64.b64encode(key.sign(canonical(release))).decode()}
    write_json(latest, envelope)
    if bootstrap:
        output = ROOT / "dist/ECMP-Collector-Enable-AutoUpdate.zip"
        output.parent.mkdir(exist_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)
            archive.writestr("collector-update-public.pem", public)
            archive.writestr(
                "启用自动升级.txt",
                "关闭原采集控制面板，把本补丁全部解压覆盖到采集端原目录，然后双击一键启动.cmd。保留 .data、runtime 和 .browsers。以后每分钟自动检查中心发布的业务代码更新。无需下载整包。",
            )
        print("One-time bootstrap patch:", output)
    print("Published:", version, "code ZIP bytes:", path.stat().st_size)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bootstrap", action="store_true", help="also build the one-time patch for collectors without auto-update"
    )
    parser.add_argument(
        "--init-key",
        action="store_true",
        help="create the signing key (only for a new center; refuses to overwrite). Encrypted when "
        + PASSPHRASE
        + " is set",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="publish even if packaged files have uncommitted changes"
    )
    parser.add_argument("--skip-tests", action="store_true", help="do not run the backend test suite first")
    parser.add_argument(
        "--node-id", help="publish only to this authenticated collector UUID; keep stable latest unchanged"
    )
    args = parser.parse_args()
    try:
        publish(args.bootstrap, args.init_key, args.allow_dirty, args.skip_tests, args.node_id)
    except PublishError as error:
        sys.exit("Refusing to publish: " + str(error))
