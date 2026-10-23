"""Real Harbor/Compose integration, with mocked hardware (no paid/model runs)."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gpu = load("docker_gpu_test", ROOT / "scripts/harbor_patches/docker_gpu.py")
adapter = load("gpu_adapter", ROOT / "scripts/run_tb2.py")
preflight = load("gpu_preflight", ROOT / "scripts/harbor_patches/resource_preflight.py")
helpers = load("gpu_test_helpers", ROOT / "tests/test_resource_preflight.py")
DEVICES = [{"uuid": "GPU-test-4070", "name": "NVIDIA GeForce RTX 4070 Laptop GPU", "memory_mb": 8188}]


@pytest.fixture
def patched(monkeypatch):
    from harbor.environments.docker.docker import DockerEnvironment as Docker
    # Restore monkeypatched Harbor after each test, including inherited methods.
    for name in ("capabilities", "_validate_gpu_support", "_write_env_compose_file"):
        monkeypatch.setattr(Docker, name, getattr(Docker, name))
    monkeypatch.setattr(Docker, "_edison_gpu_patch", False, raising=False)
    monkeypatch.setattr(gpu, "docker_gpu_inventory", lambda: DEVICES)
    gpu.patch_docker_gpu()
    gpu.patch_docker_gpu()  # idempotence
    return Docker


def environment(tmp_path, count=1, types=None, task_os="linux", **runtime):
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.trial.config import EnvironmentConfig as Runtime
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    tmp_path.mkdir(parents=True, exist_ok=True)
    return EnvironmentFactory.create_environment_from_config(
        config=Runtime(**runtime), environment_dir=tmp_path,
        environment_name="gpu-regression", session_id="gpu-regression",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04", gpus=count, gpu_types=types, os=task_os),
    )


def test_probe_uses_daemon_and_networkless_local_image_and_cleans_up(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "info":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"OSType": "linux", "Runtimes": {"nvidia": {}}}))
        return SimpleNamespace(returncode=0, stdout="GPU-test-4070, NVIDIA GeForce RTX 4070 Laptop GPU, 8188\n")
    monkeypatch.setattr(gpu.subprocess, "run", run)
    gpu._probe_result.cache_clear()
    assert gpu.docker_gpu_inventory() == DEVICES
    assert gpu.docker_gpu_inventory() == DEVICES
    assert len(calls) == 3
    command = calls[1]
    assert "--pull=never" in command and "--network=none" in command
    assert command[command.index("--gpus") + 1] == "all"
    assert calls[2][-1] == command[command.index("--name") + 1]
    gpu._probe_result.cache_clear()


@pytest.mark.parametrize("mode", ["runtime-missing", "timeout", "invalid-output", "image-missing"])
def test_probe_failures_are_cached_and_timeout_cleaned_up(monkeypatch, mode):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "info":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"OSType": "linux", "Runtimes": {} if mode == "runtime-missing" else {"nvidia": {}}}))
        if command[1] == "run" and mode == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if command[1] == "run" and mode == "image-missing":
            return SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        return SimpleNamespace(returncode=0, stdout="bad inventory")
    monkeypatch.setattr(gpu.subprocess, "run", run)
    gpu._probe_result.cache_clear()
    for _ in range(2):
        with pytest.raises(RuntimeError):
            gpu.docker_gpu_inventory()
    assert sum(c[1] == "info" for c in calls) == 1
    if mode != "runtime-missing":
        assert calls[-1][1:3] == ["rm", "-f"]
    gpu._probe_result.cache_clear()


def test_cpu_does_not_probe_or_receive_gpu(patched, monkeypatch, tmp_path):
    monkeypatch.setattr(gpu, "docker_gpu_inventory", lambda: pytest.fail("CPU task probed GPU"))
    env = environment(tmp_path, count=0)
    path = env._write_env_compose_file()
    assert "devices" not in path.read_text()
    env._cleanup_env_compose_file()


@pytest.mark.parametrize("count,types", [(2, None), (1, ["A100"]), (-1, None)])
def test_unavailable_gpu_requirements_rejected(patched, tmp_path, count, types):
    with pytest.raises(RuntimeError):
        environment(tmp_path, count=count, types=types)
    assert not gpu._type_matches("NVIDIA A1000", "A100")


def test_override_and_gpu_types_select_exact_devices(patched, tmp_path, monkeypatch):
    a100 = {"uuid": "GPU-test-a100", "name": "NVIDIA A100-SXM4-80GB", "memory_mb": 81920}
    monkeypatch.setattr(gpu, "docker_gpu_inventory", lambda: [*DEVICES, a100])
    env = environment(tmp_path, count=2, types=["A100"], override_gpus=1)
    path = env._write_env_compose_file()
    request = json.loads(path.read_text())["services"]["main"]["deploy"]["resources"]["reservations"]["devices"]
    assert request == [{"driver": "nvidia", "device_ids": ["GPU-test-a100"], "capabilities": ["gpu"]}]
    env._cleanup_env_compose_file()


def test_preflight_gpu_verifier_and_concurrency(patched, tmp_path):
    job = helpers.make_job(tmp_path, {"gpu-verifier": '[verifier.environment]\ngpus=1\n'})
    job.config = SimpleNamespace(n_concurrent_trials=1)
    report = preflight.check_job_resources(job)
    assert report["status"] == "passed"
    assert report["checks"][1]["gpu_devices"] == DEVICES
    job.config.n_concurrent_trials = 2
    with pytest.raises(RuntimeError, match="Concurrency=1"):
        preflight.check_job_resources(job)


def test_gpu_driver_failure_identifies_only_affected_trial(patched, tmp_path, monkeypatch):
    def missing():
        raise RuntimeError("NVIDIA runtime missing")
    monkeypatch.setattr(gpu, "docker_gpu_inventory", missing)
    job = helpers.make_job(tmp_path, {"cpu-first": "", "gpu-last": "[environment]\ngpus=1\n"})
    job.config = SimpleNamespace(n_concurrent_trials=1)
    report = preflight.check_job_resources(job, raise_on_failure=False)
    assert report["skipped_trials"] == 1 and report["runnable_trials"] == 1
    assert report["checks"][0]["error"] is None
    assert "NVIDIA runtime missing" in report["checks"][1]["error"]


def test_conflicting_authored_device_requests_rejected(patched, tmp_path):
    (tmp_path / "docker-compose.yaml").write_text('services:\n  main:\n    gpus: all\n')
    with pytest.raises(RuntimeError, match="不能与自动 GPU 分配叠加"):
        environment(tmp_path)


def test_windows_gpu_rejected_without_probing(patched, tmp_path, monkeypatch):
    monkeypatch.setattr(gpu, "docker_gpu_inventory", lambda: pytest.fail("Windows GPU probe"))
    with pytest.raises(RuntimeError, match="does not support GPU allocation"):
        environment(tmp_path, task_os="windows")


@pytest.mark.parametrize("failure", [None, "start", "exec", "wrong-device"])
def test_worker_smoke_cleans_up_and_checks_both_roles(tmp_path, monkeypatch, failure):
    from harbor.environments.factory import EnvironmentFactory
    smoke = load("gpu_smoke", ROOT / "scripts/check_docker_gpu.py")
    monkeypatch.setattr(smoke, "patch_docker_gpu", lambda: None)
    events = []
    class FakeEnvironment:
        _edison_gpu_devices = DEVICES
        async def start(self, **kwargs):
            events.append("start")
            if failure == "start":
                raise RuntimeError("start failed")
        async def exec(self, command, **kwargs):
            events.append("exec")
            return SimpleNamespace(return_code=int(failure == "exec"), stderr="exec failed",
                stdout="GPU-wrong" if failure == "wrong-device" else "GPU-test-4070\n")
        async def stop(self, delete):
            assert delete
            events.append("stop")
    monkeypatch.setattr(EnvironmentFactory, "create_environment_from_config", lambda **kwargs: FakeEnvironment())
    if failure:
        with pytest.raises(RuntimeError):
            asyncio.run(smoke.check())
        assert events[-1] == "stop"
        assert events.count("start") == 1
    else:
        asyncio.run(smoke.check())
        assert events == ["start", "exec", "stop"] * 2


@pytest.mark.parametrize("enabled", [True, False])
def test_real_harbor_start_renders_gpu_and_proxy_on_correct_services(tmp_path, enabled):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI needed")
    (tmp_path / "docker-compose.yaml").write_text(json.dumps({"services": {
        "main": {"network_mode": "service:api"}, "api": {"image": "ubuntu:24.04"}
    }}))
    script = '''
import asyncio, json, subprocess, sys
from pathlib import Path
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths
enabled = sys.argv[2] == "True"
if enabled:
    import docker_gpu
    docker_gpu.docker_gpu_inventory = lambda: [{"uuid": "GPU-test-4070", "name": "RTX 4070", "memory_mb": 8188}]
else:
    assert not getattr(DockerEnvironment, "_edison_gpu_patch", False)
path = Path(sys.argv[1])
env = DockerEnvironment(environment_dir=path, environment_name="gpu-test", session_id="gpu-test",
    trial_paths=TrialPaths(path / "trial"), task_env_config=EnvironmentConfig(docker_image="ubuntu:24.04", gpus=int(enabled)),
    persistent_env={"HTTP_PROXY": "http://host.docker.internal:7892"})
env._validate_daemon_mode = lambda: None
async def noop(*args, **kwargs): pass
env._validate_image_os = noop
env.ensure_dirs = noop
env._upload_environment_dir_after_start = noop
commands = []
async def compose(args, **kwargs):
    commands.append(args)
    if args[0] != "up": return
    cmd = ["docker", "compose", "-p", "gpu-test"]
    for p in env._docker_compose_paths: cmd += ["-f", str(p)]
    cmd += ["config", "--format", "json"]
    rendered = subprocess.run(cmd, env=env._compose_env_vars(), capture_output=True, text=True, timeout=20)
    assert rendered.returncode == 0, rendered.stderr
    services = json.loads(rendered.stdout)["services"]
    main = services["main"]
    assert main["network_mode"] == "service:api"
    assert "extra_hosts" not in main
    assert services["api"]["extra_hosts"]
    assert main["environment"]["HTTP_PROXY"] == "http://host.docker.internal:7892"
    assert "deploy" not in services["api"]
    if enabled:
        req = main["deploy"]["resources"]["reservations"]["devices"]
        assert req == [{"driver": "nvidia", "device_ids": ["GPU-test-4070"], "capabilities": ["gpu"]}]
    else:
        assert not main.get("deploy", {}).get("resources", {}).get("reservations", {}).get("devices")
env._run_docker_compose_command = compose
asyncio.run(env.start(force_build=False))
assert any(c[0] == "up" for c in commands)
asyncio.run(env.stop(delete=True))
assert env._env_compose_path is None
'''
    child = adapter.harbor_subprocess_env({"_resource_preflight": enabled,
        "container_proxy_url": "http://host.docker.internal:7892"})
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(enabled)],
                            env=child, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
