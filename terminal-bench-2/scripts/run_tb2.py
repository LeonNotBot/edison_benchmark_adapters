#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DATASET = "terminal-bench/terminal-bench-2"
SUPPORTED_AGENTS = {"oracle", "hermes", "terminus-2"}
ADAPTER_DIR = Path(__file__).resolve().parents[1]
HERMES_CONTAINER_PREFLIGHT_DATASET = ADAPTER_DIR / "smoke_tasks"
HERMES_CONTAINER_PREFLIGHT_TASK_NAME = "edison/hermes-container-preflight"
HERMES_CONTAINER_PREFLIGHT_LOCAL_TASK_FILTER = "hermes-container-preflight"
EDISON_DEPLOYMENT_PREFIXES = {"moon", "sky"}
EDISON_HERMES_IMPORT_PATH = "scripts.edison_hermes_agent:EdisonHermes"


def expand_path(value: str | Path) -> Path:
    return Path(str(value)).expanduser().resolve()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"JSON top-level must be an object: {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_provider(provider: str) -> str:
    provider = (provider or "").strip().lower()
    aliases = {
        "moon": "openai",
        "openai-api": "openai",
        "sky": "anthropic",
        "claude": "anthropic",
    }
    return aliases.get(provider, provider)


def strip_edison_deployment_prefix(model_identifier: str, provider: str) -> str:
    model_identifier = (model_identifier or "").strip()
    provider = normalize_provider(provider)
    if "/" not in model_identifier:
        return model_identifier
    prefix, rest = model_identifier.split("/", 1)
    normalized_prefix = normalize_provider(prefix)
    if prefix.lower() in EDISON_DEPLOYMENT_PREFIXES and normalized_prefix == provider:
        return rest
    return model_identifier


def api_model_id(model_identifier: str, provider: str) -> str:
    model_identifier = strip_edison_deployment_prefix(model_identifier, provider)
    provider = normalize_provider(provider)
    if provider and model_identifier.startswith(f"{provider}/"):
        return model_identifier.split("/", 1)[1]
    return model_identifier


def normalize_harbor_model(model: dict[str, Any]) -> str:
    model_id = str(model.get("model_identifier") or "").strip()
    provider = normalize_provider(str(model.get("normalized_provider") or model.get("provider") or ""))
    if not model_id:
        raise ValueError("input.json model.model_identifier is required")
    model_id = strip_edison_deployment_prefix(model_id, provider)
    if provider and model_id.startswith(f"{provider}/"):
        return model_id
    if provider:
        return f"{provider}/{api_model_id(model_id, provider)}"
    return model_id


def normalize_hermes_harbor_model(model: dict[str, Any]) -> str:
    """Return the model identifier Harbor should pass to EdisonHermes.

    Edison routes some deployments via friendly provider aliases:
    ``moon/gpt-5.4`` and ``sky/anthropic/claude-sonnet-4-6``. The model API
    preflight already strips those correctly. For Harbor + Hermes we also need
    the provider prefix to match Hermes' CLI provider names, especially
    ``openai-api`` for OpenAI-compatible endpoints.
    """
    model_id = str(model.get("model_identifier") or "").strip()
    provider = normalize_provider(str(model.get("normalized_provider") or model.get("provider") or ""))
    if not model_id:
        raise ValueError("input.json model.model_identifier is required")
    bare_model = api_model_id(model_id, provider)
    if provider == "openai":
        return f"openai-api/{bare_model}"
    if provider == "anthropic":
        return f"anthropic/{bare_model}"
    if provider == "openrouter":
        return f"openrouter/{bare_model}"
    if provider:
        return f"{provider}/{bare_model}"
    return model_id


