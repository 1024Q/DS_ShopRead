"""Optional Qt desktop runtime selection; legacy nodes keep their working launcher."""

import importlib.util
from pathlib import Path
import sys

QT_VERSION = "6.10.2"


def activate_qt(root):
    # Versioned, additive runtime extension. No replacement of the Python runtime.
    folder = Path(root) / ".runtime" / ("qt6-" + QT_VERSION)
    if (folder / "PySide6/QtWidgets.pyd").is_file():
        sys.path.insert(0, str(folder))
    return importlib.util.find_spec("PySide6") is not None


def main():
    root = Path(__file__).resolve().parents[1]
    if activate_qt(root):
        try:
            from .collector_qt import main as run
        except ImportError, OSError:
            from .collector_logs import DailyLog

            try:
                DailyLog(root / ".data/launcher-logs", "panel").write(
                    "WARNING Qt 运行库未完整安装，已使用兼容控制台；请重新安装 Qt 运行库补丁。"
                )
            except OSError:
                pass
            from .collector_launcher import main as run
    else:
        from .collector_launcher import main as run
    run()
