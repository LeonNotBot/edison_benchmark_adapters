"""Validate every selected TB4 environment before any trial is submitted.

Uses Harbor's resolved selection and cached task definitions. Task containers,
agents and verifiers are never started. GPU environments additionally probe
Docker through the GPU patch's disposable diagnostic container.
"""
import json


REPORT_NAME = "resource-preflight.json"
MARKER = "EDISON_RESOURCE_PREFLIGHT_FAILED"


def _check_key(trial):
    return (trial.task.get_task_id(), trial.environment.model_dump_json(), trial.verifier.disable)


def check_job_resources(job, *, raise_on_failure=True):
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.paths import TaskPaths
    from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
    from harbor.models.trial.paths import TrialPaths

    rows = []
    seen = set()
    names_by_key = {}
    for trial in job._remaining_trial_configs:
        names_by_key.setdefault(_check_key(trial), []).append(trial.trial_name)
    for trial in job._remaining_trial_configs:
        task_id = trial.task.get_task_id()
        key = _check_key(trial)
        if key in seen:
            continue
        seen.add(key)
        name = trial.task.name or str(trial.task.path or task_id)
        try:
            paths = TaskPaths(job._task_download_results[task_id].path)
            task = TaskConfig.model_validate_toml(paths.config_path.read_text())
            if task.task:
                name = task.task.name
            environments = [("agent", task.environment, paths.environment_dir)]
            if not trial.verifier.disable:
                for step in task.steps or [None]:
                    verifier_env = resolve_effective_verifier_env_config(task, step)
                    if verifier_env is not None:
                        context = paths.tests_dir
                        if step and paths.step_tests_dir(step.name).exists():
                            context = paths.step_tests_dir(step.name)
                        environments.append(("verifier" + (f":{step.name}" if step else ""), verifier_env, context))
        except Exception as exc:
            rows.append({"task": name, "trial_names": names_by_key[key], "phase": "definition", "error": str(exc)})
            continue

        for phase, definition, context in environments:
            definition = definition.model_copy(deep=True)
            runtime = trial.environment.model_copy(deep=True)
            if phase != "agent":
                runtime.extra_docker_compose = []
            resources = {k: getattr(definition, k) for k in ("cpus", "memory_mb", "storage_mb", "gpus")}
            for field in resources:
                override = getattr(runtime, "override_" + field)
                if override is not None:
                    resources[field] = override
            row = {"task": name, "trial_names": names_by_key[key], "phase": phase, "resources": resources, "error": None}
            try:
                # This adapter uses Docker. Do not instantiate arbitrary cloud
                # providers with possible remote side effects during preflight.
                if runtime.type.value != "docker" or runtime.import_path:
                    raise RuntimeError("TB4 resource preflight currently requires the built-in Docker environment")
                environment = EnvironmentFactory.create_environment_from_config(
                    config=runtime,
                    environment_dir=context,
                    environment_name="edison-resource-preflight",
                    session_id="edison-resource-preflight",
                    trial_paths=TrialPaths(job.job_dir / "resource-preflight"),
                    task_env_config=definition,
                )
                if resources["gpus"] and resources["gpus"] > 0:
                    row["gpu_devices"] = getattr(environment, "_edison_gpu_devices", [])
                    if job.config.n_concurrent_trials != 1:
                        raise RuntimeError("GPU 批次目前要求 Concurrency=1，避免多个题目争用同一 GPU")
            except Exception as exc:
                row["error"] = str(exc)
            rows.append(row)

    failures = [row for row in rows if row["error"]]
    blocked = {name for row in failures for name in row["trial_names"]}
    report = {"status": "failed" if failures else "passed", "checked_tasks": len(seen),
              "policy": "continue_supported_trials", "skipped_trials": len(blocked),
              "runnable_trials": len(job._remaining_trial_configs) - len(blocked),
              "checked_environments": len(rows), "checks": rows}
    report_path = job.job_dir / REPORT_NAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if failures and raise_on_failure:
        details = "\n".join(f"- {row['task']} [{row['phase']}]: {row['error']}" for row in failures)
        raise RuntimeError(
            f"{MARKER}: 本批次资源预检未通过，尚未启动任何新题目。\n{details}\n"
            f"请调整选题或配置支持所需资源的执行环境。报告：{report_path}"
        )
    print(f"[tb4-adapter] resource preflight: tasks={len(seen)} skipped={len(blocked)} "
          f"runnable={report['runnable_trials']} report={report_path}", flush=True)
    return report