def env_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def resolve_env_reference(value: Any) -> str:
    """Resolve a config value that may be a literal or an environment variable name.

    Edison passes real endpoints in normal runs. For local/worker self-tests,
    examples may use values like ``ANTHROPIC_BASE_URL`` or ``${ANTHROPIC_BASE_URL}``
    so secrets and deployment-specific URLs can stay in the shell environment.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = re.fullmatch(r"\$\{([^}]+)\}", raw) or re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", raw)
    if match:
        return os.environ.get(match.group(1), "")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw) and os.environ.get(raw):
        return os.environ[raw]
    return raw


def model_endpoint(model: dict[str, Any]) -> str:
    endpoint = resolve_env_reference(model.get("api_endpoint"))
    return endpoint or os.environ.get("EDISON_MODEL_API_ENDPOINT", "")


def model_api_key(model: dict[str, Any], *fallback_env_names: str) -> str:
    explicit_env_name = str(model.get("api_key_env") or "").strip()
    names = [explicit_env_name] if explicit_env_name else []
    names.extend(fallback_env_names)
    names.append("EDISON_MODEL_API_KEY")
    return env_value(*names)


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_task_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(re.split(r"[\n,]+", str(item)))
    else:
        raw_items = [str(value)]
    names: list[str] = []
    for item in raw_items:
        name = item.strip()
        if not name:
            continue
        if name not in names:
            names.append(name)
    return names


def is_local_dataset_ref(dataset: str) -> bool:
    dataset_path = Path(dataset).expanduser()
    return (
        dataset_path.exists()
        or dataset.startswith("/")
        or dataset.startswith("./")
        or dataset.startswith("../")
        or dataset.startswith("~")
    )


def harbor_dataset_config(dataset: str, task_names: list[str], limit: int) -> dict[str, Any]:
    if is_local_dataset_ref(dataset):
        entry: dict[str, Any] = {"path": dataset}
        normalized_task_names = task_names
    else:
        entry = {"name": dataset}
        dataset_org = dataset.split("/", 1)[0] if "/" in dataset else ""
        normalized_task_names = [
            task_name if "/" in task_name or not dataset_org else f"{dataset_org}/{task_name}"
            for task_name in task_names
        ]
    if normalized_task_names:
        entry["task_names"] = normalized_task_names
    entry["n_tasks"] = limit
    return entry


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else {"data": data}


def model_preflight(model: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    provider = normalize_provider(str(model.get("normalized_provider") or model.get("provider") or ""))
    model_identifier = str(model.get("model_identifier") or "").strip()
    request_model = api_model_id(model_identifier, provider)
    endpoint = model_endpoint(model).strip().rstrip("/")
    api_key = model_api_key(model)
    timeout = float(params.get("model_preflight_timeout_seconds") or 30)
    started = time.monotonic()

    if not provider:
        raise RuntimeError("model preflight failed: missing provider")
    if not model_identifier:
        raise RuntimeError("model preflight failed: missing model_identifier")
    if not endpoint:
        raise RuntimeError("model preflight failed: missing api_endpoint")

    if provider == "anthropic":
        api_key = model_api_key(model, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            raise RuntimeError("model preflight failed: missing ANTHROPIC_API_KEY")
        base = re.sub(r"/v1$", "", endpoint)
        payload = {
            "model": request_model,
            "max_tokens": 8,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
        data = post_json(
            f"{base}/v1/messages",
            {
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            payload,
            timeout,
        )
    elif provider in {"openai", "openrouter"}:
        if provider == "openrouter":
            api_key = model_api_key(model, "OPENROUTER_API_KEY")
        else:
            api_key = model_api_key(model, "OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(f"model preflight failed: missing {provider.upper()} API key")
        base = endpoint.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        payload = {
            "model": request_model,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
            "max_tokens": 8,
            "temperature": 0,
        }
        data = post_json(
            f"{base}/chat/completions",
            {
                "content-type": "application/json",
                "authorization": f"Bearer {api_key}",
            },
            payload,
            timeout,
        )
    else:
        raise RuntimeError(f"model preflight unsupported provider: {provider}")

    return {
        "ok": True,
        "provider": provider,
        "model": request_model,
        "raw_model": model_identifier,
        "harbor_model": normalize_hermes_harbor_model(model),
        "duration_seconds": round(time.monotonic() - started, 3),
        "response_keys": sorted(data.keys())[:20],
    }


def append_model_env(command: list[str], model: dict[str, Any]) -> None:
    provider = normalize_provider(str(model.get("normalized_provider") or model.get("provider") or ""))
    endpoint = model_endpoint(model).strip()

    if provider == "anthropic":
        api_key = model_api_key(model, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        if api_key:
            command.extend(["--ae", f"ANTHROPIC_API_KEY={api_key}"])
        if endpoint:
            endpoint = re.sub(r"/v1$", "", endpoint.rstrip("/"))
            command.extend(["--ae", f"ANTHROPIC_BASE_URL={endpoint}"])
    elif provider == "openrouter":
        api_key = model_api_key(model, "OPENROUTER_API_KEY")
        if api_key:
            command.extend(["--ae", f"OPENROUTER_API_KEY={api_key}"])
        if endpoint:
            command.extend(["--ae", f"OPENROUTER_BASE_URL={endpoint}"])
    elif provider == "openai":
        api_key = model_api_key(model, "OPENAI_API_KEY")
        if api_key:
            command.extend(["--ae", f"OPENAI_API_KEY={api_key}"])
        if endpoint:
            command.extend(["--ae", f"OPENAI_BASE_URL={endpoint}"])
    elif provider:
        prefix = provider.upper().replace("-", "_")
        api_key = model_api_key(model, f"{prefix}_API_KEY")
        if api_key:
            command.extend(["--ae", f"{prefix}_API_KEY={api_key}"])
        if endpoint:
            command.extend(["--ae", f"{prefix}_BASE_URL={endpoint}"])


def append_common_harbor_options(command: list[str], *, params: dict[str, Any], agent: str) -> None:
    if bool(params.get("no_delete", True)):
        command.append("--no-delete")

    timeout_multiplier = params.get("timeout_multiplier")
    if timeout_multiplier is not None:
        command.extend(["--timeout-multiplier", str(timeout_multiplier)])

    agent_multiplier = params.get("agent_timeout_multiplier", 2 if agent == "hermes" else None)
    if agent_multiplier is not None:
        command.extend(["--agent-timeout-multiplier", str(agent_multiplier)])

    verifier_multiplier = params.get("verifier_timeout_multiplier")
    if verifier_multiplier is not None:
        command.extend(["--verifier-timeout-multiplier", str(verifier_multiplier)])

    setup_multiplier = params.get("agent_setup_timeout_multiplier", 10 if agent == "hermes" else None)
    if setup_multiplier is not None:
        command.extend(["--agent-setup-timeout-multiplier", str(setup_multiplier)])

    environment_build_multiplier = params.get("environment_build_timeout_multiplier")
    if environment_build_multiplier is not None:
        command.extend(["--environment-build-timeout-multiplier", str(environment_build_multiplier)])

    if truthy(params.get("install_only"), False):
        command.append("--install-only")

    if truthy(params.get("debug"), False):
        command.append("--debug")

    for item in params.get("extra_args") or []:
        command.append(str(item))


def model_env_templates(model: dict[str, Any]) -> dict[str, str]:
    """Return Harbor config env entries that resolve real secrets from the adapter process env."""
    provider = normalize_provider(str(model.get("normalized_provider") or model.get("provider") or ""))
    endpoint = model_endpoint(model).strip()
    env: dict[str, str] = {}

    if provider == "anthropic":
        if model_api_key(model, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            env["ANTHROPIC_API_KEY"] = "${ANTHROPIC_API_KEY}"
            env["ANTHROPIC_TOKEN"] = "${ANTHROPIC_API_KEY}"
        if env_value("ANTHROPIC_AUTH_TOKEN"):
            env["ANTHROPIC_AUTH_TOKEN"] = "${ANTHROPIC_AUTH_TOKEN}"
        if endpoint:
            env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1$", "", endpoint.rstrip("/"))
    elif provider == "openrouter":
        if model_api_key(model, "OPENROUTER_API_KEY"):
            env["OPENROUTER_API_KEY"] = "${OPENROUTER_API_KEY}"
        if endpoint:
            env["OPENROUTER_BASE_URL"] = endpoint
    elif provider == "openai":
        if model_api_key(model, "OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = "${OPENAI_API_KEY}"
        if endpoint:
            env["OPENAI_BASE_URL"] = endpoint
            env["OPENAI_API_BASE"] = endpoint
    elif provider:
        prefix = provider.upper().replace("-", "_")
        if model_api_key(model, f"{prefix}_API_KEY"):
            env[f"{prefix}_API_KEY"] = f"${{{prefix}_API_KEY}}"
        if endpoint:
            env[f"{prefix}_BASE_URL"] = endpoint

    return env


def harbor_agent_config(agent: str, model: dict[str, Any]) -> dict[str, Any]:
    if agent == "oracle":
        return {"name": "oracle"}
    if agent == "hermes":
        return {
            "import_path": EDISON_HERMES_IMPORT_PATH,
            "model_name": normalize_hermes_harbor_model(model),
            "env": model_env_templates(model),
        }
    return {
        "name": agent,
        "model_name": normalize_harbor_model(model),
        "env": model_env_templates(model),
    }


def build_harbor_command(config: dict[str, Any]) -> tuple[list[str], Path]:
    benchmark = config.get("benchmark") if isinstance(config.get("benchmark"), dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
    params = benchmark.get("params") if isinstance(benchmark.get("params"), dict) else {}

    agent = str(benchmark.get("agent") or "hermes").strip().lower()
    if agent not in SUPPORTED_AGENTS:
        raise ValueError(
            f"Terminal-Bench 2 adapter currently supports {sorted(SUPPORTED_AGENTS)}, got {agent!r}. "
            "CCB/OpenClaw should be added here once their Harbor agent names and env contracts are confirmed."
        )

    dataset = str(benchmark.get("suite") or DEFAULT_DATASET).strip() or DEFAULT_DATASET
    limit = int(benchmark.get("limit") or 1)
    runs = int(benchmark.get("runs") or 1)
    task_names = parse_task_names(
        benchmark.get("task_names")
        or benchmark.get("task_name")
        or benchmark.get("tasks")
        or params.get("task_names")
        or params.get("task_name")
        or params.get("tasks")
    )
    output_dir = expand_path(str(paths.get("output_dir") or params.get("jobs_dir") or "~/data/edison_external_benchmarks/terminal-bench-2"))
    harbor_jobs_dir = output_dir / "harbor_jobs"
    harbor_jobs_dir.mkdir(parents=True, exist_ok=True)

    harbor_bin = str(params.get("harbor_bin") or os.environ.get("EXTERNAL_BENCHMARK_HARBOR_BIN") or os.environ.get("HARBOR_BIN") or "harbor")
    if "/" not in harbor_bin:
        resolved = shutil.which(harbor_bin)
        if resolved:
            harbor_bin = resolved

    if task_names or agent == "hermes":
        harbor_config = {
            "jobs_dir": str(harbor_jobs_dir),
            "n_concurrent_trials": runs,
            "timeout_multiplier": float(params.get("timeout_multiplier") or 1),
            "agent_timeout_multiplier": float(params.get("agent_timeout_multiplier") or (2 if agent == "hermes" else 1)),
            "verifier_timeout_multiplier": (
                float(params["verifier_timeout_multiplier"])
                if params.get("verifier_timeout_multiplier") is not None
                else None
            ),
            "agent_setup_timeout_multiplier": float(params.get("agent_setup_timeout_multiplier") or (10 if agent == "hermes" else 1)),
            "environment_build_timeout_multiplier": (
                float(params["environment_build_timeout_multiplier"])
                if params.get("environment_build_timeout_multiplier") is not None
                else None
            ),
            "environment": {
                "type": "docker",
                "delete": not bool(params.get("no_delete", True)),
            },
            "agents": [harbor_agent_config(agent, model)],
            "datasets": [harbor_dataset_config(dataset, task_names, limit)],
        }
        harbor_config = {k: v for k, v in harbor_config.items() if v is not None}
        config_path = output_dir / "harbor_task_config.json"
        write_json(config_path, harbor_config)
        command = [harbor_bin, "run", "--config", str(config_path)]
        if truthy(params.get("debug"), False):
            command.append("--debug")
        for item in params.get("extra_args") or []:
            command.append(str(item))
        return command, harbor_jobs_dir

    command = [
        harbor_bin,
        "run",
        "-d",
        dataset,
        "-a",
        agent,
        "-l",
        str(limit),
        "-n",
        str(runs),
        "--jobs-dir",
        str(harbor_jobs_dir),
    ]

    if agent != "oracle":
        command.extend(["-m", normalize_harbor_model(model)])
        append_model_env(command, model)

    append_common_harbor_options(command, params=params, agent=agent)

    return command, harbor_jobs_dir


def resolve_harbor_bin(params: dict[str, Any]) -> str:
    harbor_bin = str(params.get("harbor_bin") or os.environ.get("EXTERNAL_BENCHMARK_HARBOR_BIN") or os.environ.get("HARBOR_BIN") or "harbor")
    if "/" not in harbor_bin:
        resolved = shutil.which(harbor_bin)
        if resolved:
            harbor_bin = resolved
    return harbor_bin


def build_hermes_container_preflight_config(config: dict[str, Any], jobs_dir: Path) -> dict[str, Any]:
    benchmark = config.get("benchmark") if isinstance(config.get("benchmark"), dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    params = benchmark.get("params") if isinstance(benchmark.get("params"), dict) else {}
    model_name = normalize_hermes_harbor_model(model)

    return {
        "job_name": f"edison-hermes-container-preflight-{int(time.time())}",
        "jobs_dir": str(jobs_dir),
        "n_concurrent_trials": 1,
        "agent_setup_timeout_multiplier": float(params.get("container_preflight_agent_setup_timeout_multiplier") or params.get("agent_setup_timeout_multiplier") or 10),
        "agent_timeout_multiplier": float(params.get("container_preflight_agent_timeout_multiplier") or 2),
        "verifier_timeout_multiplier": float(params.get("container_preflight_verifier_timeout_multiplier") or 1),
        "environment": {
            "type": "docker",
            "delete": not bool(params.get("no_delete", True)),
        },
        "agents": [{
            "import_path": EDISON_HERMES_IMPORT_PATH,
            "model_name": model_name,
            "env": model_env_templates(model),
        }],
        "datasets": [{
            "path": str(HERMES_CONTAINER_PREFLIGHT_DATASET),
            "task_names": [HERMES_CONTAINER_PREFLIGHT_LOCAL_TASK_FILTER],
            "n_tasks": 1,
        }],
    }


def read_harbor_score(harbor_result_path: Path) -> float | None:
    try:
        harbor_result = load_json(harbor_result_path)
    except Exception:
        return None
    trial_files = sorted(
        p for p in harbor_result_path.parent.glob("*/result.json")
        if p.parent != harbor_result_path.parent
    )
    scores: list[float] = []
    for trial_file in trial_files:
        sample = normalize_trial_result(trial_file)
        if isinstance(sample.get("score"), (int, float)):
            scores.append(float(sample["score"]))
    if scores:
        return sum(scores) / len(scores)
    return metric_mean(harbor_result)


def classify_container_preflight_failure(
    *,
    jobs_dir: Path,
    stdout: str,
    stderr: str,
    returncode: int,
    score: float | None,
) -> str:
    combined = "\n".join([stderr, stdout])
    latest_job = latest_job_dir(jobs_dir)
    agent_tail = ""
    trial_tail = ""
    if latest_job:
        trial_dirs = sorted(
            [p for p in latest_job.iterdir() if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if trial_dirs:
            trial_dir = trial_dirs[0]
            agent_tail = read_text(trial_dir / "agent" / "hermes.txt", 3000)
            trial_tail = read_text(trial_dir / "trial.log", 3000)

    lower = combined.lower()
    if "no tasks matched the filter" in lower:
        stage = "容器内预检任务配置错误"
        hint = "Harbor 还没有启动 Docker；本地 smoke dataset 的 task_names 过滤条件没有匹配到任务目录。"
    elif "agenttimeouterror" in lower or "agent execution timed out" in lower:
        stage = "Hermes 容器内执行超时"
        hint = "Harbor/Docker/Hermes 已进入 agent 执行阶段，但 Hermes 没能在预检超时内完成 OK 文件创建。"
    elif "apt-get" in lower or "install.sh" in lower or "agent setup" in lower:
        stage = "Hermes 容器内安装/初始化失败"
        hint = "Harbor/Docker 已启动，但 Hermes 安装或初始化阶段失败；通常和容器网络、apt、curl、pip 或 GitHub 拉取有关。"
    elif "docker" in lower or "compose" in lower or "environment" in lower:
        stage = "Harbor/Docker 环境启动失败"
        hint = "Harbor 未能成功创建或启动 Docker trial 环境。"
    elif score is not None and score < 1:
        stage = "Hermes 容器内模型调用或任务执行未通过"
        hint = "Hermes 进了容器，但没有成功创建内容为 OK 的预检文件；可能是模型无响应、Hermes 卡住、工具调用失败或指令未完成。"
    else:
        stage = "容器内 Hermes 预检失败"
        hint = "未能明确归类，请查看 Harbor job.log、trial.log 和 agent/hermes.txt。"

    tails = []
    if agent_tail:
        tails.append(f"\n[agent/hermes.txt tail]\n{agent_tail}")
    if trial_tail:
        tails.append(f"\n[trial.log tail]\n{trial_tail}")
    return (
        f"{stage}。{hint} returncode={returncode}, score={score}. "
        f"{progress_hint(jobs_dir)}"
        f"{''.join(tails)}"
    )


def run_hermes_container_preflight(edison_input: dict[str, Any], result_json: Path) -> None:
    benchmark = edison_input.get("benchmark") if isinstance(edison_input.get("benchmark"), dict) else {}
    paths = edison_input.get("paths") if isinstance(edison_input.get("paths"), dict) else {}
    params = benchmark.get("params") if isinstance(benchmark.get("params"), dict) else {}
    output_dir = expand_path(str(paths.get("output_dir") or params.get("jobs_dir") or "~/data/edison_external_benchmarks/terminal-bench-2"))
    preflight_jobs_dir = output_dir / "container_preflight_jobs"
    preflight_jobs_dir.mkdir(parents=True, exist_ok=True)
    preflight_config_path = output_dir / "container_preflight_config.json"
    preflight_config = build_hermes_container_preflight_config(edison_input, preflight_jobs_dir)
    write_json(preflight_config_path, preflight_config)

    command = [
        resolve_harbor_bin(params),
        "run",
        "--config",
        str(preflight_config_path),
    ]
    print(
        "[tb2-adapter] container preflight command:",
        " ".join(shlex_quote(part) for part in redact_command(command)),
        flush=True,
    )
    returncode, stdout, stderr = run_harbor_streaming(
        command,
        preflight_jobs_dir,
        heartbeat_seconds=int(params.get("container_preflight_heartbeat_seconds") or 30),
    )
    harbor_result_path = latest_result_file(preflight_jobs_dir)
    score = read_harbor_score(harbor_result_path) if harbor_result_path else None
    if returncode == 0 and score == 1:
        print(
            "[tb2-adapter] container preflight ok: "
            f"agent=hermes score={score} {progress_hint(preflight_jobs_dir)}",
            flush=True,
        )
        return

    error = classify_container_preflight_failure(
        jobs_dir=preflight_jobs_dir,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        score=score,
    )
    write_json(result_json, {
        "protocol_version": "edison.external_benchmark.v1",
        "adapter": "terminal-bench-2",
        "benchmark": benchmark.get("suite") or DEFAULT_DATASET,
        "agent": "hermes",
        "status": "error",
        "score": None,
        "sample_count": 0,
        "samples": [],
        "error": f"Container Hermes preflight failed before formal TB2 run: {error}",
        "metrics": {
            "preflight_stage": "harbor_container_agent_preflight",
            "preflight_config_path": str(preflight_config_path),
            "preflight_jobs_dir": str(preflight_jobs_dir),
            "preflight_result_path": str(harbor_result_path) if harbor_result_path else None,
            "preflight_returncode": returncode,
            "preflight_score": score,
            "preflight_stdout_tail": stdout[-8000:],
            "preflight_stderr_tail": stderr[-8000:],
        },
        "created_at": now_iso(),
    })
    print(f"[tb2-adapter] container preflight failed: {error}", file=sys.stderr, flush=True)
    raise RuntimeError(error)


def latest_result_file(jobs_dir: Path) -> Path | None:
    candidates = sorted(jobs_dir.glob("*/result.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def latest_job_dir(jobs_dir: Path) -> Path | None:
    candidates = sorted(
        (p for p in jobs_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if jobs_dir.exists() else []
    return candidates[0] if candidates else None


def progress_hint(jobs_dir: Path) -> str:
    job_dir = latest_job_dir(jobs_dir)
    if not job_dir:
        return f"jobs_dir={jobs_dir} latest_job=waiting"
    trial_results = len([
        path for path in job_dir.glob("*/result.json")
        if path.parent != job_dir
    ])
    final_result = (job_dir / "result.json").exists()
    return (
        f"jobs_dir={jobs_dir} latest_job={job_dir.name} "
        f"trial_results={trial_results} final_result={'yes' if final_result else 'no'}"
    )


def harbor_progress(jobs_dir: Path) -> dict[str, Any]:
    job_dir = latest_job_dir(jobs_dir)
    if not job_dir:
        return {
            "source": "terminal-bench-2",
            "stage": "waiting_for_harbor_job",
            "current": 0,
            "total": 0,
            "job_dir": None,
            "running_trials": [],
            "updated_at": now_iso(),
        }

    result_path = job_dir / "result.json"
    stats: dict[str, Any] = {}
    total = 0
    finished_at = None
    if result_path.exists():
        try:
            result = load_json(result_path)
            total = int(result.get("n_total_trials") or 0)
            finished_at = result.get("finished_at")
            raw_stats = result.get("stats")
            stats = raw_stats if isinstance(raw_stats, dict) else {}
        except Exception:
            stats = {}

    completed = int(stats.get("n_completed_trials") or 0)
    errored = int(stats.get("n_errored_trials") or 0)
    cancelled = int(stats.get("n_cancelled_trials") or 0)
    running = int(stats.get("n_running_trials") or 0)
    pending = int(stats.get("n_pending_trials") or 0)
    current = completed + errored + cancelled

    running_trials: list[str] = []
    completed_trials: list[str] = []
    errored_trials: list[str] = []
    for trial_dir in sorted(p for p in job_dir.iterdir() if p.is_dir() and "__" in p.name):
        trial_result_path = trial_dir / "result.json"
        if not trial_result_path.exists():
            running_trials.append(trial_dir.name)
            continue
        try:
            trial = load_json(trial_result_path)
            if trial.get("exception_info"):
                errored_trials.append(trial_dir.name)
            else:
                completed_trials.append(trial_dir.name)
        except Exception:
            completed_trials.append(trial_dir.name)

    return {
        "source": "terminal-bench-2",
        "stage": "finished" if finished_at else "running",
        "current": current,
        "total": total,
        "completed": completed,
        "errored": errored,
        "cancelled": cancelled,
        "running": running,
        "pending": pending,
        "job_name": job_dir.name,
        "job_dir": str(job_dir),
        "running_trials": running_trials,
        "completed_trials": completed_trials[-10:],
        "errored_trials": errored_trials[-10:],
        "updated_at": now_iso(),
    }


def write_progress(progress_path: Path | None, jobs_dir: Path) -> None:
    if not progress_path:
        return
    try:
        write_json(progress_path, harbor_progress(jobs_dir))
    except Exception:
        pass


def _stream_reader(stream: Any, label: str, output_queue: "queue.Queue[tuple[str, str]]") -> None:
    try:
        for line in iter(stream.readline, ""):
            if not line:
                break
            output_queue.put((label, line))
    finally:
        try:
            stream.close()
        except Exception:
            pass


def harbor_subprocess_env() -> dict[str, str]:
    """Return environment for the Harbor child process.

    Harbor imports custom agents from ``import_path`` in the generated config.
    When this adapter is called by Edison, the current working directory is not
    guaranteed to be the adapter root, so make the adapter package importable
    explicitly for both local single-machine and remote worker deployments.
    """
    env = os.environ.copy()
    adapter_root = str(ADAPTER_DIR)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{adapter_root}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else adapter_root
    )
    return env


def run_harbor_streaming(
    command: list[str],
    jobs_dir: Path,
    heartbeat_seconds: int = 30,
    progress_path: Path | None = None,
) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        env=harbor_subprocess_env(),
    )
    output_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
    threads = [
        threading.Thread(target=_stream_reader, args=(proc.stdout, "stdout", output_queue), daemon=True),
        threading.Thread(target=_stream_reader, args=(proc.stderr, "stderr", output_queue), daemon=True),
    ]
    for thread in threads:
        thread.start()

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    started = time.monotonic()
    next_heartbeat = started + heartbeat_seconds

    def consume_queued_output() -> None:
        while True:
            try:
                label, text = output_queue.get_nowait()
            except queue.Empty:
                break
            if label == "stderr":
                stderr_chunks.append(text)
                print(text, end="", file=sys.stderr, flush=True)
            else:
                stdout_chunks.append(text)
                print(text, end="", flush=True)

    while True:
        consume_queued_output()
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if now >= next_heartbeat:
            elapsed = int(now - started)
            write_progress(progress_path, jobs_dir)
            print(
                f"[tb2-adapter] heartbeat elapsed={elapsed}s {progress_hint(jobs_dir)}",
                file=sys.stderr,
                flush=True,
            )
            next_heartbeat = now + heartbeat_seconds
        time.sleep(1)

    returncode = proc.wait()
    for thread in threads:
        thread.join(timeout=2)
    consume_queued_output()
    write_progress(progress_path, jobs_dir)
    return returncode, "".join(stdout_chunks), "".join(stderr_chunks)


def seconds_between(started: str | None, finished: str | None) -> float | None:
    if not started or not finished:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end = datetime.fromisoformat(finished.replace("Z", "+00:00"))
        return max(0.0, (end - start).total_seconds())
    except Exception:
        return None


def metric_mean(harbor_result: dict[str, Any]) -> float | None:
    stats = harbor_result.get("stats") if isinstance(harbor_result.get("stats"), dict) else {}
    evals = stats.get("evals") if isinstance(stats.get("evals"), dict) else {}
    for eval_data in evals.values():
        if not isinstance(eval_data, dict):
            continue
        metrics = eval_data.get("metrics")
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if isinstance(metric, dict) and isinstance(metric.get("mean"), (int, float)):
                return float(metric["mean"])
    return None


def read_text(path: Path, limit: int = 12000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def normalize_trial_result(trial_file: Path) -> dict[str, Any]:
    trial = load_json(trial_file)
    verifier = trial.get("verifier_result") if isinstance(trial.get("verifier_result"), dict) else {}
    rewards = verifier.get("rewards") if isinstance(verifier.get("rewards"), dict) else {}
    reward = rewards.get("reward")
    if not isinstance(reward, (int, float)):
        reward_file = trial_file.parent / "verifier" / "reward.txt"
        try:
            reward = float(reward_file.read_text(encoding="utf-8").strip())
        except Exception:
            reward = None

    agent_result = trial.get("agent_result") if isinstance(trial.get("agent_result"), dict) else {}
    exception_info = trial.get("exception_info") if isinstance(trial.get("exception_info"), dict) else None
    if reward is None and exception_info:
        reward = 0.0
    duration = seconds_between(trial.get("started_at"), trial.get("finished_at"))
    output = (
        agent_result.get("message")
        or agent_result.get("output")
        or read_text(trial_file.parent / "agent" / "oracle.txt")
        or read_text(trial_file.parent / "trial.log")
    )
    task_id = normalize_task_id(trial.get("task_id"), trial.get("task_name") or trial_file.parent.name)

    return {
        "id": task_id,
        "name": trial.get("task_name") or trial_file.parent.name,
        "status": "error" if exception_info else "completed",
        "score": reward,
        "reward": reward,
        "output": output,
        "latency_seconds": duration,
        "metrics": {
            "trial_name": trial.get("trial_name"),
            "source": trial.get("source"),
            "agent_info": trial.get("agent_info"),
            "verifier_result": verifier,
        },
        "error": json.dumps(exception_info, ensure_ascii=False, default=str) if exception_info else None,
    }


def normalize_task_id(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "id", "ref"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return str(fallback)


def normalize_harbor_result(
    *,
    edison_input: dict[str, Any],
    harbor_result: dict[str, Any],
    harbor_result_path: Path,
    stdout: str,
    stderr: str,
    returncode: int,
) -> dict[str, Any]:
    benchmark = edison_input.get("benchmark") if isinstance(edison_input.get("benchmark"), dict) else {}
    model = edison_input.get("model") if isinstance(edison_input.get("model"), dict) else {}
    job_dir = harbor_result_path.parent
    trial_files = sorted(
        p for p in job_dir.glob("*/result.json")
        if p.parent != job_dir
    )
    samples = [normalize_trial_result(path) for path in trial_files]
    scores = [sample["score"] for sample in samples if isinstance(sample.get("score"), (int, float))]
    mean_score = sum(scores) / len(scores) if scores else metric_mean(harbor_result)
    status = "completed" if returncode == 0 and not any(sample.get("status") == "error" for sample in samples) else "error"
    duration = seconds_between(harbor_result.get("started_at"), harbor_result.get("finished_at"))
    return {
        "protocol_version": "edison.external_benchmark.v1",
        "adapter": benchmark.get("adapter") or "terminal-bench-2",
        "benchmark": benchmark.get("suite") or DEFAULT_DATASET,
        "suite": benchmark.get("suite") or DEFAULT_DATASET,
        "agent": benchmark.get("agent") or "",
        "model": model.get("model_identifier") or "",
        "run_id": harbor_result.get("id") or job_dir.name,
        "status": status,
        "score": mean_score,
        "pass_rate": mean_score,
        "sample_count": len(samples) or int(harbor_result.get("n_total_trials") or 0),
        "metrics": {
            "duration_seconds": duration,
            "harbor_result_path": str(harbor_result_path),
            "harbor_returncode": returncode,
            "harbor_stdout_tail": stdout[-8000:],
            "harbor_stderr_tail": stderr[-8000:],
            "harbor_stats": harbor_result.get("stats"),
        },
        "samples": samples,
        "error": stderr[-4000:] if returncode != 0 else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    config_path = expand_path(args.config)
    edison_input = load_json(config_path)
    paths = edison_input.get("paths") if isinstance(edison_input.get("paths"), dict) else {}
    benchmark = edison_input.get("benchmark") if isinstance(edison_input.get("benchmark"), dict) else {}
    params = benchmark.get("params") if isinstance(benchmark.get("params"), dict) else {}
    model = edison_input.get("model") if isinstance(edison_input.get("model"), dict) else {}
    result_json = expand_path(str(paths.get("result_json") or "result.json"))
    progress_json = expand_path(str(paths["progress_json"])) if paths.get("progress_json") else None

    try:
        command, harbor_jobs_dir = build_harbor_command(edison_input)
    except Exception as exc:
        write_json(result_json, {
            "protocol_version": "edison.external_benchmark.v1",
            "adapter": "terminal-bench-2",
            "benchmark": DEFAULT_DATASET,
            "status": "error",
            "score": None,
            "sample_count": 0,
            "samples": [],
            "error": str(exc),
            "created_at": now_iso(),
        })
        print(f"[tb2-adapter] config error: {exc}", file=sys.stderr)
        return 2

    agent = str(benchmark.get("agent") or "hermes").strip().lower()
    if agent != "oracle" and truthy(params.get("model_preflight"), True):
        try:
            preflight = model_preflight(model, params)
            print(
                "[tb2-adapter] model preflight ok: "
                f"provider={preflight['provider']} model={preflight['model']} "
                f"raw_model={preflight['raw_model']} harbor_model={preflight['harbor_model']} "
                f"duration={preflight['duration_seconds']}s",
                flush=True,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            write_json(result_json, {
                "protocol_version": "edison.external_benchmark.v1",
                "adapter": "terminal-bench-2",
                "benchmark": benchmark.get("suite") or DEFAULT_DATASET,
                "agent": agent,
                "model": model.get("model_identifier") or "",
                "status": "error",
                "score": None,
                "sample_count": 0,
                "samples": [],
                "error": f"Model preflight failed before Harbor run: {exc}",
                "metrics": {
                    "preflight_stage": "model_api",
                    "provider": normalize_provider(str(model.get("normalized_provider") or model.get("provider") or "")),
                    "raw_model": model.get("model_identifier") or "",
                    "model": api_model_id(
                        str(model.get("model_identifier") or ""),
                        str(model.get("normalized_provider") or model.get("provider") or ""),
                    ),
                    "harbor_model": normalize_harbor_model(model) if model.get("model_identifier") else "",
                },
                "created_at": now_iso(),
            })
            print(f"[tb2-adapter] model preflight failed: {exc}", file=sys.stderr, flush=True)
            return 3

    if agent == "hermes" and truthy(params.get("container_agent_preflight"), False):
        try:
            run_hermes_container_preflight(edison_input, result_json)
        except Exception:
            return 4

    print("[tb2-adapter] command:", " ".join(shlex_quote(part) for part in redact_command(command)), flush=True)
    write_progress(progress_json, harbor_jobs_dir)
    returncode, stdout, stderr = run_harbor_streaming(command, harbor_jobs_dir, progress_path=progress_json)

    harbor_result_path = latest_result_file(harbor_jobs_dir)
    if not harbor_result_path:
        write_json(result_json, {
            "protocol_version": "edison.external_benchmark.v1",
            "adapter": "terminal-bench-2",
            "benchmark": DEFAULT_DATASET,
            "status": "error",
            "score": None,
            "sample_count": 0,
            "samples": [],
            "error": f"Harbor did not produce result.json under {harbor_jobs_dir}",
            "metrics": {
                "harbor_returncode": returncode,
                "harbor_stdout_tail": stdout[-8000:],
                "harbor_stderr_tail": stderr[-8000:],
            },
            "created_at": now_iso(),
        })
        return returncode or 1

    harbor_result = load_json(harbor_result_path)
    normalized = normalize_harbor_result(
        edison_input=edison_input,
        harbor_result=harbor_result,
        harbor_result_path=harbor_result_path,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )
    write_json(result_json, normalized)
    print(f"[tb2-adapter] wrote Edison result: {result_json}", flush=True)
    return returncode


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    for part in command:
        if re.match(r"^[A-Z0-9_]*(?:KEY|TOKEN|SECRET|AUTH)[A-Z0-9_]*=", part):
            name = part.split("=", 1)[0]
            redacted.append(f"{name}=***")
        else:
            redacted.append(part)
    return redacted


if __name__ == "__main__":
    raise SystemExit(main())
