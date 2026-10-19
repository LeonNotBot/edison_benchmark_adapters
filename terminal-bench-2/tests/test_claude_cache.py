import asyncio
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "harbor_patches"))
from claude_cache import install_cached_claude, verified_binary

spec = importlib.util.spec_from_file_location("cache_adapter", ROOT / "scripts/run_tb2.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
VERSION = "2.1.292"


def make_cache(root, platform="linux-x64"):
    binary = root / VERSION / platform / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test executable")
    (root / VERSION / "manifest.json").write_text(json.dumps({
        "version": VERSION, "platforms": {platform: {
            "size": binary.stat().st_size,
            "checksum": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }},
    }))
    (root / "DEFAULT_VERSION").write_text(VERSION)
    return binary


@pytest.mark.parametrize("platform", ["linux-x64", "linux-arm64", "linux-x64-musl", "linux-arm64-musl"])
def test_verified_binary_and_corruption(tmp_path, platform):
    binary = make_cache(tmp_path, platform)
    assert verified_binary(tmp_path, VERSION, platform) == binary
    binary.write_bytes(b"bad! executable")  # same size, different hash
    with pytest.raises(RuntimeError, match="Online download was not attempted"):
        verified_binary(tmp_path, VERSION, platform)


@pytest.mark.parametrize("version,platform", [("../escape", "linux-x64"), (VERSION, "../../escape")])
def test_cache_paths_are_validated(tmp_path, version, platform):
    with pytest.raises(ValueError):
        verified_binary(tmp_path, version, platform)


class Environment:
    def __init__(self, platform="linux-x64"):
        self.platform = platform
        self.uploads = []

    async def exec(self, command):
        subprocess.run(["bash", "-n"], input=command, text=True, check=True)
        return SimpleNamespace(return_code=0, stdout=self.platform)

    async def upload_file(self, source_path, target_path):
        self.uploads.append((source_path, target_path))


class Agent:
    logger = logging.getLogger("cache-test")

    def __init__(self, reported=VERSION):
        self.commands = []
        self.reported = reported

    async def exec_as_root(self, environment, command, **kwargs):
        self.commands.append(command)
        subprocess.run(["bash", "-n"], input=command, text=True, check=True)

    async def exec_as_agent(self, environment, command, **kwargs):
        self.commands.append(command)
        subprocess.run(["bash", "-n"], input=command, text=True, check=True)
        return SimpleNamespace(stdout=f"{self.reported} (Claude Code)")


def test_cache_copies_only_program_without_bootstrap_or_install(tmp_path):
    binary = make_cache(tmp_path)
    env, agent = Environment(), Agent()
    asyncio.run(install_cached_claude(agent, env, tmp_path, VERSION))
    assert env.uploads == [(binary, "/installed-agent/edison-claude")]
    commands = "\n".join(agent.commands)
    assert "downloads.claude.ai" not in commands
    assert "claude install" not in commands
    assert "curl" not in commands
    assert "apt-get install -y procps" in commands


def test_missing_platform_fails_before_install_without_online_fallback(tmp_path):
    make_cache(tmp_path)
    env, agent = Environment("linux-arm64"), Agent()
    with pytest.raises(RuntimeError, match="linux-arm64"):
        asyncio.run(install_cached_claude(agent, env, tmp_path, VERSION))
    assert not env.uploads and not agent.commands


def test_wrong_executable_version_fails(tmp_path):
    make_cache(tmp_path)
    with pytest.raises(RuntimeError, match="version check failed"):
        asyncio.run(install_cached_claude(Agent("2.1.0"), Environment(), tmp_path, VERSION))


def test_cache_pins_job_and_is_scoped_to_claude(tmp_path, monkeypatch):
    make_cache(tmp_path)
    monkeypatch.setattr(adapter, "DEFAULT_CLAUDE_CACHE_DIR", tmp_path)
    monkeypatch.delenv("EDISON_CLAUDE_CODE_CACHE_DIR", raising=False)
    model = {"provider": "anthropic", "model_identifier": "anthropic/test-model",
             "api_endpoint": "https://example.invalid/v1"}
    config = adapter.harbor_agent_config_with_runtime_env("claude-code", model, {})
    assert config["kwargs"]["version"] == VERSION
    assert config["env"]["DISABLE_AUTOUPDATER"] == "1"
    env = adapter.harbor_subprocess_env({}, "claude-code")
    assert env["EDISON_TB4_CLAUDE_CACHE_DIR"] == str(tmp_path)
    assert env["EDISON_TB4_CLAUDE_CACHE_VERSION"] == VERSION
    for agent in ("oracle", "hermes", "terminus-2"):
        assert "EDISON_TB4_CLAUDE_CACHE_DIR" not in adapter.harbor_subprocess_env({}, agent)
    assert "EDISON_TB4_CLAUDE_CACHE_DIR" not in adapter.harbor_subprocess_env(
        {"use_claude_cache": False}, "claude-code")
    # An explicit version must not silently use the cached default.
    assert adapter.claude_cache_config({"claude_code_version": "2.1.0"})[1] == "2.1.0"


def test_worker_without_cache_preserves_online_behavior(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "DEFAULT_CLAUDE_CACHE_DIR", tmp_path)
    monkeypatch.delenv("EDISON_CLAUDE_CODE_CACHE_DIR", raising=False)
    assert adapter.claude_cache_config({}) is None
    with pytest.raises(ValueError, match="DEFAULT_VERSION"):
        adapter.claude_cache_config({"claude_code_cache_dir": str(tmp_path)})


def test_real_harbor_hook_uses_cache_even_if_apt_patch_disabled(tmp_path, monkeypatch):
    from harbor.agents.installed.claude_code import ClaudeCode
    import claude_cache
    make_cache(tmp_path)
    calls = []
    async def original(*args):
        pytest.fail("online installer must not run")
    async def cached(agent, environment, root, version):
        calls.append((root, version))
    monkeypatch.setattr(ClaudeCode, "install", original)
    monkeypatch.setattr(claude_cache, "install_cached_claude", cached)
    monkeypatch.setenv("EDISON_TB4_CLAUDE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("EDISON_TB4_CLAUDE_CACHE_VERSION", VERSION)
    monkeypatch.delenv("EDISON_TB4_PATCH_CLAUDE_APT", raising=False)
    spec = importlib.util.spec_from_file_location("cache_patch", ROOT / "scripts/harbor_patches/sitecustomize.py")
    patch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(patch)
    agent = object.__new__(ClaudeCode)
    agent._version = VERSION
    asyncio.run(agent.install(object()))
    assert calls == [(tmp_path, VERSION)]
    agent._version = "2.1.0"
    with pytest.raises(RuntimeError, match="differs"):
        asyncio.run(agent.install(object()))


def load_downloader():
    spec = importlib.util.spec_from_file_location("cache_download", ROOT / "scripts/cache_claude_code.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("bad_binary", [False, True])
def test_downloader_only_activates_verified_release(tmp_path, monkeypatch, bad_binary):
    downloader = load_downloader()
    data = b"official executable"
    manifest = {"version": VERSION, "platforms": {"linux-x64": {
        "size": len(data), "checksum": hashlib.sha256(data).hexdigest(),
    }}}
    def fake_download(url, target, timeout, resume=False):
        if url.endswith("manifest.json"):
            target.write_text(json.dumps(manifest))
        else:
            assert resume
            target.write_bytes(b"corrupt" if bad_binary else data)
    monkeypatch.setattr(downloader, "download", fake_download)
    if bad_binary:
        with pytest.raises(RuntimeError):
            downloader.prepare(tmp_path, VERSION, ["linux-x64"])
        assert not (tmp_path / "DEFAULT_VERSION").exists()
        assert not (tmp_path / VERSION / "linux-x64/claude").exists()
    else:
        downloader.prepare(tmp_path, VERSION, ["linux-x64"])
        assert (tmp_path / "DEFAULT_VERSION").read_text().strip() == VERSION
        assert verified_binary(tmp_path, VERSION, "linux-x64").read_bytes() == data


def test_download_retry_resumes_instead_of_restarting(monkeypatch, tmp_path):
    downloader = load_downloader()
    commands = []
    def curl(command):
        commands.append(command)
        return SimpleNamespace(returncode=28 if len(commands) == 1 else 0)
    monkeypatch.setattr(downloader.subprocess, "run", curl)
    monkeypatch.setattr(downloader.time, "sleep", lambda _: None)
    downloader.download("https://example.invalid/claude", tmp_path / "partial", 30, resume=True)
    assert len(commands) == 2
    assert all("--continue-at" in command for command in commands)
