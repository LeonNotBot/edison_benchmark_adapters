"""Resource failures must be persisted without cancelling subsequent trials."""
import asyncio
from datetime import datetime
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load("skip_preflight", ROOT / "scripts/harbor_patches/resource_preflight.py")
adapter = load("skip_adapter", ROOT / "scripts/run_tb2.py")
helpers = load("skip_helpers", ROOT / "tests/test_resource_preflight.py")


@pytest.mark.parametrize("only_skips,attempts", [(False, 1), (True, 1), (False, 2)])
def test_real_job_queue_persistence_stats_resume_and_normalization(tmp_path, monkeypatch, only_skips, attempts):
    from harbor.job import Job
    from harbor.metrics.mean import Mean
    from harbor.models.job.config import JobConfig
    from harbor.models.job.lock import build_trial_lock
    from harbor.models.trial.paths import TrialPaths
    from harbor.models.trial.result import TrialResult, AgentInfo
    from harbor.trial.hooks import TrialEvent, TrialHookEvent
    from harbor.trial.trial import Trial
    import harbor.job as job_module

    definitions = {"gpu-first": "[environment]\ngpus=1\n"}
    if not only_skips:
        definitions["cpu-after"] = ""
    fixture = helpers.make_job(tmp_path, definitions)
    tasks = [trial.task for trial in fixture._remaining_trial_configs]
    config = JobConfig(job_name="mixed", jobs_dir=tmp_path / "jobs", tasks=tasks,
                       agents=[{"name": "oracle"}], n_concurrent_trials=1, n_attempts=attempts, quiet=True)
    monkeypatch.setattr(job_module, "capture_job_finished_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(job_module, "record_command_job_id", lambda *args: None)
    monkeypatch.setattr(Job, "_run_trials_with_queue", Job._run_trials_with_queue)
    preflight.patch_job_resource_preflight()
    executed = []

    class FakeCPUTrial:
        def __init__(self, trial_config):
            self.config = trial_config
            self.hooks = {event: [] for event in TrialEvent}

        def add_hook(self, event, hook):
            self.hooks[event].append(hook)

        async def run(self):
            c = self.config
            paths = TrialPaths(c.trials_dir / c.trial_name)
            paths.trial_dir.mkdir(parents=True, exist_ok=True)
            lock = build_trial_lock(trial_config=c, task_download_result=fixture._task_download_results[c.task.get_task_id()])
            result = TrialResult(task_name="cpu-after", trial_name=c.trial_name, config=c,
                task_id=c.task.get_task_id(), task_checksum=lock.task.digest.removeprefix("sha256:"),
                trial_uri=paths.trial_dir.resolve().as_uri(), agent_info=AgentInfo(name="oracle", version="test"),
                source=c.task.source, verifier_result={"rewards": {"reward": 1.0}},
                started_at=datetime.now(), finished_at=datetime.now())
            paths.config_path.write_text(c.model_dump_json())
            paths.result_path.write_text(result.model_dump_json())
            paths.lock_path.write_text(lock.model_dump_json())
            for event in (TrialEvent.START, TrialEvent.END):
                for hook in self.hooks[event]:
                    await hook(TrialHookEvent(event=event, config=c, task_name="cpu-after", result=result, lock=lock))
            return result

    async def create(cls, c):
        assert c.task.path.name == "cpu-after", "Unsupported GPU task reached Trial.create"
        executed.append(c.trial_name)
        return FakeCPUTrial(c)
    monkeypatch.setattr(Trial, "create", classmethod(create))

    def new_job():
        return Job(config=config, _task_configs=tasks, _metrics={"adhoc": [Mean()]},
                   _task_download_results=fixture._task_download_results)

    job = new_job()
    result = asyncio.run(job.run())
    assert result.finished_at is not None
    assert len(result.trial_results) == len(definitions) * attempts
    skipped = [r for r in result.trial_results if r.exception_info]
    assert len(skipped) == attempts
    assert all(r.exception_info.exception_type == "ResourcePreflightError" for r in skipped)
    assert all(r.verifier_result is None for r in skipped)
    assert len(executed) == (0 if only_skips else attempts)
    assert result.stats.n_errored_trials == attempts
    for r in skipped:
        directory = job.job_dir / r.trial_name
        assert "继续其他题目" in (directory / "exception.txt").read_text()
        assert (directory / "lock.json").exists()
    normalized = adapter.normalize_harbor_result(edison_input={}, harbor_result=result.model_dump(mode="json"),
        harbor_result_path=job.job_dir / "result.json", stdout="", stderr="", returncode=0)
    assert normalized["score"] == (None if only_skips else 1.0)
    assert normalized["status"] == "completed_with_errors"
    assert normalized["metrics"]["resource_skipped_samples"] == attempts
    assert normalized["metrics"]["coverage"] == (0 if only_skips else 0.5)
    assert len({(s["id"], s["run_index"]) for s in normalized["samples"]}) == len(result.trial_results)
    assert all(s["score"] is None for s in normalized["samples"] if s["metrics"]["resource_skipped"])
    # Resume keeps the existing skips and successes, with no duplicate rows.
    resumed = new_job()
    assert not resumed._remaining_trial_configs
    again = asyncio.run(resumed.run())
    assert len(again.trial_results) == len(result.trial_results)
    assert len(executed) == (0 if only_skips else attempts)


def test_cancellation_is_not_converted_to_resource_skip(tmp_path, monkeypatch):
    from harbor.job import Job

    job = helpers.make_job(tmp_path, {"cpu": ""})
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(Job, "_run_trials_with_queue", cancelled)
    original_execute = job._trial_queue._execute_trial_with_retries
    preflight.patch_job_resource_preflight()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(Job._run_trials_with_queue(job))
    assert job._trial_queue._execute_trial_with_retries == original_execute
