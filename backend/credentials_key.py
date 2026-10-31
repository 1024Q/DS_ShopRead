"""Shop-password encryption key, optionally protected with Windows DPAPI.

Default: unchanged plaintext ``credentials.key`` (fully backward compatible).
With ``ECMP_CREDENTIALS_DPAPI=1`` on Windows, the key is migrated once to
``credentials.key.dpapi`` (bound to the Windows account running the collector)
and the plaintext file is removed, so a copied ``.data`` folder cannot decrypt
saved shop passwords. Once the protected file exists it is always used.

Keep an offline copy before migrating if you need cross-machine restores:
    python -m backend.credentials_key export <path>
"""

import os
import sys
from pathlib import Path

PLAIN = "credentials.key"
PROTECTED = "credentials.key.dpapi"


def enabled():
    from .dpapi import available

    return available() and os.environ.get("ECMP_CREDENTIALS_DPAPI", "").strip().lower() in ("1", "true", "yes")


def _write_atomic(path, data):
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    temp.replace(path)


def _unprotect(path):
    from .dpapi import protect

    try:
        return protect(path.read_text(encoding="ascii").strip(), True).encode("ascii")
    except OSError as error:
        raise RuntimeError(
            f"无法解开 {PROTECTED}：请使用原 Windows 账户运行采集端，或用离线备份的 credentials.key 恢复"
        ) from error


def _protect_to(path, key):
    from .dpapi import protect

    blob = protect(key.decode("ascii"))
    # Never drop the plaintext before proving the protected copy decrypts.
    if protect(blob, True).encode("ascii") != key:
        raise RuntimeError("DPAPI 加密校验失败，保留原 credentials.key")
    _write_atomic(path, blob.encode("ascii"))


def load(directory):
    from cryptography.fernet import Fernet

    directory = Path(directory)
    protected, plain = directory / PROTECTED, directory / PLAIN
    if protected.exists():
        return _unprotect(protected)
    if not plain.exists():
        key = Fernet.generate_key()
        if enabled():
            _protect_to(protected, key)
        else:
            _write_atomic(plain, key)
        return key
    key = plain.read_bytes()
    if enabled():
        Fernet(key)  # refuse to migrate a damaged key
        _protect_to(protected, key)
        if _unprotect(protected) == key:
            plain.unlink()
    return key


def export(directory, target):
    target = Path(target)
    if target.exists():
        raise FileExistsError(f"{target} 已存在，未覆盖")
    target.write_bytes(load(directory))
    return target


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "export":
        root = Path(os.getenv("PDD_DATA_DIR") or Path(__file__).resolve().parents[1] / ".data")
        print("已导出：", export(root, sys.argv[2]))
    else:
        print("用法：python -m backend.credentials_key export <离线保存路径>")
        sys.exit(2)
