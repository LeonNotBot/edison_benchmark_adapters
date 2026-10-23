import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("resource_preflight", ROOT / "scripts/harbor_patches/resource_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def make_job(tmp_path, definitions):
    from harbor.models.trial.config import TrialConfig

    trials, downloads = [], {}
    for name, definition in definitions.items():
        task_dir = tmp_path / name
        task_dir.mkdir()
        (task_dir / "task.toml").write_text(definition)
        for directory in ("environment", "tests"):
            (task_dir / directory).mkdir()
            (task_dir / directory / "Dockerfile").write_text("FROM scratch\n")
        trial = TrialConfig(task={"path": task_dir}, agent={"name": "oracle"})
        trials.append(trial)
        downloads[trial.task.get_task_id()] = SimpleNamespace(path=task_dir)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    return SimpleNamespace(_remaining_trial_configs=trials, _task_download_results=downloads, job_dir=job_dir)


@pytest.fixture(autouse=True)
def never_start_containers(monkeypatch):
    from harbor.environments.docker.docker import DockerEnvironment

    async def forbidden(*args, **kwargs):
        pytest.fail("Resource preflight must not start containers")
    monkeypatch.setattr(DockerEnvironment, "start", forbidden)


def test_gpu_at_end_blocks_entire_queue_and_reports_all_failures(tmp_path, monkeypatch):
    from harbor.job import Job

    job = make_job(tmp_path, {"cpu-first": "", "gpu-last": "[environment]\ngpus=1\n",
                              "gpu-too": "[environment]\ngpus=2\n"})
    dispatched = []
    async def dispatch(*args, **kwargs):
        dispatched.append(True)
    monkeypatch.setattr(Job, "_run_trials_with_queue", dispatch)
    preflight.patch_job_resource_preflight()
    with pytest.raises(RuntimeError, match=preflight.MARKER) as exc:
        asyncio.run(Job._run_trials_with_queue(job))
    assert not dispatched
    assert "gpu-last" in str(exc.value) and "gpu-too" in str(exc.value)
    report = json.loads((job.job_dir / preflight.REPORT_NAME).read_text())
    assert report["checked_tasks"] == 3
    assert sum(bool(row["error"]) for row in report["checks"]) == 2


def test_cpu_only_selection_proceeds_and_does_not_scan_unselected_tasks(tmp_path, monkeypatch):
    from harbor.job import Job

    job = make_job(tmp_path, {"cpu": "", "unselected-gpu": "[environment]\ngpus=1\n"})
    job._remaining_trial_configs = [job._remaining_trial_configs[0]] * 2
    dispatched = []
    async def dispatch(*args, **kwargs):
        dispatched.append(True)
        return "started"
    monkeypatch.setattr(Job, "_run_trials_with_queue", dispatch)
    preflight.patch_job_resource_preflight()
    assert asyncio.run(Job._run_trials_with_queue(job)) == "started"
    assert dispatched == [True]
    report = json.loads((job.job_dir / preflight.REPORT_NAME).read_text())
    assert report["status"] == "passed" and report["checked_tasks"] == 1


def test_separate_verifier_gpu_checked_before_agent_starts(tmp_path):
    job = make_job(tmp_path, {"gpu-verifier": '[verifier.environment]\ngpus=1\n'})
    with pytest.raises(RuntimeError, match=r"\[verifier\]"):
        preflight.check_job_resources(job)
    report = json.loads((job.job_dir / preflight.REPORT_NAME).read_text())
    assert not report["checks"][0]["error"]
    assert report["checks"][1]["resources"]["gpus"] == 1


def test_disabled_verifier_and_runtime_resource_overrides(tmp_path):
    job = make_job(tmp_path, {"cpu": '[verifier.environment]\ngpus=1\n'})
    trial = job._remaining_trial_configs[0]
    trial.verifier.disable = True
    assert preflight.check_job_resources(job)["status"] == "passed"
    trial.environment.override_gpus = 1
    with pytest.raises(RuntimeError, match=r"\[agent\]"):
        preflight.check_job_resources(job)


def test_malformed_task_is_not_silently_skipped(tmp_path):
    job = make_job(tmp_path, {"broken": "not-valid-toml"})
    with pytest.raises(RuntimeError, match=r"\[definition\]"):
        preflight.check_job_resources(job)
