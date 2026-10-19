#!/usr/bin/env python3
"""Download an exact official Linux Claude Code release; never execute it on the host."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "harbor_patches"))
from claude_cache import validate_version, verified_binary

BASE_URL = "https://downloads.claude.ai/claude-code-releases"
DEFAULT_ROOT = Path(__file__).resolve().parents[3] / "claude-code-cache"


def download(url: str, target: Path, timeout: int, resume: bool = False) -> None:
    for attempt in range(3):
        command = [
            "curl", "--fail", "--location", "--silent", "--show-error", "--http1.1",
            "--connect-timeout", "20", "--max-time", str(timeout),
            "--speed-time", "45", "--speed-limit", "1024",
        ]
        if resume:
            command += ["--continue-at", "-"]
        result = subprocess.run(command + ["--output", str(target), url])
        if result.returncode == 0:
            return
        if result.returncode not in {18, 28, 35, 52, 56} or attempt == 2:
            raise subprocess.CalledProcessError(result.returncode, command)
        print(f"Download interrupted; retry {attempt + 1}/2 (resume={resume})", flush=True)
        time.sleep(2)


def prepare(root: Path, version: str, platforms: list[str]) -> None:
    validate_version(version)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=root) as temporary:
        staging = Path(temporary)
        release = staging / version
        release.mkdir()
        download(f"{BASE_URL}/{version}/manifest.json", release / "manifest.json", 45)
        manifest = json.loads((release / "manifest.json").read_text())
        if manifest.get("version") != version:
            raise ValueError("Official manifest version mismatch")
        for platform in platforms:
            try:
                verified_binary(root, version, platform)
                print(f"Already verified: {version}/{platform}", flush=True)
                continue
            except RuntimeError:
                pass
            entry = manifest["platforms"][platform]
            print(f"Downloading {version}/{platform}: {entry['size']} bytes", flush=True)
            partial = root / ".partials" / version / platform / "claude"
            partial.parent.mkdir(parents=True, exist_ok=True)
            if not partial.is_file() or partial.stat().st_size != entry["size"]:
                download(f"{BASE_URL}/{version}/{platform}/claude", partial, 600, resume=True)
            binary = release / platform / "claude"
            binary.parent.mkdir(parents=True, exist_ok=True)
            # Link for validation; retain interrupted downloads across invocations.
            binary.hardlink_to(partial)
            try:
                verified_binary(staging, version, platform)
            except RuntimeError:
                partial.unlink(missing_ok=True)
                raise
            destination = root / version / platform / "claude"
            destination.parent.mkdir(parents=True, exist_ok=True)
            binary.chmod(0o755)
            binary.replace(destination)
            partial.unlink()
            print(f"SHA256 verified: {entry['checksum']}", flush=True)
        destination = root / version
        destination.mkdir(exist_ok=True)
        (release / "manifest.json").replace(destination / "manifest.json")
        # Activate only after every requested artifact has passed verification.
        marker = staging / "DEFAULT_VERSION"
        marker.write_text(version + "\n")
        marker.replace(root / "DEFAULT_VERSION")
    print(f"Cache ready: {root} (default {version})", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Exact version, not stable/latest")
    parser.add_argument("--platform", action="append", required=True,
                        choices=["linux-x64", "linux-arm64", "linux-x64-musl", "linux-arm64-musl"])
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    prepare(args.cache_dir.expanduser().resolve(), args.version, args.platform)
