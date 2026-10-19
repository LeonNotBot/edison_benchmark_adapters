"""Install a verified, Worker-local Claude binary into a fresh Harbor environment."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def validate_version(version: str) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ValueError("Claude cache requires an exact version, e.g. 2.1.292")
    return version


def verified_binary(root: Path, version: str, platform: str) -> Path:
    validate_version(version)
    if platform not in {"linux-x64", "linux-arm64", "linux-x64-musl", "linux-arm64-musl"}:
        raise ValueError(f"Unsupported Claude cache platform: {platform}")
    directory = root / version
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        entry = manifest["platforms"][platform]
        binary = directory / platform / "claude"
        if manifest["version"] != version:
            raise ValueError("manifest version mismatch")
        if binary.stat().st_size != entry["size"]:
            raise ValueError("binary size mismatch")
        with binary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != entry["checksum"]:
            raise ValueError("binary SHA256 mismatch")
        return binary
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"Claude local cache unavailable/invalid: {directory / platform}; "
            "prepare it with scripts/cache_claude_code.py. Online download was not attempted."
        ) from exc


async def install_cached_claude(agent, environment, root: Path, version: str) -> None:
    # Detect the container architecture, never the Mac/Worker architecture.
    detected = await environment.exec(command=(
        "set -e; test \"$(uname -s)\" = Linux; "
        "case $(uname -m) in x86_64|amd64) p=linux-x64;; "
        "aarch64|arm64) p=linux-arm64;; *) exit 1;; esac; "
        "if ls /lib/ld-musl-*.so.1 >/dev/null 2>&1; then p=$p-musl; fi; printf '%s' \"$p\""
    ))
    if detected.return_code != 0:
        raise RuntimeError("Cannot detect container platform for Claude local cache")
    platform = detected.stdout.strip()
    binary = verified_binary(root, version, platform)
    agent.logger.info(f"[edison-claude-cache] Using {version} / {platform}; no Claude download")

    # These tools are used for Claude process cleanup; do not install curl/node/npm.
    await agent.exec_as_root(environment, command=(
        "set -e; if ! command -v ps >/dev/null 2>&1 || ! command -v pgrep >/dev/null 2>&1; then "
        "if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y procps; "
        "elif command -v apk >/dev/null 2>&1; then apk add --no-cache procps; "
        "elif command -v yum >/dev/null 2>&1; then yum install -y procps-ng; "
        "else echo 'Missing ps/pgrep and no supported package manager' >&2; exit 1; fi; fi; "
        "mkdir -p /installed-agent /usr/local/bin"
    ), env={"DEBIAN_FRONTEND": "noninteractive"})
    await environment.upload_file(source_path=binary, target_path="/installed-agent/edison-claude")
    await agent.exec_as_root(environment, command=(
        "set -e; cp /installed-agent/edison-claude /usr/local/bin/claude; "
        "chmod 755 /usr/local/bin/claude; rm /installed-agent/edison-claude"
    ))
    # Harbor runs Claude from ~/.local/bin. Link only the program, no user state.
    result = await agent.exec_as_agent(environment, command=(
        'set -e; mkdir -p "$HOME/.local/bin"; '
        'ln -sf /usr/local/bin/claude "$HOME/.local/bin/claude"; '
        'export PATH="$HOME/.local/bin:$PATH"; claude --version'
    ), env={"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    if not re.search(r"(?<![\w.])" + re.escape(version) + r"(?![\w.-])", result.stdout or ""):
        raise RuntimeError(f"Cached Claude version check failed; expected {version}")
    agent.logger.info(f"[edison-claude-cache] Installed and verified Claude Code {version}")
