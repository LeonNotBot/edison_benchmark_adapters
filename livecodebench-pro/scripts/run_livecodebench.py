#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ADAPTER_NAME = "livecodebench-pro"
BENCHMARK_NAME = "livecodebench_pro"
DEFAULT_SPLIT = "quater_2024_10_12"
EDISON_DEPLOYMENT_PREFIXES = {"moon", "sky"}


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


def truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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


def inspect_model_id(model: dict[str, Any]) -> str:
    model_id = str(model.get("model_identifier") or "").strip()
    provider = normalize_provider(
        str(model.get("normalized_provider") or model.get("provider") or "")
    )
    if not model_id:
        raise ValueError("input.json model.model_identifier is required")
    bare_model = api_model_id(model_id, provider)
    if provider:
        return f"{provider}/{bare_model}"
    return model_id


def parse_csv_or_lines(value: Any) -> list[str]:
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

    items: list[str] = []
    for item in raw_items:
        text = item.strip()
        if text and text not in items:
            items.append(text)
    return items


def env_value(*names: str | None) -> str:
    for name in names:
        if not name:
            continue
        value = os.environ.get(str(name))
        if value:
            return value
    return ""


def resolve_endpoint(value: Any) -> str:
    raw = str(value or "").strip()
    if raw and "://" not in raw and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw):
        return os.environ.get(raw, "").strip()
    return raw


def post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else {"data": data}


def model_preflight(model: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    provider = normalize_provider(
        str(model.get("normalized_provider") or model.get("provider") or "")
    )
    raw_model = str(model.get("model_identifier") or "").strip()
    request_model = api_model_id(raw_model, provider)
    endpoint = resolve_endpoint(
        model.get("api_endpoint") or os.environ.get("EDISON_MODEL_API_ENDPOINT")
    ).rstrip("/")
    timeout = float(params.get("model_preflight_timeout_seconds") or 60)
    started = time.monotonic()

    if not provider:
        raise RuntimeError("model preflight failed: missing provider")
    if not raw_model:
        raise RuntimeError("model preflight failed: missing model_identifier")
    if not endpoint:
        raise RuntimeError("model preflight failed: missing api_endpoint")

    api_key_env = str(model.get("api_key_env") or "").strip()
    if provider == "anthropic":
        api_key = env_value(
            api_key_env,
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "EDISON_MODEL_API_KEY",
        )
        if not api_key:
            raise RuntimeError("model preflight failed: missing ANTHROPIC_API_KEY")
        base = re.sub(r"/v1$", "", endpoint)
        data = post_json(
            f"{base}/v1/messages",
            {
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            {
                "model": request_model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "Reply exactly: OK"}],
            },
            timeout,
        )
    elif provider in {"openai", "openrouter"}:
        if provider == "openrouter":
            api_key = env_value(api_key_env, "OPENROUTER_API_KEY", "EDISON_MODEL_API_KEY")
        else:
            api_key = env_value(api_key_env, "OPENAI_API_KEY", "EDISON_MODEL_API_KEY")
        if not api_key:
            raise RuntimeError(f"model preflight failed: missing {provider.upper()} API key")
        base = endpoint if endpoint.endswith("/v1") else f"{endpoint}/v1"
        data = post_json(
            f"{base}/chat/completions",
            {
                "content-type": "application/json",
                "authorization": f"Bearer {api_key}",
            },
            {
                "model": request_model,
                "messages": [{"role": "user", "content": "Reply exactly: OK"}],
                "max_tokens": 8,
                "temperature": 0,
            },
            timeout,
        )
    else:
        raise RuntimeError(f"model preflight unsupported provider: {provider}")

    return {
        "provider": provider,
        "model": request_model,
        "raw_model": raw_model,
        "duration_seconds": round(time.monotonic() - started, 3),
        "response_keys": sorted(data.keys()),
    }


