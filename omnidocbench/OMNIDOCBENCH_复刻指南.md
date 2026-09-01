# OmniDocBench 接入 Edison —— edison-eval-upstream 改动指南

> **目标**:复刻「OmniDocBench 文档解析 benchmark 接入 Edison」在 **edison-eval仓库**的全部改动。
> **范围**:仅 git 仓库内改动。adapter 代码(`edison_benchmark_adapters/omnidocbench`)由外部传入,本文档不涉及。
> **基线**:2026-08-25,end-to-end 跑通,limit=3 实测 avg_score=90.93。

---

## 0. 改动总览

edison-eval-upstream 仓库(branch `feat/aliyun-deploy-v2`)共 **7 处改动**,全部为工作区未提交状态:

| 文件 | 改动 | 作用 |
|------|------|------|
| `backend/app/services/external_benchmark_runner.py` | +12 行 | 后端自动填 `gt_json`,前端无需暴露路径 |
| `backend/app/services/benchmark_adapters.py` | +1 行 | 注册 runner label `external:omnidocbench:model` |
| `backend/Dockerfile` | +13 行 | 阿里云 apt 源 + docker CLI(docker-in-docker 硬需求) |
| `docker-compose.override.yml` | +16 行 | 同路径挂载 + docker.sock + 环境变量 |
| `frontend/src/lib/benchmarkAdapters.ts` | +1 行 | 前端 runner label 映射 |
| `frontend/src/pages/models/ModelDetailPage.tsx` | +1 行 | model version 的 runner 选项 |
| `frontend/src/pages/tasks/TaskWizardPage.tsx` | +19 行,-1 行 | external benchmark preset + 删除死代码 |

外部依赖(不在本仓库):
- `backend/docker-27.3.1-aarch64.tgz` (66MB,需**手动下载**放到 backend/ 目录,见第 2.1 节)
- `edison_benchmark_adapters/omnidocbench/` (adapter 代码,由外部传入)
- `OmniDocBench/full_dataset/` (数据集,adapter 运行时自动下载)

---

## 1. 后端改动(2 处)

### 1.1 `backend/app/services/external_benchmark_runner.py`

在 `_external_benchmark_params()` 函数中,`terminal-bench-2` 分支后追加 `omnidocbench` 分支。

**改造点**:先将 `_adapter_name(config)` 调用提取为变量 `adapter_name`(避免重复调用),再添加 elif。

**找到此处**(约 67 行):
```python
def _external_benchmark_params(config: dict) -> dict:
    ...
    if _adapter_name(config) == "terminal-bench-2":
        params.setdefault("timeout_multiplier", 5)
        ...
```

**改为**:
```python
def _external_benchmark_params(config: dict) -> dict:
    ...
    adapter_name = _adapter_name(config)

    if adapter_name == "terminal-bench-2":
        params.setdefault("timeout_multiplier", 5)
        params.setdefault("patch_terminus_apt", True)
        params.setdefault("container_apt_mirror", settings.EXTERNAL_BENCHMARK_CONTAINER_APT_MIRROR)

    elif adapter_name == "omnidocbench":
        # 默认使用 OmniDocBench 完整数据集(1651 样本),adapter 的 run.sh 会自动下载
        adapters_dir = _expand_path(settings.EXTERNAL_BENCHMARK_ADAPTERS_DIR)
        full_dataset = adapters_dir.parent / "OmniDocBench" / "full_dataset"
        params.setdefault("gt_json", str(full_dataset / "OmniDocBench.json"))
        # images_dir 不设,让 adapter 从 gt_json 自动推导(同级 images/)
        # predictions_dir 不设,触发自动推理

    return params
```

**作用**:前端建任务时不用传 gt_json 绝对路径,后端根据 `EXTERNAL_BENCHMARK_ADAPTERS_DIR` 环境变量推导数据集位置。`adapters_dir.parent / "OmniDocBench"` 即数据集目录(见第 3 节目录约定)。

---

### 1.2 `backend/app/services/benchmark_adapters.py`

在 `GENERIC_RUNNER_LABELS` 字典中添加 omnidocbench runner 映射。

**找到此处**(约 136 行):
```python
GENERIC_RUNNER_LABELS = {
    "external:terminal-bench-2:bash": "TerminalBench-2",
    ...
}
```

