"""Keep task-local traffic direct and preserve proxy access in separate verifiers.

Enabled only by Edison's adapter when a container proxy is configured. Does not
change Harbor network policies, copy agent compose files, or alter task tests.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit


def _local_host(value: str) -> str | None:
    host = urlsplit(value if "://" in value else "//" + value).hostname
    if host and re.fullmatch(r"[a-zA-Z0-9_-]+", host):
        return host
    return None


def task_service_names(environment) -> set[str]:
    cached = getattr(environment, "_edison_proxy_service_names", None)
    if cached is not None:
        return cached
    import yaml

    names: set[str] = set()
    paths = [environment._environment_docker_compose_path, *environment.extra_docker_compose_paths]
    for path in paths:
        if not path.is_file():
            continue
        data = yaml.safe_load(path.read_text()) or {}
        names.update(str(name) for name in data.get("services", {}))
    environment._edison_proxy_service_names = names
    return names


def proxy_safe_env(environment, env: dict[str, str] | None) -> dict[str, str] | None:
    if not env or not any(env.get(k) for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY")):
        return env
    result = dict(env)
    hosts = task_service_names(environment).copy()
    # Verifier.env is passed to exec, not necessarily present at startup.
    # Only unqualified service names are auto-bypassed; external domains keep
    # using the configured proxy. Explicit user NO_PROXY entries are retained.
    values = {**environment.task_env_config.env, **env}
    for key, value in values.items():
        if not key.upper().endswith(("_HOST", "_HOSTNAME", "_BASE", "_URL", "_BROKERS")):
            continue
        for item in str(value).split(","):
            try:
                host = _local_host(item.strip())
            except ValueError:
                continue
            if host:
                hosts.add(host)
    entries = []
    for raw in (result.get("no_proxy", ""), result.get("NO_PROXY", ""), "localhost,127.0.0.1,::1", *sorted(hosts)):
        entries.extend(part.strip() for part in raw.split(",") if part.strip())
    result["NO_PROXY"] = result["no_proxy"] = ",".join(dict.fromkeys(entries))
    return result


def proxy_hosts_service(environment) -> str:
    """Find the service owning main's network namespace and /etc/hosts.

    Docker rejects extra_hosts on a container sharing another container's
    network. Its hosts file is inherited from the network owner instead.
    Follow task-authored service chains as well as Harbor's egress overlay;
    never change the task's network mode or bypass its egress policy.
    """
    import yaml

    modes = {"main": ""}
    paths = [environment._environment_docker_compose_path, *environment.extra_docker_compose_paths]
    for path in paths:
        if not path.is_file():
            continue
        document = yaml.safe_load(path.read_text()) or {}
        for name, config in document.get("services", {}).items():
            modes.setdefault(name, "")
            if isinstance(config, dict) and "network_mode" in config:
                modes[name] = str(config["network_mode"] or "")

    if getattr(environment, "_enable_egress_control", False):
        sidecar = environment._EGRESS_CONTROL_SERVICE_NAME
        modes.setdefault(sidecar, "")
        for name in environment._egress_controlled_service_names():
            modes[name] = f"service:{sidecar}"

    service = "main"
    visited = set()
    while True:
        if service in visited or service not in modes:
            raise ValueError("Cannot place proxy host mapping: invalid network_mode service chain")
        visited.add(service)
        mode = modes[service]
        if "$" in mode or mode.startswith("container:"):
            # An external container cannot be modified through this compose
            # project. Require an explicit configuration rather than silently
            # losing proxy access or changing someone else's container.
            raise ValueError(
                "Cannot place proxy host mapping on an external or unresolved network_mode. "
                "Use a reachable proxy IP, or configure the network owner's host mapping "
                "and set container_proxy_host_gateway=false."
            )
        if not mode.startswith("service:"):
            return service
        service = mode.removeprefix("service:")


def patch_docker_proxy_network() -> None:
    from harbor.environments.docker.docker import DockerEnvironment

    if getattr(DockerEnvironment, "_edison_proxy_network_patch", False):
        return
    original_startup = DockerEnvironment._startup_env
    original_merge = DockerEnvironment._merge_env
    original_write = DockerEnvironment._write_env_compose_file

    def startup(self):
        return proxy_safe_env(self, original_startup(self))

    def merge(self, env):
        return proxy_safe_env(self, original_merge(self, env))

    def write(self):
        path = original_write(self)
        alias = os.environ.get("EDISON_TB_PROXY_HOST_ALIAS", "")
        if alias:
            data = json.loads(path.read_text())
            owner = proxy_hosts_service(self)
            service = data.setdefault("services", {}).setdefault(owner, {})
            service.setdefault("extra_hosts", []).append(alias)
            path.write_text(json.dumps(data, indent=2))
        return path

    DockerEnvironment._startup_env = startup
    DockerEnvironment._merge_env = merge
    DockerEnvironment._write_env_compose_file = write
    DockerEnvironment._edison_proxy_network_patch = True
