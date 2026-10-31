"""Build an additive Qt runtime extension from the verified development wheels.

No collector code, credentials, browsers or business data are included.
Extract into a stopped collector's root, then restart its console.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "6.10.2"


def build():
    output = ROOT / "dist" / f"ECMP-Qt6-Runtime-{VERSION}.zip"
    output.parent.mkdir(exist_ok=True)
    files = {}
    for name in ("PySide6-Essentials", "shiboken6"):
        dist = importlib.metadata.distribution(name)
        if dist.version != VERSION:
            raise RuntimeError(f"{name} must be {VERSION}")
        for entry in dist.files:
            if ".." in entry.parts or entry.suffix == ".pyc" or "__pycache__" in entry.parts:
                continue
            source = Path(dist.locate_file(entry))
            if source.is_file():
                files[f".runtime/qt6-{VERSION}/{entry.as_posix()}"] = source
    # Preserve package license metadata, DLLs and Qt platform plugins unchanged.
    manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for name, path in files.items():
            archive.write(path, name)
        archive.writestr("qt-runtime-manifest.json", json.dumps(manifest, indent=2))
        archive.writestr(
            "Qt运行库升级说明.txt",
            "先退出采集控制台（停止服务），备份原目录；将本包解压到采集端根目录。\n需要配套的 PySide6 控制台代码。重新打开后节点信息显示 PySide6。\n本包仅新增 .runtime/qt6-6.10.2，不覆盖原 Python、浏览器或业务数据。\n回退：退出控制台，将 .runtime/qt6-6.10.2 改名后重新打开，兼容启动器会使用旧版界面。\n请保留随包 Qt / PySide6 / Shiboken 许可文件。",
        )
    output.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(output.read_bytes()).hexdigest() + "  " + output.name, encoding="ascii"
    )
    print(output)
    print(f"{len(files)} files, {output.stat().st_size} bytes")


if __name__ == "__main__":
    build()
