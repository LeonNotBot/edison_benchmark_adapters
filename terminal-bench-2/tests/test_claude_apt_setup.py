import asyncio
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('edison_apt_patch', ROOT / 'scripts/harbor_patches/sitecustomize.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)
spec = importlib.util.spec_from_file_location('tb_adapter_apt', ROOT / 'scripts/run_tb2.py')
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def test_generated_setup_is_valid_shell_and_keeps_security_checks():
    command = patch.claude_apt_setup_command('https://mirrors.aliyun.com/debian')
    subprocess.run(['bash', '-n'], input=command, text=True, check=True)
    assert 'debian-security|https://mirrors.aliyun.com/debian-security' in command
    assert 'Acquire::Retries "3"' in command
    assert 'APT::Update::Error-Mode "any"' in command
    assert 'trusted=yes' not in command and 'AllowUnauthenticated' not in command
    assert 'sed -i' not in patch.claude_apt_setup_command('')


@pytest.mark.parametrize('mirror', ['file:///tmp/repo', 'http://user:pass@example.com/debian',
                                    'https://example.com/a|b', 'https://example.com/a&b'])
def test_invalid_source_rejected(mirror):
    with pytest.raises(ValueError):
        patch.claude_apt_setup_command(mirror)


def test_claude_patch_is_agent_scoped_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv('EDISON_TB4_PATCH_CLAUDE_APT', '1')
    for agent in ('terminus-2', 'oracle', 'hermes'):
        assert 'EDISON_TB4_PATCH_CLAUDE_APT' not in adapter.harbor_subprocess_env({}, agent)
    assert 'EDISON_TB4_PATCH_CLAUDE_APT' not in adapter.harbor_subprocess_env({'patch_claude_apt': False}, 'claude-code')
    env = adapter.harbor_subprocess_env({'container_apt_mirror': 'https://mirrors.aliyun.com/debian'}, 'claude-code')
    assert env['EDISON_TB4_CLAUDE_APT_MIRROR'] == 'https://mirrors.aliyun.com/debian'


def test_real_harbor_install_hook_runs_before_original_and_once(monkeypatch):
    from harbor.agents.installed.claude_code import ClaudeCode
    calls = []
    async def original(self, environment):
        calls.append('original')
    async def root(self, environment, command):
        calls.append(command)
    monkeypatch.setattr(ClaudeCode, 'install', original)
    monkeypatch.setattr(ClaudeCode, 'exec_as_root', root)
    monkeypatch.setenv('EDISON_TB4_PATCH_CLAUDE_APT', '1')
    monkeypatch.setenv('EDISON_TB4_CLAUDE_APT_MIRROR', 'https://mirrors.aliyun.com/debian')
    patch._patch_claude_code_install()
    patch._patch_claude_code_install()
    asyncio.run(ClaudeCode.install(object.__new__(ClaudeCode), object()))
    assert len(calls) == 2 and calls[-1] == 'original'
    assert 'mirrors.aliyun.com' in calls[0]


@pytest.mark.parametrize('source,expected', [
    ('http://mirrors.aliyun.com/debian/', 'https://mirrors.aliyun.com/debian'),
    ('http://apt.internal/debian', 'http://apt.internal/debian'),
    ('', ''),
])
def test_claude_legacy_mirror_uses_https_without_changing_custom_sources(source, expected):
    env = adapter.harbor_subprocess_env({'container_apt_mirror': source}, 'claude-code')
    assert env['EDISON_TB4_CLAUDE_APT_MIRROR'] == expected