**添加一行**:
```python
GENERIC_RUNNER_LABELS = {
    "external:terminal-bench-2:bash": "TerminalBench-2",
    ...
    "external:omnidocbench:model": "OmniDocBench",
}
```

**作用**:注册 runner key,后端才能识别 `external:omnidocbench:model` 这个 runner。不注册会在任务创建时校验失败。

---

## 2. Dockerfile 改动(docker-in-docker 支持)

### 2.1 `backend/Dockerfile`

两处改动,**目的不同,缺一不可**。

**完整 Dockerfile**(替换原文件):
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# 1. 换阿里云 debian 镜像源(国内网络稳定) —— 只为加速下面的 apt-get install
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 2. docker CLI (静态二进制, 仅 client; daemon 复用宿主 socket) —— docker-in-docker 硬需求
# 需手动下载到 backend/(见下方下载方式)
COPY docker-27.3.1-aarch64.tgz /tmp/docker.tgz
RUN tar -xzf /tmp/docker.tgz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt uv

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**改动 1:阿里云源**
- 第 6 行:`sed -i 's|deb.debian.org|mirrors.aliyun.com|g'`
- 作用:加速 `apt-get install git curl ca-certificates`

**改动 2:docker CLI**
- 第 13-18 行:COPY 本地 tgz → 解压 → 只取 `docker` 二进制 → 删临时文件
- 作用:OmniDocBench adapter 用 **docker-in-docker** 跑官方评测镜像,容器内必须有 `docker` 命令
- 架构:示例用 `aarch64`(Apple Silicon),x86 换成 `docker-27.3.1-x86_64.tgz`

**为什么用 tgz 而非 apt 装 docker**(踩坑记录):
- `apt-get install docker.io` → **OOM**(拖 daemon+containerd 整套,slim 镜像扛不住)
- `curl download.docker.com` 在线下载 → **SSL 失败 exit 35**
- 本地预下 tgz + COPY + 只取 client 二进制 → ✅ 唯一跑通,且最精简

**docker CLI tgz 下载方式**:
```bash
# aarch64 (Apple Silicon)
curl -fsSLO https://download.docker.com/linux/static/stable/aarch64/docker-27.3.1.tgz
mv docker-27.3.1.tgz edison-eval-upstream/backend/

# x86_64
curl -fsSLO https://download.docker.com/linux/static/stable/x86_64/docker-27.3.1.tgz
mv docker-27.3.1.tgz edison-eval-upstream/backend/docker-27.3.1-x86_64.tgz
# 并修改 Dockerfile 第 13 行: COPY docker-27.3.1-x86_64.tgz /tmp/docker.tgz
```

⚠️ **tgz 文件 66MB,不要提交到 git**。每个环境单独下载放 `backend/` 即可。

---

## 3. docker-compose.override.yml(同路径挂载 + 环境变量)

**为什么要同路径挂载**:adapter 在 worker 容器内执行 `docker run -v {宿主路径}:...`,这个路径由**宿主 docker daemon** 解释。若容器内路径 ≠ 宿主路径,daemon 找不到文件,挂载失败。

**完整 `docker-compose.override.yml`**(新建或替换):
```yaml
services:
  backend:
    volumes:
      # 同路径挂载: 容器内路径 == 宿主路径
      - /Users/super/myself/work/edison_init/edison_benchmark_adapters:/Users/super/myself/work/edison_init/edison_benchmark_adapters
    environment:
      EXTERNAL_BENCHMARK_ADAPTERS_DIR: /Users/super/myself/work/edison_init/edison_benchmark_adapters
      EXTERNAL_BENCHMARK_RUNS_DIR: /Users/super/myself/work/edison_init/edison_external_runs

  worker:
    command: celery -A app.tasks worker -l info -c 2
    volumes:
      - ./backend:/app
      - upload_data:/data
      # adapter 代码 (同路径)
      - /Users/super/myself/work/edison_init/edison_benchmark_adapters:/Users/super/myself/work/edison_init/edison_benchmark_adapters
      # OmniDocBench 数据集: gt_json + predictions 所在 (同路径)
      - /Users/super/myself/work/edison_init/OmniDocBench:/Users/super/myself/work/edison_init/OmniDocBench
      # external benchmark 运行目录 (同路径, adapter 在此建 odb_result 供 docker run -v 挂载)
      - /Users/super/myself/work/edison_init/edison_external_runs:/Users/super/myself/work/edison_init/edison_external_runs
      # docker-in-docker: 复用宿主 daemon (macOS Docker Desktop socket)
      - /Users/super/.docker/run/docker.sock:/var/run/docker.sock
    environment:
      EXTERNAL_BENCHMARK_ADAPTERS_DIR: /Users/super/myself/work/edison_init/edison_benchmark_adapters
      EXTERNAL_BENCHMARK_RUNS_DIR: /Users/super/myself/work/edison_init/edison_external_runs
```

