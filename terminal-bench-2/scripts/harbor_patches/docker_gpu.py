"""Scoped Harbor 0.20 Docker NVIDIA support; no installed Harbor files changed.

Probe the selected Docker daemon with a disposable, networkless container.
Use concrete device UUIDs in Compose, including for separate verifier envs.
This is device access, not a cross-job GPU scheduler or a VRAM reservation.
"""
import csv
import json
import os
import re
import subprocess
import uuid
from functools import lru_cache


def _run(command, timeout=30):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"GPU preflight command failed ({command[0]}): {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"GPU preflight failed: {(result.stderr or result.stdout)[-2000:]}")
    return result.stdout


def _probe_inventory():
    # Check the daemon, not the local CLI host (which may use a remote context).
    info = json.loads(_run(["docker", "info", "--format", "{{json .}}"]))
    if info.get("OSType") != "linux" or "nvidia" not in (info.get("Runtimes") or {}):
        raise RuntimeError("GPU 题需要 Linux Docker 和已配置的 NVIDIA Container Toolkit (nvidia runtime)")
    image = os.environ.get("EDISON_TB_GPU_PROBE_IMAGE", "ubuntu:24.04")
    name = "edison-gpu-probe-" + uuid.uuid4().hex
    try:
        output = _run([
            "docker", "run", "--rm", "--pull=never", "--name", name,
            "--network=none", "--runtime=nvidia", "--gpus", "all",
            "--entrypoint", "nvidia-smi", image,
            "--query-gpu=uuid,name,memory.total", "--format=csv,noheader,nounits",
        ], timeout=60)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Docker GPU 探测失败；请先准备本地探测镜像 {image}，并确认容器内 nvidia-smi 可用。{exc}"
        ) from exc
    finally:
        # --rm handles successful exits; explicit cleanup also handles timeout.
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    devices = []
    for fields in csv.reader(output.splitlines(), skipinitialspace=True):
        if len(fields) != 3 or not fields[0].startswith("GPU-"):
            raise RuntimeError("Docker GPU 探测返回了无法识别的设备信息")
        devices.append({"uuid": fields[0].strip(), "name": fields[1].strip(),
                        "memory_mb": int(fields[2].strip())})
    if not devices or len({d["uuid"] for d in devices}) != len(devices):
        raise RuntimeError("Docker GPU 探测没有返回有效且唯一的 GPU 设备")
    return devices


@lru_cache(maxsize=1)
def _probe_result():
    # Cache failures as well: a batch of GPU tasks must not repeat a failing
    # 60-second probe for each environment. A new job uses a new subprocess.
    try:
        return _probe_inventory(), None
    except (RuntimeError, ValueError) as exc:
        return [], str(exc)


def docker_gpu_inventory():
    devices, error = _probe_result()
    if error:
        raise RuntimeError(error)
    return devices


def _type_matches(name, requested):
    # Match whole model tokens: A100 must not match A1000. Unknown aliases
    # fail closed instead of silently substituting an incompatible GPU.
    tokens = lambda value: re.findall(r"[a-z0-9]+", value.lower())
    haystack, needle = tokens(name), tokens(requested)
    return bool(needle) and any(haystack[i:i + len(needle)] == needle
                               for i in range(len(haystack) - len(needle) + 1))


def select_gpu_devices(environment):
    count = environment._effective_gpus
    if count < 0:
        raise RuntimeError("GPU 数量不能小于 0")
    if not count:
        return []
    if environment._is_windows_container:
        raise RuntimeError("Edison GPU 适配目前仅支持 Linux 容器")
    inventory = docker_gpu_inventory()
    types = environment.task_env_config.gpu_types
    eligible = [d for d in inventory if not types or any(_type_matches(d["name"], t) for t in types)]
    if len(eligible) < count:
        available = ", ".join(f"{d['name']} ({d['memory_mb']} MiB)" for d in inventory)
        raise RuntimeError(f"题目需要 {count} 张 GPU，类型 {types or '不限'}；可用设备：{available}")
    return eligible[:count]


def validate_authored_gpu_config(environment):
    import yaml

    for path in [environment._environment_docker_compose_path, *environment.extra_docker_compose_paths]:
        if not path.is_file():
            continue
        for name, service in (yaml.safe_load(path.read_text()) or {}).get("services", {}).items():
            service = service or {}
            devices = (((service.get("deploy") or {}).get("resources") or {}).get("reservations") or {}).get("devices")
            if service.get("gpus") is not None or devices:
                raise RuntimeError(
                    f"服务 {name} 已配置 Compose GPU/设备预留，不能与自动 GPU 分配叠加；需要单独适配该题"
                )


def patch_docker_gpu():
    from harbor.environments.docker.docker import DockerEnvironment

    if getattr(DockerEnvironment, "_edison_gpu_patch", False):
        return
    original_capabilities = DockerEnvironment.capabilities.fget
    original_validate = DockerEnvironment._validate_gpu_support
    original_write = DockerEnvironment._write_env_compose_file

    def capabilities(self):
        return original_capabilities(self).model_copy(update={"gpus": not self._is_windows_container})

    def validate(self):
        original_validate(self)
        self._edison_gpu_devices = select_gpu_devices(self)
        if self._edison_gpu_devices:
            validate_authored_gpu_config(self)

    def write(self):
        path = original_write(self)
        devices = self._edison_gpu_devices
        if devices:
            document = json.loads(path.read_text())
            main = document.setdefault("services", {}).setdefault("main", {})
            main.setdefault("deploy", {}).setdefault("resources", {}).setdefault("reservations", {})["devices"] = [
                {"driver": "nvidia", "device_ids": [d["uuid"] for d in devices], "capabilities": ["gpu"]}
            ]
            path.write_text(json.dumps(document, indent=2))
            self.logger.info("[edison-gpu] Assigned GPU devices to main: %s", devices)
        return path

    DockerEnvironment.capabilities = property(capabilities)
    DockerEnvironment._validate_gpu_support = validate
    DockerEnvironment._write_env_compose_file = write
    DockerEnvironment._edison_gpu_patch = True
