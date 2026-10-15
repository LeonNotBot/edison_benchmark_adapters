"""Offline integration checks; never start Docker or call a model API."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_tb2.py"
spec = importlib.util.spec_from_file_location("run_tb2", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def config(tmp_path, agent="claude-code"):
    return {
        "task": {"id": "tb4-offline"},
        "benchmark": {"adapter": "terminal-bench-2", "suite": "terminal-bench/terminal-bench@4.0.0",
                      "agent": agent, "limit": 1, "runs": 1, "task_names": "layout-config-recreation2",
                      "params": {"timeout_multiplier": 1, "agent_timeout_multiplier": 1,
                                 "agent_setup_timeout_multiplier": 1, "no_delete": False}},
        "model": {"provider": "sky", "normalized_provider": "anthropic",
                  "model_identifier": "sky/anthropic/deepseek-flash",
                  "api_endpoint": "https://example.invalid/v1"},
        "paths": {"run_dir": str(tmp_path), "output_dir": str(tmp_path / "output"),
                  "result_json": str(tmp_path / "result.json"), "progress_json": str(tmp_path / "progress.json")},
    }


def test_tb4_generated_config_matches_real_harbor_schema(tmp_path, monkeypatch):
    from harbor.models.job.config import JobConfig

    monkeypatch.setenv("EDISON_MODEL_API_KEY", "test-secret-not-for-files")
    c = config(tmp_path)
    c["benchmark"]["params"].update(claude_code_version="2.1.0", reasoning_effort="high")
    command, _, _ = adapter.build_harbor_command(c)
    raw = Path(command[-1]).read_text()
    assert "test-secret-not-for-files" not in raw
    job = JobConfig.model_validate_json(raw)
    assert job.datasets[0].name == "terminal-bench/terminal-bench"
    assert job.datasets[0].version == "4.0.0"
    assert job.datasets[0].task_names == ["terminal-bench/layout-config-recreation2"]
    assert job.agents[0].name == "claude-code"
    assert job.agents[0].model_name == "deepseek-flash"
    assert job.agents[0].extra_allowed_hosts == ["example.invalid"]
    assert job.agents[0].env["ANTHROPIC_API_KEY"] == "${EDISON_MODEL_API_KEY}"
    assert job.agents[0].env["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    assert job.agents[0].kwargs == {"version": "2.1.0", "reasoning_effort": "high"}
    assert job.agents[0].override_timeout_sec is None
    assert job.timeout_multiplier == job.agent_timeout_multiplier == 1
    assert job.environment.type == "docker" and job.environment.delete


@pytest.mark.parametrize("identifier,expected", [
    ("anthropic/claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("sky/anthropic/deepseek-flash", "deepseek-flash"),
    ("deepseek-flash", "deepseek-flash"),
    ("sky/vendor/model", "vendor/model"),
])
def test_claude_model_prefix(tmp_path, identifier, expected):
    model = config(tmp_path)["model"]
    model["model_identifier"] = identifier
    assert adapter.harbor_agent_config("claude-code", model)["model_name"] == expected


@pytest.mark.parametrize("provider", ["moon", "openai", "openrouter"])
def test_claude_rejects_wrong_protocol(tmp_path, provider):
    model = config(tmp_path)["model"]
    model.update(provider=provider, normalized_provider=provider)
    with pytest.raises(ValueError, match="Anthropic"):
        adapter.harbor_agent_config("claude-code", model)


def test_claude_child_does_not_inherit_other_logins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stale")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "stale")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "stale")
    monkeypatch.setenv("ANTHROPIC_MODEL", "wrong-model")
    monkeypatch.setenv("EDISON_MODEL_API_KEY", "selected-version-key")
    monkeypatch.setenv("EDISON_MODEL_API_ENDPOINT", "https://example.invalid/v1/")
    env = adapter.harbor_subprocess_env({}, "claude-code")
    assert env["ANTHROPIC_API_KEY"] == "selected-version-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    for key in ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_MODEL",
                "CLAUDE_CODE_USE_BEDROCK", "AWS_BEARER_TOKEN_BEDROCK"):
        assert key not in env


def test_preflight_uses_same_wire_model_and_anthropic_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    model = config(tmp_path)["model"]
    model["api_model_identifier"] = "deepseek-flash"
    calls = []
    def post(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return {"content": [{"type": "text", "text": "OK"}]}
    monkeypatch.setattr(adapter, "post_json", post)
    adapter.model_preflight(model, {})
    url, headers, body = calls[0]
    assert url == "https://example.invalid/v1/messages"
    assert headers["x-api-key"] == "test-key"
    assert body["model"] == adapter.harbor_agent_config("claude-code", model)["model_name"]


def test_wrong_protocol_from_shared_routing_cannot_bypass_claude_guard(tmp_path):
    model = config(tmp_path)["model"]
    model["api_protocol"] = "openai"
    with pytest.raises(ValueError, match="Anthropic"):
        adapter.harbor_agent_config("claude-code", model)


@pytest.mark.parametrize("suite", ["terminal-bench/terminal-bench-2", "terminal-bench/terminal-bench-2-1"])
@pytest.mark.parametrize("agent", ["oracle", "hermes", "terminus-2"])
def test_legacy_suites_keep_names(tmp_path, suite, agent):
    c = config(tmp_path, agent)
    c["benchmark"]["suite"] = suite
    command, _, _ = adapter.build_harbor_command(c)
    job = json.loads(Path(command[-1]).read_text())
    assert job["datasets"][0]["name"] == suite
    assert "version" not in job["datasets"][0]


def test_runtime_rejects_old_harbor(monkeypatch):
    monkeypatch.setattr(adapter.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "0.1.0", ""))
    with pytest.raises(RuntimeError, match="0.20.0"):
        adapter.check_tb4_runtime("harbor")


def test_adapter_subprocess_returns_edison_result_and_progress(tmp_path):
    # Stand-in only for the expensive Harbor run; use the real adapter CLI,
    # config generation, streaming, file discovery and result conversion.
    fake = tmp_path / "fake-harbor"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, sys
from pathlib import Path
if '--version' in sys.argv:
    print('0.20.0'); sys.exit(0)
c = json.loads(Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
assert c['datasets'][0]['version'] == '4.0.0'
assert c['agents'][0]['model_name'] == 'deepseek-flash'
job = Path(c['jobs_dir']) / c['job_name']
trial = job / 'layout-config-recreation2__test'
trial.mkdir(parents=True)
(trial / 'result.json').write_text(json.dumps({'task_name': 'terminal-bench/layout-config-recreation2', 'verifier_result': {'rewards': {'reward': 1.0}}, 'agent_result': {'output': 'fixture output'}}))
(job / 'result.json').write_text(json.dumps({'id': 'offline', 'n_total_trials': 1, 'stats': {'n_trials': 1, 'n_errors': 0}}))
''')
    fake.chmod(0o755)
    c = config(tmp_path)
    c["benchmark"]["params"].update(harbor_bin=str(fake), model_preflight=False)
    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(c))
    env = {**os.environ, "EDISON_MODEL_API_KEY": "offline-only", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(input_file)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads((tmp_path / "result.json").read_text())
    assert output["suite"] == "terminal-bench/terminal-bench@4.0.0"
    assert output["agent"] == "claude-code"
    assert output["status"] == "completed" and output["score"] == 1.0
    assert output["samples"][0]["output"] == "fixture output"
    assert (tmp_path / "progress.json").exists()
