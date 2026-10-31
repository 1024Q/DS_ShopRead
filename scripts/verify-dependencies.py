"""Check installed versions against a lock without installing or upgrading anything."""

import argparse
import importlib.metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(lock):
    failures = []
    count = 0
    for line in Path(lock).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==", 1)
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            actual = "missing"
        count += 1
        if actual != expected:
            failures.append(f"{name}: expected {expected}, installed {actual}")
    if failures:
        raise RuntimeError(
            "Runtime differs from lock; use the separate runtime upgrade workflow:\n" + "\n".join(failures)
        )
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--center", action="store_true")
    args = parser.parse_args()
    lock = ROOT / ("backend/center/requirements.lock" if args.center else "backend/requirements.lock")
    print(f"Verified {verify(lock)} pinned dependencies")