def prepare_inspect_env(
    model: dict[str, Any],
    inspect_evals_dir: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    provider = normalize_provider(
        str(model.get("normalized_provider") or model.get("provider") or "")
    )
    endpoint = resolve_endpoint(
        model.get("api_endpoint") or os.environ.get("EDISON_MODEL_API_ENDPOINT")
    ).rstrip("/")
    api_key_env = str(model.get("api_key_env") or "").strip()

    if provider == "anthropic":
        api_key = env_value(
            api_key_env,
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "EDISON_MODEL_API_KEY",
        )
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
        if endpoint:
            env["ANTHROPIC_BASE_URL"] = re.sub(r"/v1$", "", endpoint)
    elif provider == "openai":
        api_key = env_value(api_key_env, "OPENAI_API_KEY", "EDISON_MODEL_API_KEY")
        if api_key:
            env["OPENAI_API_KEY"] = api_key
        if endpoint:
            env["OPENAI_BASE_URL"] = endpoint if endpoint.endswith("/v1") else f"{endpoint}/v1"
    elif provider == "openrouter":
        api_key = env_value(api_key_env, "OPENROUTER_API_KEY", "EDISON_MODEL_API_KEY")
        if api_key:
            env["OPENROUTER_API_KEY"] = api_key

    env["PYTHONPATH"] = (
        str(inspect_evals_dir / "src")
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )
    return env


def compact_json_text(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text[:limit]


def sample_error_from_inspect(sample: dict[str, Any]) -> Any:
    return sample.get("error") or sample.get("exception") or sample.get("message")


def sample_error_message(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message") or error.get("exception_message") or error.get("type")
        if message:
            return str(message)
    if error:
        return str(error).splitlines()[0][:500]
    return ""


def sample_score_from_inspect(sample: dict[str, Any]) -> float | None:
    scorer = (sample.get("scores") or {}).get("livecodebench_pro_scorer") or {}
    value = scorer.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is not None:
        text_value = str(value).strip().lower()
        if text_value in {"c", "correct", "true", "pass", "passed"}:
            return 1.0
        if text_value in {"i", "incorrect", "false", "fail", "failed"}:
            return 0.0
        if text_value in {"p", "partial", "partially_correct"}:
            return 0.5
    if sample_error_from_inspect(sample):
        return 0.0
    return None


def overall_score_from_inspect(log_data: dict[str, Any], samples: list[dict[str, Any]]) -> float | None:
    for score_entry in (log_data.get("results") or {}).get("scores") or []:
        if score_entry.get("name") != "livecodebench_pro_scorer":
            continue
        accuracy = (score_entry.get("metrics") or {}).get("accuracy", 0)
        if isinstance(accuracy, dict):
            accuracy = accuracy.get("value", 0)
        if isinstance(accuracy, (int, float)) and not isinstance(accuracy, bool):
            return float(accuracy)

    sample_scores = [
        s["score"]
        for s in samples
        if isinstance(s.get("score"), (int, float)) and not isinstance(s.get("score"), bool)
    ]
    if not sample_scores:
        return None
    return sum(sample_scores) / len(sample_scores)


def normalize_samples(log_data: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, sample in enumerate(log_data.get("samples") or [], start=1):
        if not isinstance(sample, dict):
            continue
        sample_id = str(sample.get("id") or sample.get("sample_id") or index)
        sample_error = sample_error_from_inspect(sample)
        score = sample_score_from_inspect(sample)
        status = "error" if sample_error else ("completed" if score is not None else "error")
        output: Any = sample.get("target") or sample.get("output")
        if sample_error:
            output = {
                "message": sample_error_message(sample_error) or "Inspect sample failed before scoring",
                "error": sample_error,
            }
        normalized.append(
            {
                "id": sample_id,
                "name": sample_id,
                "status": status,
                "score": score,
                "reward": score,
                "output": compact_json_text(output),
                "latency_seconds": None,
                "metrics": {
                    "source": BENCHMARK_NAME,
                    "inspect_sample": sample,
                    "scoring_method": "inspect_error_as_zero" if sample_error else "livecodebench_pro_scorer",
                    "judge_reason": sample_error_message(sample_error) if sample_error else "",
                },
                "error": compact_json_text(sample_error) if sample_error else None,
            }
        )
    return normalized


def latest_eval_log(log_dir: Path) -> Path:
    eval_logs = sorted(
        log_dir.glob("*livecodebench-pro*.eval"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not eval_logs:
        raise RuntimeError(f"No livecodebench eval log found under {log_dir}")
    return eval_logs[0]


def write_error_result(
    result_json: Path,
    model: dict[str, Any],
    error: str,
    metrics: dict[str, Any] | None = None,
) -> None:
    write_json(
        result_json,
        {
            "protocol_version": "edison.external_benchmark.v1",
            "adapter": ADAPTER_NAME,
            "benchmark": BENCHMARK_NAME,
            "suite": BENCHMARK_NAME,
            "model": model.get("model_identifier") or "",
            "status": "error",
            "score": None,
            "pass_rate": None,
            "sample_count": 0,
            "samples": [],
            "metrics": metrics or {},
            "error": error,
            "created_at": now_iso(),
        },
    )


def build_inspect_command(
    inspect_bin: Path,
    inspect_task: Path,
    model_name: str,
    benchmark: dict[str, Any],
    params: dict[str, Any],
    log_dir: Path,
) -> list[str]:
    limit = int(benchmark.get("limit") or params.get("limit", 1))
    sample_ids = parse_csv_or_lines(
        benchmark.get("task_names")
        or benchmark.get("task_name")
        or benchmark.get("tasks")
        or params.get("sample_ids")
        or params.get("sample_id")
        or params.get("task_names")
        or params.get("task_name")
        or params.get("tasks")
    )

    command = [
        str(inspect_bin),
        "eval",
        f"{inspect_task}@livecodebench_pro",
        "--model",
        model_name,
        "-T",
        f"split={params.get('split') or benchmark.get('split') or DEFAULT_SPLIT}",
        "--log-dir",
        str(log_dir),
    ]

    if sample_ids:
        command.extend(["--sample-id", ",".join(sample_ids)])
    else:
        command.extend(["--limit", str(limit)])

    difficulty = params.get("difficulty") or benchmark.get("difficulty")
    if difficulty:
        command.extend(["-T", f"difficulty={difficulty}"])

    max_samples = params.get("max_samples")
    if max_samples not in (None, ""):
        command.extend(["--max-samples", str(max_samples)])

    max_connections = params.get("max_connections")
    if max_connections not in (None, ""):
        command.extend(["--max-connections", str(max_connections)])

    timeout = params.get("inspect_timeout_seconds") or params.get("timeout_seconds")
    if timeout not in (None, ""):
        command.extend(["--timeout", str(timeout)])

    if truthy(params.get("no_fail_on_error"), True):
        command.append("--no-fail-on-error")

    if truthy(params.get("continue_on_fail"), True):
        command.append("--continue-on-fail")

    return command


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

    output_dir = expand_path(str(paths.get("output_dir") or "./output"))
    result_json = expand_path(str(paths.get("result_json") or output_dir / "result.json"))
    log_dir = expand_path(str(params.get("log_dir") or output_dir / "logs"))
    output_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    if truthy(params.get("model_preflight"), True):
        try:
            preflight = model_preflight(model, params)
            print(
                "[livecodebench-pro-adapter] model preflight ok: "
                f"provider={preflight['provider']} "
                f"model={preflight['model']} "
                f"raw_model={preflight['raw_model']} "
                f"duration={preflight['duration_seconds']}s",
                flush=True,
            )
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            write_error_result(
                result_json,
                model,
                f"Model preflight failed before Inspect run: {exc}",
                {"preflight_stage": "model_api"},
            )
            print(f"[livecodebench-pro-adapter] model preflight failed: {exc}", file=sys.stderr, flush=True)
            return 3

    try:
        model_name = inspect_model_id(model)
        inspect_evals_dir = Path(os.environ.get("INSPECT_EVALS_DIR", "/inspect_evals")).expanduser()
        inspect_bin = Path(
            os.environ.get("INSPECT_BIN", str(inspect_evals_dir / ".venv" / "bin" / "inspect"))
        ).expanduser()
        inspect_task = inspect_evals_dir / "src" / "inspect_evals" / "livecodebench_pro" / "livecodebench_pro.py"

        if not inspect_bin.is_file() or not os.access(inspect_bin, os.X_OK):
            raise RuntimeError(f"inspect cli not found or not executable: {inspect_bin}")
        if not inspect_task.is_file():
            raise RuntimeError(f"livecodebench task file not found: {inspect_task}")

        command = build_inspect_command(
            inspect_bin=inspect_bin,
            inspect_task=inspect_task,
            model_name=model_name,
            benchmark=benchmark,
            params=params,
            log_dir=log_dir,
        )

        print("[livecodebench-pro-adapter] command:", " ".join(command), flush=True)

        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            env=prepare_inspect_env(model, inspect_evals_dir),
        )

        if result.returncode != 0:
            raise RuntimeError(f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}")

        eval_file = latest_eval_log(log_dir)
        dump_file = eval_file.with_suffix(".json")
        with dump_file.open("w", encoding="utf-8") as f:
            subprocess.run(
                [str(inspect_bin), "log", "dump", str(eval_file)],
                stdout=f,
                text=True,
                check=True,
            )

        log_data = load_json(dump_file)
        samples = normalize_samples(log_data)
        score = overall_score_from_inspect(log_data, samples)

        output = {
            "protocol_version": "edison.external_benchmark.v1",
            "adapter": ADAPTER_NAME,
            "benchmark": BENCHMARK_NAME,
            "suite": benchmark.get("suite") or BENCHMARK_NAME,
            "agent": benchmark.get("agent") or "model",
            "model": model.get("model_identifier") or "",
            "run_id": eval_file.stem,
            "status": "completed",
            "score": score,
            "pass_rate": score,
            "sample_count": len(samples),
            "metrics": {
                "inspect_log": str(eval_file),
                "inspect_log_json": str(dump_file),
                "inspect_returncode": result.returncode,
                "inspect_stdout_tail": result.stdout[-8000:],
                "inspect_stderr_tail": result.stderr[-8000:],
            },
            "samples": samples,
            "error": None,
            "created_at": now_iso(),
        }

        write_json(result_json, output)
        print(f"[livecodebench-pro-adapter] wrote Edison result: {result_json}", flush=True)
        return 0

    except Exception as exc:
        write_error_result(result_json, model, str(exc))
        print(f"[livecodebench-pro-adapter] error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
