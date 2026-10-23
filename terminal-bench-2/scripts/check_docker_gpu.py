#!/usr/bin/env python3
"""Exercise patched Harbor agent/verifier Docker allocation without a model."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent / "harbor_patches"))
from docker_gpu import patch_docker_gpu  # noqa: E402


async def check():
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.config import EnvironmentConfig as RuntimeConfig
    from harbor.models.trial.paths import TrialPaths

    patch_docker_gpu()
    with tempfile.TemporaryDirectory(prefix="edison-harbor-gpu-") as directory:
        root = Path(directory)
        # Two independently created environments exercise the same allocation
        # path used for the agent and a separate verifier. Neither is reused.
        for phase in ("agent", "verifier"):
            context = root / phase
            context.mkdir()
            environment = EnvironmentFactory.create_environment_from_config(
                config=RuntimeConfig(type="docker"), environment_dir=context,
                environment_name="edison-gpu-check",
                session_id="edison-gpu-check-" + uuid.uuid4().hex[:12],
                trial_paths=TrialPaths(root / (phase + "-logs")),
                task_env_config=EnvironmentConfig(
                    docker_image=os.environ.get("EDISON_TB_GPU_PROBE_IMAGE", "ubuntu:24.04"), gpus=1,
                ),
            )
            try:
                await environment.start(force_build=False)
                result = await environment.exec(
                    "nvidia-smi --query-gpu=uuid --format=csv,noheader", timeout_sec=30,
                )
                if result.return_code:
                    raise RuntimeError(f"{phase} GPU check failed: {result.stderr or result.stdout}")
                expected = {d["uuid"] for d in environment._edison_gpu_devices}
                actual = set((result.stdout or "").split())
                if actual != expected:
                    raise RuntimeError(f"{phase} GPU visibility mismatch: expected={expected}, actual={actual}")
                print(f"[edison-gpu-check] {phase} PASS: {sorted(actual)}", flush=True)
            finally:
                await environment.stop(delete=True)
    print("Harbor Docker GPU allocation PASS (agent + separate verifier). No model API called.", flush=True)


if __name__ == "__main__":
    asyncio.run(check())
