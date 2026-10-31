"""Portable runtime locations, including the local workstation installation."""


def executable(root, windowed=False):
    name = "pythonw.exe" if windowed else "python.exe"
    for directory in (root / "runtime", root / ".runtime/collector"):
        path = directory / name
        if path.exists():
            return path
    raise ValueError("采集端运行环境缺失，请检查 runtime 或 .runtime/collector 目录")
