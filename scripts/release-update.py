"""Explicit test -> selected Git commit -> existing signed collector publisher."""

import argparse
import hashlib
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + ROOT.as_posix(), *args], cwd=ROOT)


def safe_path(name):
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Use project-relative paths: " + name)
    resolved = (ROOT / path).resolve()
    if not resolved.is_relative_to(ROOT.resolve()):
        raise ValueError("Path leaves project: " + name)
    parts = {part.lower() for part in path.parts}
    if parts & {
        ".git",
        ".data",
        ".browsers",
        ".runtime",
        ".venv",
        "node_modules",
        "tmp",
        "dist",
        ".pdd-chrome-profile",
    }:
        raise ValueError("Local data/runtime path is forbidden: " + name)
    if any(p.startswith((".next", ".env", ".pdd-chrome-shop")) and p != ".env.example" for p in parts):
        raise ValueError("Local configuration/build path is forbidden: " + name)
    if path.suffix.lower() in {".pem", ".key", ".sqlite3", ".db", ".zip"} or path.name == "collector-version.json":
        raise ValueError("Credential/data/generated file is forbidden: " + name)
    return path.as_posix()


def changed(paths):
    entries = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths).decode("utf-8").split("\0")
    result = []
    for entry in entries:
        if not entry:
            continue
        if any(c in entry[:2] for c in "RCU") or entry[:2] in {"AA", "DD"}:
            raise ValueError("Resolve rename/conflict before releasing: " + entry[3:])
        result.append(safe_path(entry[3:]))
    return sorted(set(result))


def staged_guard(files):
    staged = set(git("diff", "--cached", "--name-only", "-z").decode("utf-8").split("\0")) - {""}
    if staged - set(files):
        raise ValueError("Unrelated staged files; unstage them first: " + ", ".join(sorted(staged - set(files))))


def fingerprint(files, publisher):
    values = {
        **publisher.collect(),
        **{f: (ROOT / f).read_bytes() if (ROOT / f).is_file() else b"<deleted>" for f in files},
    }
    return git("rev-parse", "HEAD"), {f: hashlib.sha256(value).hexdigest() for f, value in values.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True, help="Git commit message")
    parser.add_argument("--paths", nargs="+", required=True, help="Explicit project-relative files/directories")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; no tests, staging, commit or publish")
    parser.add_argument(
        "--build", action="store_true", help="Also build UI into a new .next-release-* directory; does not deploy"
    )
    parser.add_argument("--node-id", help="Optional collector UUID for a pilot release")
    args = parser.parse_args()
    if not args.message.strip():
        raise ValueError("Commit message is empty")
    if args.node_id:
        uuid.UUID(args.node_id)
    # Publisher invokes Git too; retain existing per-process configuration.
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = ROOT.as_posix()
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)
    spec = importlib.util.spec_from_file_location("collector_publisher", ROOT / "scripts/publish-collector-update.py")
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    files = changed([safe_path(p) for p in args.paths])
    if not files:
        raise ValueError("No changes in selected paths; nothing to commit")
    staged_guard(files)
    outside = set(publisher.dirty_files()) - set(files)
    if outside:
        raise ValueError("Other collector changes must be selected too: " + ", ".join(sorted(outside)))
    print("Selected changes:\n" + "\n".join(files), flush=True)
    print(
        "Plan: isolated backend tests -> frontend tests -> optional UI build -> selected commit -> signed publish",
        flush=True,
    )
    if args.dry_run:
        return
    git("var", "GIT_AUTHOR_IDENT")
    publisher.load_key(ROOT / ".data/center/update-signing.local.pem")
    baseline = fingerprint(files, publisher)
    publisher.run_tests()
    node = ROOT / ".runtime/node.exe"
    subprocess.run([str(node), "scripts/test-web.cjs"], cwd=ROOT, check=True)
    if args.build:
        directory = ".next-release-" + uuid.uuid4().hex[:12]
        subprocess.run(
            [str(node), "node_modules/next/dist/bin/next", "build", "--webpack"],
            cwd=ROOT,
            env={**os.environ, "PDD_NEXT_DIST_DIR": directory},
            check=True,
        )
        print("UI build (not deployed): " + directory, flush=True)
    if baseline != fingerprint(files, publisher) or files != changed(args.paths):
        raise ValueError("Code or HEAD changed during validation; rerun the release")
    staged_guard(files)
    git("add", "--all", "--", *files)
    subprocess.run(
        ["git", "-c", "safe.directory=" + ROOT.as_posix(), "commit", "--only", "-m", args.message, "--", *files],
        cwd=ROOT,
        check=True,
    )
    if fingerprint(files, publisher)[1] != baseline[1]:
        raise ValueError("Commit hook changed tested files; rerun validation before publishing")
    publisher.publish(skip_tests=True, node_id=args.node_id)
    print("Commit and signed release completed. Services are not restarted by this script.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        sys.exit("Release stopped: " + str(error))