async def record_unavailable_trial(job, config, failures):
    """Persist a normal Harbor exception result and fire END exactly once.

    No agent is constructed, no model is called, and no reward is fabricated.
    Keeping config/lock/result permits normal progress accounting and resume.
    """
    from datetime import datetime
    from harbor.models.job.lock import build_trial_lock
    from harbor.models.trial.paths import TrialPaths
    from harbor.models.trial.result import TrialResult, AgentInfo, ModelInfo, ExceptionInfo
    from harbor.trial.hooks import TrialEvent, TrialHookEvent

    lock = build_trial_lock(trial_config=config,
                            task_download_result=job._task_download_results[config.task.get_task_id()])
    paths = TrialPaths(config.trials_dir / config.trial_name)
    paths.trial_dir.mkdir(parents=True, exist_ok=True)
    message = "资源预检未通过，本题未执行；继续其他题目。\n" + "\n".join(
        f"[{row['phase']}] {row['error']}" for row in failures)
    now = datetime.now()
    model_name = config.agent.model_name
    provider, model_name = model_name.split("/", 1) if model_name and "/" in model_name else (None, model_name)
    result = TrialResult(
        task_name=failures[0]["task"], trial_name=config.trial_name,
        trial_uri=paths.trial_dir.resolve().as_uri(), task_id=config.task.get_task_id(),
        task_checksum=lock.task.digest.removeprefix("sha256:"), config=config,
        source=config.task.source, started_at=now, finished_at=now,
        agent_info=AgentInfo(name=config.agent.name or config.agent.import_path or "unknown", version="not-started",
            model_info=ModelInfo(name=model_name, provider=provider) if model_name else None),
        exception_info=ExceptionInfo(exception_type="ResourcePreflightError", exception_message=message,
            exception_traceback="", occurred_at=now),
    )
    paths.config_path.write_text(config.model_dump_json(indent=2, exclude_defaults=True))
    paths.lock_path.write_text(lock.model_dump_json(indent=2, exclude_none=True))
    paths.log_path.write_text(message + "\n")
    paths.exception_message_path.write_text(message + "\n")
    paths.result_path.write_text(result.model_dump_json(indent=2))
    event = TrialHookEvent(event=TrialEvent.END, task_name=result.task_name, config=config, result=result, lock=lock)
    for hook in job._trial_queue._hooks[TrialEvent.END]:
        await hook(event)
    print(f"[tb4-adapter] skipped resource-unavailable trial={config.trial_name}: {message}", flush=True)
    return result


def patch_job_resource_preflight():
    from harbor.job import Job

    original = Job._run_trials_with_queue
    if getattr(original, "_edison_resource_preflight", False):
        return

    async def run(self, *args, **kwargs):
        report = check_job_resources(self, raise_on_failure=False)
        failures = {}
        for row in report["checks"]:
            if row["error"]:
                for name in row["trial_names"]:
                    failures.setdefault(name, []).append(row)
        queue = self._trial_queue
        execute = queue._execute_trial_with_retries

        async def execute_or_skip(config):
            if config.trial_name in failures:
                # Skip before Trial.create: Harbor's constructor exceptions
                # otherwise escape its retry loop and cancel the TaskGroup.
                return await record_unavailable_trial(self, config, failures[config.trial_name])
            return await execute(config)

        queue._execute_trial_with_retries = execute_or_skip
        try:
            return await original(self, *args, **kwargs)
        finally:
            queue._execute_trial_with_retries = execute

    run._edison_resource_preflight = True
    Job._run_trials_with_queue = run
