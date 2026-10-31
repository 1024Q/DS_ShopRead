"""Build a portable Windows x64 ZIP from the verified local Python environment.
Explicit allowlist: never package .data, accounts, tokens, browser profiles or Git.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def build(output=None):
    if sys.platform != "win32" or sys.maxsize < 2**32:
        raise RuntimeError("Build on Windows x64")
    destination = Path(output or ROOT / "dist" / ("collector-" + datetime.now().strftime("%Y%m%d-%H%M%S"))).resolve()
    if destination.exists():
        raise RuntimeError("Output already exists; use a new output directory")
    destination.mkdir(parents=True)
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", "*.log", "*.local*", ".env*", "tests", "test", "idlelib", "ensurepip"
    )
    # Use the already verified portable runtime. Never copy a developer's data directory.
    runtime = destination / "runtime"
    portable = ROOT / ".runtime/collector"
    if not (portable / "pythonw.exe").is_file():
        raise RuntimeError("Verified portable runtime missing: .runtime/collector")
    import subprocess

    subprocess.run([str(portable / "python.exe"), str(ROOT / "scripts/verify-dependencies.py")], check=True)
    shutil.copytree(portable, runtime, ignore=ignore)
    # Ship exactly the signed stable code, including its version and public verification key.
    sys.path.insert(0, str(ROOT))
    from backend.collector_update import verify

    envelope = json.loads((ROOT / ".data/center/releases/latest.json").read_text(encoding="utf-8"))
    release = verify(ROOT, envelope)
    if "backend/collector_desktop.py" in release["files"]:
        from backend.collector_desktop import QT_VERSION

        qt = ROOT / ".runtime" / ("qt6-" + QT_VERSION)
        if not (qt / "PySide6/QtWidgets.pyd").is_file():
            raise RuntimeError("Install the verified Qt runtime extension before building a new desktop package")
        shutil.copytree(qt, destination / ".runtime" / qt.name, ignore=ignore)
    code_zip = ROOT / ".data/center/releases" / (release["version"] + ".zip")
    if hashlib.sha256(code_zip.read_bytes()).hexdigest() != release["sha256"]:
        raise RuntimeError("Signed release ZIP checksum mismatch")
    with zipfile.ZipFile(code_zip) as code:
        for name, expected in release["files"].items():
            data = code.read(name)
            if hashlib.sha256(data).hexdigest() != expected:
                raise RuntimeError("Release file checksum mismatch")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    shutil.copy2(ROOT / "collector-update-public.pem", destination / "collector-update-public.pem")
    shutil.copytree(ROOT / ".browsers/chrome-win64", destination / ".browsers/chrome-win64", ignore=ignore)
    (destination / "Start-Collector.cmd").write_text(
        '@echo off\r\ncd /d "%~dp0"\r\nif not exist "runtime\\pythonw.exe" (echo Please extract the entire ZIP first. & pause & exit /b 1)\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0collector-launcher.py"\r\n',
        encoding="ascii",
    )
    shutil.copy2(destination / "Start-Collector.cmd", destination / "一键启动.cmd")
    shutil.copy2(destination / "COLLECTOR-QUICKSTART.md", destination / "使用说明.txt")
    (destination / "package-info.json").write_text(
        json.dumps(
            {
                "python": sys.version,
                "built_at": datetime.now().isoformat(),
                "platform": "Windows x64",
                "credentials_included": False,
                "version": release["version"],
                "enrollment": "center approval required",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {
        str(p.relative_to(destination)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in destination.rglob("*")
        if p.is_file()
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in destination.rglob("*"):
            if path.is_file():
                zipped.write(path, str(Path("ECMP-Collector") / path.relative_to(destination)))
    archive.with_suffix(".zip.sha256").write_text(
        hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name, encoding="ascii"
    )
    print(str(archive))
    print(f"Files: {len(manifest)}, ZIP bytes: {archive.stat().st_size}")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    build(parser.parse_args().output)
