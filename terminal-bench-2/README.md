# Terminal-Bench 2 Edison Adapter

这是 Edison External Benchmark 的第一个样板 adapter。

它负责把 Edison 生成的 `input.json` 转成 Harbor 命令，运行 `terminal-bench/terminal-bench-2`，再把 Harbor 原始结果归一化为 Edison 标准 `result.json`。

## 目录位置

Mac 本机 all-in-one：

```bash
/Users/leon/code/edison_inspect_aliyun_v2/edison_benchmark_adapters/terminal-bench-2
```

Ubuntu worker：

```bash
/home/leon/code/edison_inspect_aliyun/edison_benchmark_adapters/terminal-bench-2
```

Edison worker 会调用：

```bash
run.sh --config <input.json>
```

## 运行环境

要求 worker 的 Python 环境中可以执行 `harbor`。

推荐在 Edison venv 中安装 Harbor 后，通过环境变量指定：

Mac 本机 all-in-one：

```bash
export EXTERNAL_BENCHMARK_PYTHON_BIN=/Users/leon/code/edison_inspect_aliyun_v2/edison-eval/venv/bin/python
export EXTERNAL_BENCHMARK_HARBOR_BIN=/Users/leon/code/edison_inspect_aliyun_v2/edison-eval/venv/bin/harbor
```

Ubuntu worker：

```bash
export EXTERNAL_BENCHMARK_PYTHON_BIN=/home/leon/code/edison_inspect_aliyun/edison-eval/venv/bin/python
export EXTERNAL_BENCHMARK_HARBOR_BIN=/home/leon/code/edison_inspect_aliyun/edison-eval/venv/bin/harbor
```

或者确保默认 `python3` 所在环境已安装 Harbor。

## 支持 agent

当前第一版支持：

- `oracle`
- `hermes`
- `terminus-2`

预留但暂未接通：

- `ccb`
- `openclaw`

说明：

- `oracle` 用于 Harbor / Docker / TB2 / verifier 链路自检，不代表模型能力。
- `hermes` 使用 Edison 自定义 `EdisonHermes` agent，以适配 Edison 的 Provider、Endpoint 和 API Key 注入方式。
- `terminus-2` 走 Harbor 内置 agent，是 Terminal-Bench 2 官方示例路线之一。

后续接 CCB/OpenClaw 时，优先在 `scripts/run_tb2.py` 中扩展 agent 映射，不需要改 Edison 主工程。

## Edison 配置示例

```json
{
  "adapter": "terminal-bench-2",
  "agent": "hermes",
  "suite": "terminal-bench/terminal-bench-2",
  "task_names": "fix-git",
  "limit": 1,
  "runs": 1,
  "timeout_seconds": 7200,
  "output_dir": "~/data/edison_external_benchmarks/terminal-bench-2",
  "params": {
    "model_preflight": true,
    "model_preflight_timeout_seconds": 30,
    "container_agent_preflight": false,
    "container_preflight_agent_timeout_multiplier": 2,
    "no_delete": true,
    "agent_timeout_multiplier": 2,
    "agent_setup_timeout_multiplier": 10
  }
}
```

## 调试建议

`hermes` / `terminus-2` 真实模型任务可能长时间无输出。adapter 会在 Harbor 前先做一次轻量模型 API 预检：

- `model_preflight=true`：默认开启，先检查 Provider / Endpoint / API Key / 模型标识是否基本可用。
- `model_preflight_timeout_seconds=30`：预检最长等待秒数。

如需继续验证 Docker 容器内的 Hermes 是否真的能拿到模型配置并执行工具，可开启容器内 agent 预检：

- `container_agent_preflight=true`：正式 TB2 运行前，先启动一个 Harbor 本地 smoke task。
- smoke task 会要求 Hermes 在 trial 容器内创建 `/tmp/edison_hermes_container_preflight_ok.txt`，内容为 `OK`。
- verifier 会检查该文件，借此验证 Harbor、Docker、Hermes、模型 API 和工具执行是否能在同一条链路内跑通。
- `container_preflight_agent_timeout_multiplier=2`：smoke task 自身 agent 超时为 120 秒，默认倍率为 2，约 240 秒。

注意：当前容器内 agent 预检只针对 `hermes` 实现；`terminus-2` 暂时只做模型 API 预检和正式 Harbor 任务。容器内预检会额外启动一次 Harbor / Docker / Hermes 流程，适合调试阶段使用；它不解决 Hermes 每个 trial 现场安装的成本问题。

Harbor 自身的 agent 执行超时可以用倍率控制：

- `agent_timeout_multiplier=2`：Hermes 当前建议默认值，约 3600 秒，用于避免较重任务过早超时。
- `agent_timeout_multiplier=1`：使用 TB2 原始超时，当前约 1800 秒。
- `agent_timeout_multiplier=0.05`：smoke 调试时约 90 秒，适合快速判断是否卡住。
- `agent_setup_timeout_multiplier=10`：Hermes 安装阶段通常较慢，默认给更长 setup 时间。

为了避免 smoke 调试时默认抽到 `make-mips-interpreter` 这类重题，可以指定 TB2 题目短名：

- `task_names="fix-git"`：推荐用于第一版真实模型 smoke。
- `task_names="regex-log"`：另一个相对轻量候选。
- `task_names=""`：不指定题目时，回退到 Harbor / TB2 自身的 `limit` 抽题逻辑。
- 多题可以用逗号、换行或 JSON 数组传入，例如 `fix-git,regex-log`。

如果只是验证 Harbor / Docker / Hermes 安装链路，可在 `params.extra_args` 中临时追加 Harbor 支持的调试参数；正式评测前再恢复默认超时。

## 手动测试

准备一个 Edison `input.json` 后：

```bash
EXTERNAL_BENCHMARK_PYTHON_BIN=/Users/leon/code/edison_inspect_aliyun_v2/edison-eval/venv/bin/python \
EXTERNAL_BENCHMARK_HARBOR_BIN=/Users/leon/code/edison_inspect_aliyun_v2/edison-eval/venv/bin/harbor \
  ./run.sh --config /path/to/input.json
```

执行成功后，adapter 会在 `input.json.paths.result_json` 指定的位置写入 Edison 标准结果。