⚠️ **必须修改**:将所有 `/Users/super/myself/work/edison_init` 替换为**你的实际绝对路径**。硬编码是已知限制(不可移植)。

⚠️ **docker.sock 路径**:
- macOS Docker Desktop:`/Users/YOUR_USERNAME/.docker/run/docker.sock`
- Linux:`/var/run/docker.sock:/var/run/docker.sock`

**目录约定**(相对 edison_init/ 根):
```
edison_init/
├── edison-eval/          # 本仓库
├── edison_benchmark_adapters/     # adapter 代码(外部传入)
│   └── omnidocbench/
├── OmniDocBench/full_dataset/     # 数据集(adapter 自动下载)
│   ├── OmniDocBench.json
│   └── images/
└── edison_external_runs/          # 任务运行目录
```

后端 1.1 的 `adapters_dir.parent / "OmniDocBench"` 就是靠这个同级约定推导的。

---

## 4. 前端改动(3 处)

### 4.1 `frontend/src/lib/benchmarkAdapters.ts`

在 `genericRunnerLabels` 对象中添加映射。

**找到此处**(约 129 行):
```typescript
const genericRunnerLabels: Record<string, string> = {
  'external:terminal-bench-2:bash': 'TerminalBench-2',
  ...
}
```

**添加一行**:
```typescript
'external:omnidocbench:model': 'OmniDocBench',
```

---

### 4.2 `frontend/src/pages/models/ModelDetailPage.tsx`

在 `runnerOptions` 数组中添加选项。

**找到此处**(约 30 行):
```typescript
const runnerOptions = [
  { value: 'external:terminal-bench-2:bash', label: 'TerminalBench-2' },
  ...
]
```

**添加一行**:
```typescript
{ value: 'external:omnidocbench:model', label: 'OmniDocBench' },
```

---

### 4.3 `frontend/src/pages/tasks/TaskWizardPage.tsx`

两处改动:
1. 在 `externalBenchmarkPresets` 数组添加 preset
2. **删除**一行死代码

**改动 1:添加 preset**(约 184 行):
```typescript
const externalBenchmarkPresets: ExternalBenchmarkPreset[] = [
  {
    key: 'terminal-bench-2',
    label: 'TerminalBench-2',
    ...
  },
  // ↓↓↓ 添加这个 preset ↓↓↓
  {
    key: 'omnidocbench',
    label: 'OmniDocBench',
    description: '调用 worker 上的 omnidocbench adapter，评测文档解析（含 CDM 公式指标），需要 TeX Live 环境。',
    config: {
      adapter: 'omnidocbench',
      agent: 'model',
      suite: 'omnidocbench',
      task_names: '',
      limit: 1,
      runs: 1,
      timeout_seconds: 28800,
      output_dir: '~/data/edison_external_benchmarks/omnidocbench',
      result_file: 'result.json',
      params: {
        model_preflight: false,
      },
    },
  },
]
```

> ⚠️ **注意**:preset 默认 `suite: 'omnidocbench'` 与实测跑通的 `OmniDocBench/end2end` 不一致,`params` 也只有 `model_preflight`。这是前端默认值,用户可在建任务时修改。实测验证过的组合是:
> - `suite: "OmniDocBench/end2end"`
> - `params: {vlm_max_tokens: 16384, vlm_temperature: 0}`

**改动 2:删除死代码**(约 810 行):

**找到此处**:
```typescript
const isTerminalBenchExternal = form.exec_mode === 'external' && externalAdapter === 'terminal-bench-2'
const isLiveCodeBenchProExternal = form.exec_mode === 'external' && externalAdapter === 'livecodebench-pro'
const isOmniDocBenchExternal = form.exec_mode === 'external' && externalAdapter === 'omnidocbench'  // ← 删除这行
const workerRoutingEnabled = form.exec_mode === 'inspect' || form.exec_mode === 'pinchbench' || form.exec_mode === 'external'
```

