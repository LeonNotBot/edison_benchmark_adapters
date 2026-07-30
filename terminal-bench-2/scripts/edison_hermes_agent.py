"""Edison-flavoured Hermes agent for Terminal-Bench / Harbor.

Harbor's built-in Hermes integration treats the ``openai`` model prefix as a
special case and invokes Hermes without ``--provider``. That is fine for
plain OpenAI, but Edison often routes OpenAI-compatible deployments through
custom endpoints such as ``moon`` and Hermes expects those to be called via
``--provider openai-api`` with a bare model name.

This small adapter keeps Harbor's install / trajectory logic, but fixes the
runtime model/provider/env mapping so Terminal-Bench 2 behaves like Edison's
SWE-bench Hermes runner.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

from harbor.agents.installed.hermes import Hermes
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.agent.name import AgentName


_NATIVE_PROVIDERS: dict[str, tuple[str | None, list[str], list[str]]] = {
    # Harbor model prefix -> (Hermes CLI --provider, API key envs, base URL envs)
    "anthropic": (
        "anthropic",
        ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"],
        ["ANTHROPIC_BASE_URL"],
    ),
    # Edison uses Hermes provider "openai-api" for OpenAI-compatible endpoints.
    "openai-api": (
        "openai-api",
        ["OPENAI_API_KEY"],
        ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
    ),
    # Keep native openai as a fallback, but still route through openai-api so
    # custom OPENAI_BASE_URL endpoints work consistently.
    "openai": (
        "openai-api",
        ["OPENAI_API_KEY"],
        ["OPENAI_BASE_URL", "OPENAI_API_BASE"],
    ),
    "zai": ("zai", ["GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"], []),
    "kimi": ("kimi-coding", ["KIMI_API_KEY"], []),
    "minimax": ("minimax", ["MINIMAX_API_KEY"], []),
    "minimax-cn": ("minimax-cn", ["MINIMAX_CN_API_KEY"], []),
}


class EdisonHermes(Hermes):
    """Hermes runner with Edison-compatible provider/env handling."""

    @staticmethod
    def name() -> str:
        return AgentName.HERMES.value

    def _copy_env(self, env: dict[str, str], keys: list[str]) -> None:
        for key in keys:
            value = self._get_env(key)
            if value:
                env[key] = value

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("Model name must be in the format provider/model_name")

        provider, model = self.model_name.split("/", 1)

        env: dict[str, str] = {
            "HERMES_HOME": "/tmp/hermes",
            "TERMINAL_ENV": "local",
        }

        # Forward common provider environment variables from Harbor extra_env or
        # the adapter process environment. This is the key difference from
        # Harbor's built-in Hermes implementation.
        self._copy_env(
            env,
            [
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_TOKEN",
                "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "OPENROUTER_API_KEY",
                "OPENROUTER_BASE_URL",
                "GLM_API_KEY",
                "ZAI_API_KEY",
                "Z_AI_API_KEY",
                "KIMI_API_KEY",
                "MINIMAX_API_KEY",
                "MINIMAX_CN_API_KEY",
            ],
        )

        hermes_provider_flag: str | None = None
        use_native = False

        native_info = _NATIVE_PROVIDERS.get(provider)
        if native_info:
            native_flag, key_names, base_url_names = native_info
            for key_name in key_names:
                key_val = self._get_env(key_name) or env.get(key_name)
                if key_val:
                    env[key_name] = key_val
                    hermes_provider_flag = native_flag
                    use_native = True
                    break
            for base_url_name in base_url_names:
                base_url = self._get_env(base_url_name) or env.get(base_url_name)
                if base_url:
                    env[base_url_name] = base_url

        if not use_native:
            openrouter_key = self._get_env("OPENROUTER_API_KEY") or env.get("OPENROUTER_API_KEY")
            if not openrouter_key:
                if native_info:
                    key_hint = " or ".join(native_info[1])
                    raise ValueError(f"No API key found. Set {key_hint} or OPENROUTER_API_KEY.")
                raise ValueError("No API key found. Set OPENROUTER_API_KEY.")
            env["OPENROUTER_API_KEY"] = openrouter_key

        # Native providers use bare model name with explicit Hermes provider.
        # OpenRouter-style fallback keeps the full provider/model string.
        cli_model = model if hermes_provider_flag else self.model_name
        config_yaml = self._build_config_yaml(cli_model)
        env["HARBOR_INSTRUCTION"] = instruction

        await self.exec_as_agent(
            environment,
            command=(
                "mkdir -p /tmp/hermes && "
                f"cat > /tmp/hermes/config.yaml << 'EOF'\n{config_yaml}EOF"
            ),
            env=env,
            timeout_sec=10,
        )

        mcp_command = self._build_register_mcp_servers_command()
        if mcp_command:
            await self.exec_as_agent(environment, command=mcp_command, env=env, timeout_sec=10)

        skills_command = self._build_register_skills_command()
        if skills_command:
            await self.exec_as_agent(environment, command=skills_command, env=env, timeout_sec=10)

        cli_parts = [
            'export PATH="/usr/local/bin:$HOME/.hermes/bin:$HOME/.local/bin:$PATH"',
            "hermes --yolo chat",
            '-q "$HARBOR_INSTRUCTION"',
            "-Q",
            f"--model {shlex.quote(cli_model)}",
        ]
        if hermes_provider_flag:
            cli_parts.append(f"--provider {shlex.quote(hermes_provider_flag)}")
        toolsets_flag = self._resolved_flags.get("toolsets")
        if toolsets_flag:
            cli_parts.append(f"--toolsets {shlex.quote(str(toolsets_flag))}")

        run_cmd = (
            f"{cli_parts[0]} && "
            f"{' '.join(cli_parts[1:])} "
            "2>&1 | stdbuf -oL tee /logs/agent/hermes.txt"
        )

        try:
            await self.exec_as_agent(environment, command=run_cmd, env=env)
        finally:
            try:
                await self.exec_as_agent(
                    environment,
                    command=(
                        'export PATH="$HOME/.local/bin:$PATH" && '
                        "hermes sessions export /logs/agent/hermes-session.jsonl "
                        "--source cli 2>/dev/null || true"
                    ),
                    env={"HERMES_HOME": "/tmp/hermes"},
                    timeout_sec=30,
                )
            except Exception:
                pass
