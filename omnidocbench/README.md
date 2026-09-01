# OmniDocBench Edison Adapter

Edison External Benchmark 的 OmniDocBench 文档解析评测 adapter。

它把 Edison 生成的 `input.json` 转成 docker-in-docker 命令，在官方镜像内运行
OmniDocBench end2end 评测，再把结果归一化为 Edison 标准 `result.json`。

## 目录结构

```
omnidocbench/
├── run.sh                        # Edison worker 入口（薄封装，转发到 scripts/）
├── README.md                     # 本文档
├── scripts/
│   ├── __init__.py
│   └── run_omnidocbench.py       # 主逻辑：读 input → docker run → 写 result
└── examples/
    ├── input.json                # 输入样例
    ├── input.annotated.jsonc     # 输入格式（带注释）
    ├── result.json               # 输出样例（真实分数 0.7799）
    └── result.annotated.jsonc    # 输出格式（带注释）
```

Edison worker 会调用：

```bash
run.sh --config <input.json>
```

## 运行原理（docker-in-docker）

与 terminal-bench-2 一致，本 adapter 不在 worker 内安装 TeX Live / ImageMagick /
Ghostscript 等重依赖，而是通过官方镜像运行评测：

```
ghcr.io/zeng-weijun/omnidocbench-eval:repro-ubuntu2204
```

该镜像已内置 Python 环境 + TeX Live + ImageMagick + Ghostscript，78.84 基线在此镜像复现。

adapter 执行的核心命令：

```bash
docker run --rm --entrypoint bash \
  -v <gt_json>:/workspace/demo_data/omnidocbench_demo/OmniDocBench_demo.json:ro \
  -v <predictions_dir>:/workspace/demo_data/end2end:ro \
  -v <run_dir>/odb_result:/workspace/result \
  ghcr.io/zeng-weijun/omnidocbench-eval:repro-ubuntu2204 \
  -c 'python pdf_validation.py --config configs/end2end.yaml'
```

worker 需能访问 `/var/run/docker.sock`（docker-compose.yml 已挂载），无需改
Dockerfile / docker-compose.yml。

## 输入参数

OmniDocBench 特有的数据路径和推理参数通过 `benchmark.params` 透传（宿主机绝对路径）：

### VLM API 配置（model 字段）

| 字段 | 说明 | Fallback 环境变量 |
|------|------|------------------|
| `model.api_endpoint` | API endpoint（主配置源） | `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` / `OPENROUTER_BASE_URL` |
| `model.api_key_env` | 指定读取哪个环境变量的 key | `EDISON_MODEL_API_KEY` → `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` |
| `model.normalized_provider` | provider 类型（`anthropic`/`openai`/`openrouter`） | - |
| `model.model_identifier` | 模型 ID（如 `claude-sonnet-4`） | - |

**配置优先级**：
- Edison model 表显式配置（`model.api_endpoint` / `model.api_key_env`）优先
- 环境变量 fallback 仅兜底（本地测试/开发场景）
- 与官方不同模型不同参数不同，adapter 通过 Edison model 配置灵活适配各模型

### 数据路径参数（后端自动填默认值，前端无需暴露）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `gt_json` | `OmniDocBench/full_dataset/OmniDocBench.json` | ground truth JSON（1651样本），后端 `_external_benchmark_params` 自动填充 |
| `images_dir` | 从 `gt_json` 推导（同级 `images/`） | 源图片目录，不设时自动推导 |
| `predictions_dir` | 不设 → 触发自动推理 | 模型预测 Markdown 目录，未提供时用 VLM API 自动推理生成 |

**默认行为**：
- 后端 `_external_benchmark_params()` 为 omnidocbench 自动设置 `gt_json` 指向完整数据集（1651样本）
- `run.sh` 在首次运行时自动检测并下载完整数据集（通过 `hf` CLI 从 `opendatalab/OmniDocBench`）
- 前端创建任务时无需填写这些路径；如需评测自定义数据集，可在 `params` 中显式覆盖

### VLM 推理可选参数（仅当需要自动推理时）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `vlm_max_tokens` | 16384 | 模型输出最大 token 数（对齐官方长文档配置 Logics_Parsing=16384 / DotsOCR=24000） |
| `vlm_temperature` | 不设 | 采样温度（不设时用模型默认，评测建议 0 提高复现性） |
| `vlm_top_p` | 不设 | 核采样参数（不设时用模型默认） |
| `vlm_timeout_seconds` | 120 | 单张图片 API 调用超时（秒） |

**参数设计原则**：
- 默认参数（如 `vlm_max_tokens=16384`）对齐官方长文档模型配置，避免输出截断
- 可选参数（如 `vlm_temperature`）仅当明确指定时传给 API，否则用模型自身默认值
- 这样既保证长文档不丢分，又支持多模型（Claude/GPT4/Qwen/Gemini）灵活切换

完整输入格式见 `examples/input.annotated.jsonc`。

## 评分口径

综合分**对齐官方 3 维公式**（`OmniDocBench/src/runtime/eval_report.py:131` 和 `README.md:515`）：

| 维度 | 指标 | 键路径 | 计算 |
|------|------|--------|------|
| text_block | Edit_dist | `all.ALL_page_avg` | `1 - Edit_dist` |
| display_formula | CDM | `page.CDM.ALL` | 直接取（按页聚合）|
| table | TEDS | `page.TEDS.ALL` | 直接取（按页聚合）|
| reading_order | Edit_dist | `all.ALL_page_avg` | `1 - Edit_dist`（单独报告，**不进 overall**）|

```
overall = ((1 - text_edit)×100 + table_TEDS×100 + formula_CDM×100) / 3 / 100
```

**为什么是 3 维不是 4 维**：官方 overall 只算 text_block、table、display_formula 三个维度，
reading_order 单独报告但不计入综合分。formula/table 用 `.page` 键（先按页聚合再总体平均），
比 `.all` 键更细粒度，与官方 notebook 一致。

结果从容器输出的 `end2end_quick_match_metric_result.json` 读取。
完整输出格式见 `examples/result.annotated.jsonc`。

## 本地运行

```bash
./run.sh --config examples/input.json
```

执行成功后，adapter 会在 `input.json.paths.result_json` 指定位置写入 Edison 标准结果。

> **本地测试 vs 线上运行**：线上由后端 `_external_benchmark_params()` 自动填充 `gt_json`，前端无需传。本地测试因为没有后端填充这一步，`examples/input.json` 里显式写了 `gt_json`（相对路径指向内置 demo 数据），保证 `./run.sh` 开箱即跑。

## 运行环境

- worker 可执行 `docker`（docker-in-docker）
- 首次运行需能拉取官方镜像（国内需配置代理）；镜像已缓存后离线可跑