**删除**:
```typescript
const isOmniDocBenchExternal = form.exec_mode === 'external' && externalAdapter === 'omnidocbench'
```

**原因**:这行声明了变量但从未使用(死代码),可能触发 TS `noUnusedLocals` 或 ESLint 错误,卡构建。曾作为预留逻辑存在,但后续 JSX 未接入,成为负债。

---

## 5. 部署验证

### 5.1 改代码后重启哪个容器?

| 改了什么 | 重启操作 | 原因 |
|---------|---------|------|
| `external_benchmark_runner.py` | `docker compose restart worker` | Celery 无 --reload,必须重启加载新 module |
| 其他后端 Python | `docker compose restart backend` | 后端改动不涉及 worker |
| Dockerfile | `docker compose build && docker compose up -d` | 镜像改了需重建 |
| docker-compose.override.yml | `docker compose down && docker compose up -d` | compose 配置改了需重启 |
| 前端 | 前端 dev server 自动 reload | 无需手动操作 |
| adapter 代码 | **无需重启** | adapter 经 run.sh 在新进程执行,直接读磁盘 |

⚠️ **常见误区**:改 `external_benchmark_runner.py` 后只重启 backend,worker 仍用旧代码 → 任务拿不到 gt_json。

### 5.2 最小验证

```bash
# 1. 确保 tgz 在 backend/
ls backend/docker-27.3.1-aarch64.tgz

# 2. 修改 docker-compose.override.yml 路径为你的绝对路径

# 3. 重建镜像 + 启动
docker compose build
docker compose up -d

# 4. 重启 worker(若改了 external_benchmark_runner.py)
docker compose restart worker

# 5. 前端建任务:
#    - exec_mode: external
#    - adapter: omnidocbench
#    - suite: OmniDocBench/end2end
#    - limit: 3
#    - params: {vlm_max_tokens: 16384, vlm_temperature: 0}

# 6. 验证 gt_json 自动填充:
#    任务启动后,查看 edison_external_runs/<TASK_ID>/input.json
#    应有 "gt_json": "/abs/path/OmniDocBench/full_dataset/OmniDocBench.json"

# 7. 等任务完成,查看分数:
#    GET /api/v1/tasks/<TASK_ID>/summary → data.avg_score
```

**实测基线**:limit=3,avg_score=90.93,scores=[85.96, 98.74, 88.08],passed=3/failed=0。

---

## 6. 踩坑教训

1. **改 `external_benchmark_runner.py` 必须重启 worker 不是 backend** —— 生成 input.json、执行任务的是 Celery worker,无 --reload。

2. **同路径挂载是硬约束** —— adapter 在容器内执行 `docker run -v {路径}:...`,宿主 daemon 解释,路径必须宿主实际存在且与容器内一致。

3. **docker.sock 路径因平台而异** —— macOS Docker Desktop 是 `~/.docker/run/docker.sock`,Linux 通常是 `/var/run/docker.sock`。

4. **禁止 push** —— compose 有硬编码绝对路径,adapter 代码不在本仓库。本项目策略:只 pull 对比,不 push。

5. **前端 preset 默认值仅供参考** —— 实测跑通的是 `suite=OmniDocBench/end2end` + vlm params,preset 里的 `suite=omnidocbench` 需用户改。

6. **死代码会卡构建** —— `isOmniDocBenchExternal` 未使用,TS strict 模式会报错,必须删。

---

## 7. 复刻检查清单

- [ ] 后端 2 处:`external_benchmark_runner.py` + `benchmark_adapters.py`
- [ ] Dockerfile:阿里云源 + docker CLI(tgz 已下载到 backend/)
- [ ] docker-compose.override.yml:同路径挂载(改成你的绝对路径)+ docker.sock
- [ ] 前端 3 处:`benchmarkAdapters.ts` + `ModelDetailPage.tsx` + `TaskWizardPage.tsx`(含删除死代码)
- [ ] 重建镜像:`docker compose build && docker compose up -d`
- [ ] 重启 worker:`docker compose restart worker`(若改了 external_benchmark_runner.py)
- [ ] adapter 代码已就位:`edison_benchmark_adapters/omnidocbench/`(由外部传入)
- [ ] 建任务验证 gt_json 自动填充 + limit=3 出分
