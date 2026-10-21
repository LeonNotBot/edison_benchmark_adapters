"""Messages routing and real Harbor env checks, without Docker or paid API calls."""
import asyncio
import contextvars
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_tb2.py"
spec = importlib.util.spec_from_file_location("openrouter_adapter", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def model(endpoint="https://openrouter.ai/api/v1"):
    return {"provider": "openrouter", "normalized_provider": "openrouter",
            "model_identifier": "openrouter/moonshotai/kimi-k3",
            "api_model_identifier": "moonshotai/kimi-k3", "api_protocol": "openai",
            "api_endpoint": endpoint}


@pytest.mark.parametrize("endpoint", ["https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/", "https://openrouter.ai/api"])
def test_claude_messages_config_and_preflight_agree(endpoint, monkeypatch):
    monkeypatch.setenv("EDISON_MODEL_API_KEY", "selected-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "stale-key")
    selected = model(endpoint)
    original = dict(selected)
    agent = adapter.harbor_agent_config("claude-code", selected)
    calls = []
    monkeypatch.setattr(adapter, "post_json", lambda *args: calls.append(args) or {"content": []})
    result = adapter.model_preflight(selected, {}, agent="claude-code")
    url, headers, payload, _ = calls[0]
    assert url == agent["env"]["ANTHROPIC_BASE_URL"] + "/v1/messages"
    assert url == "https://openrouter.ai/api/v1/messages"
    assert headers["authorization"] == "Bearer selected-key"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "x-api-key" not in headers
    assert payload["model"] == agent["model_name"] == "moonshotai/kimi-k3"
    assert result["harbor_model"] == agent["model_name"]
    assert agent["env"]["ANTHROPIC_AUTH_TOKEN"] == "${EDISON_MODEL_API_KEY}"
    assert agent["env"]["ANTHROPIC_API_KEY"] == ""
    assert "selected-key" not in json.dumps(agent)
    assert selected == original


def test_other_openrouter_agents_still_use_chat_completions(monkeypatch):
    monkeypatch.setenv("EDISON_MODEL_API_KEY", "selected-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = []
    monkeypatch.setattr(adapter, "post_json", lambda *args: calls.append(args) or {"choices": []})
    adapter.model_preflight(model(), {}, agent="hermes")
    assert calls[0][0] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0][2]["model"] == "moonshotai/kimi-k3"
    assert "ANTHROPIC_AUTH_TOKEN" not in adapter.harbor_agent_config("hermes", model())["env"]


@pytest.mark.parametrize("wire", [None, "moonshotai/kimi-k3", "openrouter/moonshotai/kimi-k3"])
def test_preserves_vendor_prefix(wire):
    selected = model()
    selected["api_model_identifier"] = wire
    assert adapter.harbor_agent_config("claude-code", selected)["model_name"] == "moonshotai/kimi-k3"


def test_real_harbor_command_uses_bearer_and_selected_model(tmp_path, monkeypatch):
    from harbor.agents.installed.claude_code import ClaudeCode
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
    from harbor.models.job.config import JobConfig

    monkeypatch.setenv("EDISON_MODEL_API_KEY", "selected-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "old-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-token")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "old-oauth")
    monkeypatch.setenv("ANTHROPIC_MODEL", "old-model")
    c = {"task": {"id": "openrouter-test"},
         "model": model(), "paths": {"run_dir": str(tmp_path), "output_dir": str(tmp_path / "output")},
         "benchmark": {"suite": "terminal-bench/terminal-bench@4.0.0", "agent": "claude-code",
                       "limit": 1, "runs": 1, "task_names": "html-js-filter", "params": {}}}
    command, _, _ = adapter.build_harbor_command(c)
    raw = Path(command[-1]).read_text()
    assert "selected-key" not in raw
    job = JobConfig.model_validate_json(raw)
    assert job.agents[0].extra_allowed_hosts == ["openrouter.ai"]
    child = adapter.harbor_subprocess_env({}, "claude-code", model())
    assert child["ANTHROPIC_API_KEY"] == ""
    assert child["ANTHROPIC_AUTH_TOKEN"] == "selected-key"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
    assert "ANTHROPIC_MODEL" not in child
    for key in list(adapter.os.environ):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_FORCE_")):
            monkeypatch.delenv(key)
    for key, value in child.items():
        monkeypatch.setenv(key, value)
    extra = {k: v.replace("${EDISON_MODEL_API_KEY}", "selected-key") for k, v in job.agents[0].env.items()}
    agent = ClaudeCode(logs_dir=tmp_path, model_name=job.agents[0].model_name, extra_env=extra)
    agent._resume = False

    class StopBeforeModelCall(Exception):
        pass

    class CaptureEnvironment:
        _persistent_env = {}
        _exec_env_overlays = contextvars.ContextVar("test_overlays", default=())
        scoped_exec_env = BaseEnvironment.scoped_exec_env
        _merge_env = BaseEnvironment._merge_env

        async def exec(self, command, env=None, **kwargs):
            if "--output-format=stream-json" in command:
                effective = self._merge_env(env)
                assert effective["ANTHROPIC_API_KEY"] == ""
                assert effective["ANTHROPIC_AUTH_TOKEN"] == "selected-key"
                assert effective["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
                for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                    assert effective[name] == "moonshotai/kimi-k3"
                assert "CLAUDE_CODE_OAUTH_TOKEN" not in effective
                raise StopBeforeModelCall
            return SimpleNamespace(return_code=0, stdout="", stderr="")

    environment = CaptureEnvironment()
    with environment.scoped_exec_env(agent.extra_env), pytest.raises(StopBeforeModelCall):
        asyncio.run(agent.run("Reply OK", environment, AgentContext()))
