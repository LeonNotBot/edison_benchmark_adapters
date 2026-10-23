"""Validate every selected TB4 environment before any trial is submitted.

Uses Harbor's resolved selection and cached task definitions. Task containers,
agents and verifiers are never started. GPU environments additionally probe
Docker through the GPU patch's disposable diagnostic container.
"""
import json


REPORT_NAME = "resource-preflight.json"
MARKER = "EDISON_RESOURCE_PREFLIGHT_FAILED"


def check_job_resources(job):
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.config import TaskConfig
    from harbor.models.task.paths import TaskPaths
    from harbor.models.task.verifier_mode import resolve_effective_verifier_env_config
    from harbor.models.trial.paths import TrialPaths

    rows = []
    seen = set()
    for trial in job._remaining_trial_configs:
        task_id = trial.task.get_task_id()
        key = (task_id, trial.environment.model_dump_json(), trial.verifier.disable)
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
            rows.append({"task": name, "phase": "definition", "error": str(exc)})
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
            row = {"task": name, "phase": phase, "resources": resources, "error": None}
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
    report = {"status": "failed" if failures else "passed", "checked_tasks": len(seen),
              "checked_environments": len(rows), "checks": rows}
    report_path = job.job_dir / REPORT_NAME
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        details = "\n".join(f"- {row['task']} [{row['phase']}]: {row['error']}" for row in failures)
        raise RuntimeError(
            f"{MARKER}: 本批次资源预检未通过，尚未启动任何新题目。\n{details}\n"
            f"请调整选题或配置支持所需资源的执行环境。报告：{report_path}"
        )
    print(f"[tb4-adapter] resource preflight passed: tasks={len(seen)} environments={len(rows)} report={report_path}", flush=True)
    return report


def patch_job_resource_preflight():
    from harbor.job import Job

    original = Job._run_trials_with_queue
    if getattr(original, "_edison_resource_preflight", False):
        return

    async def run(self, *args, **kwargs):
        check_job_resources(self)
        return await original(self, *args, **kwargs)

    run._edison_resource_preflight = True
    Job._run_trials_with_queue = run
