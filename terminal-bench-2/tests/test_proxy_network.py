"""Offline regression for task-local requests leaking to the worker proxy."""
import contextvars
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.request import proxy_bypass_environment

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


patch = load("proxy_patch", ROOT / "scripts/harbor_patches/proxy_network.py")
adapter = load("proxy_adapter", ROOT / "scripts/run_tb2.py")
PROXY = {"HTTP_PROXY": "http://host.docker.internal:7892",
         "http_proxy": "http://host.docker.internal:7892",
         "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "internal.example"}


def fake_environment(tmp_path):
    return SimpleNamespace(_environment_docker_compose_path=tmp_path / "docker-compose.yaml",
                           extra_docker_compose_paths=[], task_env_config=SimpleNamespace(env={}))


def test_verifier_local_services_bypass_proxy_but_external_requests_do_not(tmp_path):
    env = fake_environment(tmp_path)
    values = {**PROXY, "CUSTOMER_HOST": "customer", "CUSTOMER_BASE": "http://customer:9000",
              "KAFKA_BROKERS": "kafka:9092", "ANTHROPIC_BASE_URL": "https://openrouter.ai/api"}
    # Reproduce the old behavior, then validate the repaired request routing.
    assert not proxy_bypass_environment("customer:9000", {"no": PROXY["NO_PROXY"]})
    fixed = patch.proxy_safe_env(env, values)
    proxies = {"http": fixed["http_proxy"], "no": fixed["no_proxy"]}
    for host in ("customer:9000", "kafka:9092", "localhost", "127.0.0.1", "internal.example"):
        assert proxy_bypass_environment(host, proxies)
    assert not proxy_bypass_environment("openrouter.ai", proxies)
    assert not proxy_bypass_environment("github.com", proxies)
    assert fixed["NO_PROXY"] == fixed["no_proxy"]
    assert fixed["HTTP_PROXY"] == PROXY["HTTP_PROXY"]
    assert values["NO_PROXY"] == PROXY["NO_PROXY"]


def test_agent_compose_services_and_custom_no_proxy_are_preserved(tmp_path):
    env = fake_environment(tmp_path)
    env._environment_docker_compose_path.write_text("services:\n  main: {}\n  customer: {}\n  kafka: {}\n")
    result = patch.proxy_safe_env(env, PROXY)
    assert {"customer", "kafka", "main", "internal.example"} <= set(result["no_proxy"].split(","))
    assert patch.proxy_safe_env(env, {"CUSTOMER_HOST": "customer"}) == {"CUSTOMER_HOST": "customer"}
    assert patch.proxy_safe_env(env, None) is None


@pytest.mark.parametrize("params,enabled,alias", [
    ({}, False, None),
    ({"container_proxy_url": "http://host.docker.internal:7892"}, True, "host.docker.internal:host-gateway"),
    ({"container_proxy_url": "http://10.0.0.1:7892"}, True, None),
    ({"container_proxy_url": "http://host.docker.internal:7892", "container_proxy_host_gateway": False}, True, None),
    ({"container_proxy_url": "http://host.docker.internal:7892", "container_proxy_enabled": False}, False, None),
])
def test_patch_activation_follows_container_proxy(params, enabled, alias, monkeypatch):
    monkeypatch.delenv("EDISON_TB2_CONTAINER_PROXY_URL", raising=False)
    monkeypatch.setenv("EDISON_TB_PROXY_NETWORK_PATCH", "stale")
    monkeypatch.setenv("EDISON_TB_PROXY_HOST_ALIAS", "stale")
    env = adapter.harbor_subprocess_env(params)
    assert (env.get("EDISON_TB_PROXY_NETWORK_PATCH") == "1") == enabled
    assert env.get("EDISON_TB_PROXY_HOST_ALIAS") == alias


def test_real_harbor_separate_verifier_env_and_startup_compose(tmp_path):
    # Use a subprocess to exercise sitecustomize and avoid patching other tests.
    script = '''
import contextvars, json, sys
from pathlib import Path
from types import SimpleNamespace
from harbor.environments.docker.docker import DockerEnvironment
env = object.__new__(DockerEnvironment)
env.environment_dir = Path(sys.argv[1])
env.extra_docker_compose_paths = []  # Harbor clears these for separate verifier.
env.task_env_config = SimpleNamespace(env={})
env._persistent_env = {
    "HTTP_PROXY": "http://host.docker.internal:7892",
    "NO_PROXY": "localhost,127.0.0.1,::1",
}
env._exec_env_overlays = contextvars.ContextVar("overlays", default=())
env._env_compose_temp_dir = None
env._env_compose_path = None
path = env._write_env_compose_file()
data = json.loads(path.read_text())
assert set(data["services"]) == {"main"}
main = data["services"]["main"]
assert main["extra_hosts"] == ["host.docker.internal:host-gateway"]
assert "volumes" not in main and "network_mode" not in main
assert main["environment"]["HTTP_PROXY"] == "http://host.docker.internal:7892"
assert env.extra_docker_compose_paths == []
with env.scoped_exec_env({"NO_PROXY": "localhost"}):
    merged = env._merge_env({"CUSTOMER_HOST": "customer", "KAFKA_BROKERS": "kafka:9092"})
    assert {"customer", "kafka"} <= set(merged["no_proxy"].split(","))
    assert merged["no_proxy"] == merged["NO_PROXY"]
env._cleanup_env_compose_file()
'''
    child = adapter.harbor_subprocess_env({"container_proxy_url": "http://host.docker.internal:7892"})
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=child,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_proxy_override_defers_host_mapping_until_network_mode_is_known(tmp_path):
    path = adapter.write_container_proxy_compose(
        tmp_path, {"container_proxy_url": PROXY["HTTP_PROXY"]}, PROXY)
    assert json.loads(path.read_text()) == {"services": {"main": {"environment": PROXY}}}


@pytest.mark.parametrize("services,egress,owner", [
    ({"main": {}}, False, "main"),
    ({"main": {"network_mode": "service:api"}, "api": {}}, False, "api"),
    ({"main": {"network_mode": "service:api"}, "api": {"network_mode": "service:net"}, "net": {}}, False, "net"),
    ({"main": {}}, True, "harbor-docker-egress-control-sidecar"),
    ({"main": {"network_mode": "service:api"}, "api": {}}, True, "harbor-docker-egress-control-sidecar"),
    ({"main": {"networks": ["default"]}}, True, "main"),
])
def test_real_harbor_compose_shared_network_mapping(tmp_path, services, egress, owner):
    """Render with real Compose + Harbor; no daemon, images or paid model needed."""
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI required for merged-config regression")
    task_compose = tmp_path / "docker-compose.yaml"
    task_compose.write_text(json.dumps({"services": {
        name: {"image": "alpine:3.20", **config} for name, config in services.items()
    }}))
    proxy_compose = adapter.write_container_proxy_compose(tmp_path, {}, PROXY)
    script = '''
import contextvars, json, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from harbor.environments.docker.docker import DockerEnvironment
env = object.__new__(DockerEnvironment)
env.environment_dir = Path(sys.argv[1])
env.extra_docker_compose_paths = [env.environment_dir / "container_proxy_compose.json"]
env.task_env_config = SimpleNamespace(env={})
env._persistent_env = {"HTTP_PROXY": "http://host.docker.internal:7892"}
env._exec_env_overlays = contextvars.ContextVar("overlays", default=())
env._env_compose_temp_dir = None
env._env_compose_path = None
env._egress_control_services_compose_temp_dir = None
env._egress_control_services_compose_path = None
env._enable_egress_control = sys.argv[2] == "True"
# Match Harbor start() ordering: environment overlay precedes egress overlay.
path = env._write_env_compose_file()
paths = [env._environment_docker_compose_path, *env.extra_docker_compose_paths, path]
env._write_egress_control_services_compose_file()
if env._enable_egress_control:
    paths.append(env._DOCKER_COMPOSE_EGRESS_CONTROL_PATH)
    if env._egress_control_services_compose_path:
        paths.append(env._egress_control_services_compose_path)
command = ["docker", "compose", "--project-name", "edison-proxy-regression"]
for p in paths:
    command.extend(["-f", str(p)])
command.extend(["config", "--format", "json"])
rendered = subprocess.run(command, capture_output=True, text=True, timeout=20)
assert rendered.returncode == 0, rendered.stderr
data = json.loads(rendered.stdout)
services = data["services"]
owner = sys.argv[3]
hosts = services[owner]["extra_hosts"]
if isinstance(hosts, list):
    hosts = dict(item.replace(":", "=", 1).split("=", 1) for item in hosts)
assert hosts == {"host.docker.internal": "host-gateway"}, hosts
for name, config in services.items():
    if name != owner:
        assert not config.get("extra_hosts"), (name, config)
    if config.get("network_mode", "").startswith(("service:", "container:")):
        assert not config.get("extra_hosts"), (name, config)
assert services["main"]["environment"]["HTTP_PROXY"] == "http://host.docker.internal:7892"
assert "localhost" in services["main"]["environment"]["NO_PROXY"]
original = json.loads(env._environment_docker_compose_path.read_text())["services"]
for name, config in original.items():
    if "network_mode" in config:
        assert services[name]["network_mode"] == config["network_mode"]
if env._enable_egress_control:
    sidecar = env._EGRESS_CONTROL_SERVICE_NAME
    for name in env._egress_controlled_service_names():
        assert services[name]["network_mode"] == "service:" + sidecar
    assert "NET_ADMIN" in services[sidecar]["cap_add"]
env._cleanup_env_compose_file()
env._cleanup_egress_control_services_compose_file()
'''
    child = adapter.harbor_subprocess_env({"container_proxy_url": PROXY["HTTP_PROXY"]})
    child.update(PYTHONDONTWRITEBYTECODE="1", EGRESS_CONTROL_SIDECAR_IMAGE_NAME="alpine:3.20")
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(egress), owner],
                            env=child, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    # The authored compose and reusable proxy override were not rewritten.
    assert "extra_hosts" not in proxy_compose.read_text()
    assert "extra_hosts" not in task_compose.read_text()


@pytest.mark.parametrize("services", [
    {"main": {"network_mode": "container:external"}},
    {"main": {"network_mode": "service:missing"}},
    {"main": {"network_mode": "service:api"}, "api": {"network_mode": "service:main"}},
    {"main": {"network_mode": "${TASK_NETWORK_MODE}"}},
])
def test_unsupported_network_owner_fails_before_docker_create(tmp_path, services):
    env = fake_environment(tmp_path)
    env._environment_docker_compose_path.write_text(json.dumps({"services": services}))
    with pytest.raises(ValueError, match="Cannot place proxy host mapping"):
        patch.proxy_hosts_service(env)
