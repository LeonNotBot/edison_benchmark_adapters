# LiveCodeBench Pro Edison Adapter

这是 Edison External Benchmark 的 LiveCodeBench Pro adapter。

它负责把 Edison 生成的 `input.json` 转成 Inspect AI 命令，运行 `inspect_evals/livecodebench_pro`，再把 Inspect 日志归一化为 Edison 标准 `result.json`。

## 目录位置

Mac 本机 all-in-one：

```bash
/Users/leon/code/edison_inspect_aliyun_v2/edison_benchmark_adapters/livecodebench-pro
```

Ubuntu worker：

```bash
/home/leon/code/edison_inspect_aliyun/edison_benchmark_adapters/livecodebench-pro
```

Edison worker 会调用：

```bash
run.sh --config <input.json>
```

## 运行环境

要求 worker 上已经准备好 `inspect_evals`，并且可以执行 Inspect CLI。

推荐显式指定：

```bash
export EXTERNAL_BENCHMARK_PYTHON_BIN=/home/leon/code/edison_inspect_aliyun/edison-eval/venv/bin/python
export INSPECT_EVALS_DIR=/home/leon/code/edison_inspect_aliyun/inspect_evals
export INSPECT_BIN=/home/leon/code/edison_inspect_aliyun/inspect_evals/.venv/bin/inspect
```

Mac 本机路径对应替换为：

```bash
export EXTERNAL_BENCHMARK_PYTHON_BIN=/Users/leon/code/edison_inspect_aliyun_v2/edison-eval/venv/bin/python
export INSPECT_EVALS_DIR=/Users/leon/code/edison_inspect_aliyun_v2/inspect_evals
export INSPECT_BIN=/Users/leon/code/edison_inspect_aliyun_v2/inspect_evals/.venv/bin/inspect
```

## Agent 怎么填

LiveCodeBench Pro 当前没有 Hermes / CCB / OpenClaw 这种外部 agent。

Edison 前端里建议填：

```json
"agent": "model"
```

含义是：adapter 通过 Inspect AI 默认 `generate` solver 直接调用模型生成 C++ 代码，然后由 LiveCodeBench Pro / LightCPVerifier 判分。

## 常用参数

```json
{
  "adapter": "livecodebench-pro",
  "agent": "model",
  "suite": "livecodebench-pro",
  "task_names": "2053B",
  "limit": 1,
  "runs": 1,
  "timeout_seconds": 28800,
  "output_dir": "~/data/edison_external_benchmarks/livecodebench-pro",
  "params": {
    "model_preflight": true,
    "model_preflight_timeout_seconds": 60,
    "split": "quater_2024_10_12",
    "no_fail_on_error": true,
    "continue_on_fail": true
  }
}
```

说明：

- `task_names`：可选。这里对应 Inspect 的 `--sample-id`，用于指定 LiveCodeBench Pro 题号，例如 `2053B`。多个题号可用逗号、换行或 JSON 数组。
- `limit`：不指定 `task_names` 时生效，表示抽取多少道题。
- `split`：传给 Inspect task 的数据集 split，默认 `quater_2024_10_12`。
- `difficulty`：可选，支持 `easy`、`medium`、`hard`。
- `model_preflight`：默认开启，正式跑 Inspect 前先做一次模型 API 预检。
- `no_fail_on_error` / `continue_on_fail`：默认建议开启，避免单个样本失败时整批任务直接中断。

## 关于 HuggingFace / Docker 网络

LiveCodeBench Pro 会从 HuggingFace 下载题目和 testcase，并通过 Docker sandbox 运行 verifier。

如果出现类似：

```text
Test case not found for problem 2034G1
```

并且直接访问：

```bash
curl -I https://huggingface.co/datasets/QAQAQAQAQ/LiveCodeBench-Pro-Testcase/resolve/<revision>/2034G1.zip
```

返回 `404 EntryNotFound`，说明这个 testcase 在当前 revision 下确实不存在或数据集索引与 testcase 仓库不匹配，不是 Edison 主链路或模型 Key 的问题。

日常 smoke 建议先指定一个已验证样本，例如：

```json
"task_names": "2053B"
```

## 手动测试

```bash
cd /home/leon/code/edison_inspect_aliyun/edison_benchmark_adapters/livecodebench-pro

export EXTERNAL_BENCHMARK_PYTHON_BIN=/home/leon/code/edison_inspect_aliyun/edison-eval/venv/bin/python
export INSPECT_EVALS_DIR=/home/leon/code/edison_inspect_aliyun/inspect_evals
export INSPECT_BIN=/home/leon/code/edison_inspect_aliyun/inspect_evals/.venv/bin/inspect

./run.sh --config examples/input.json
```

执行成功后，adapter 会在 `input.json.paths.result_json` 指定的位置写入 Edison 标准结果。
