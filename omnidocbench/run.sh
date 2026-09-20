#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DATASET_DIR="${REPO_ROOT}/OmniDocBench/full_dataset"

# 检测完整数据集（GT + 全部图片），不完整则下载。完整数据集为 1651 样本。
# hf download 支持断点续传，重复调用会补齐缺失文件，已存在的文件秒过。
EXPECTED_IMAGES=1651
GT_FILE="${DATASET_DIR}/OmniDocBench.json"

# Do not use `ls ... | wc -l` here. With `set -o pipefail`, a missing images
# directory makes `ls` return non-zero and exits the script before the
# first-run download branch gets a chance to create the dataset directory.
count_images() {
    local images_dir="${DATASET_DIR}/images"
    if [ ! -d "${images_dir}" ]; then
        echo 0
        return
    fi
    find "${images_dir}" -maxdepth 1 -type f \( \
        -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' \
    \) | wc -l | tr -d ' '
}

resolve_hf_bin() {
    if [ -n "${OMNIDOCBENCH_HF_BIN:-}" ]; then
        if [ -x "${OMNIDOCBENCH_HF_BIN}" ]; then
            echo "${OMNIDOCBENCH_HF_BIN}"
            return
        fi
        echo "[OmniDocBench] 错误：OMNIDOCBENCH_HF_BIN 不可执行: ${OMNIDOCBENCH_HF_BIN}" >&2
        return 1
    fi

    if command -v hf >/dev/null 2>&1; then
        command -v hf
        return
    fi

    # Workers already maintain the inspect_evals uv environment. Celery does
    # not necessarily include that environment's bin directory in PATH, so
    # use its hf CLI directly before asking operators to install another copy.
    local inspect_hf="${REPO_ROOT}/inspect_evals/.venv/bin/hf"
    if [ -x "${inspect_hf}" ]; then
        echo "${inspect_hf}"
        return
    fi

    return 1
}

IMG_COUNT="$(count_images)"

if [ ! -f "${GT_FILE}" ] || [ "${IMG_COUNT}" -lt "${EXPECTED_IMAGES}" ]; then
    echo "[OmniDocBench] 数据集不完整（GT存在=$([ -f "${GT_FILE}" ] && echo 是 || echo 否), 图片=${IMG_COUNT}/${EXPECTED_IMAGES}），开始下载/续传到 ${DATASET_DIR} ..." >&2
    mkdir -p "${DATASET_DIR}"
    if HF_BIN="$(resolve_hf_bin)"; then
        # 下载整个 dataset 仓库：包含 OmniDocBench.json(GT) + images/(1651张图片)
        echo "[OmniDocBench] 使用 hf CLI: ${HF_BIN}" >&2
        # Edison workers may run cached Inspect benchmarks with global HF
        # offline flags. OmniDocBench's first-run bootstrap explicitly needs
        # network access, so disable offline mode only for this child process.
        HF_HUB_OFFLINE=0 HF_DATASETS_OFFLINE=0 \
            "${HF_BIN}" download opendatalab/OmniDocBench \
            --repo-type dataset --local-dir "${DATASET_DIR}" --quiet
    else
        echo "[OmniDocBench] 错误：找不到 hf CLI；请先在 inspect_evals 执行 uv sync --frozen，或设置 OMNIDOCBENCH_HF_BIN" >&2
        exit 1
    fi
    IMG_COUNT="$(count_images)"
    echo "[OmniDocBench] 下载完成: GT + ${IMG_COUNT} 张图片" >&2
    if [ ! -f "${GT_FILE}" ] || [ "${IMG_COUNT}" -lt "${EXPECTED_IMAGES}" ]; then
        echo "[OmniDocBench] 错误：数据集下载不完整（GT或图片缺失），请检查网络后重试" >&2
        exit 1
    fi
else
    echo "[OmniDocBench] 使用已缓存数据集: ${DATASET_DIR} (GT + ${IMG_COUNT} 张图片)" >&2
fi

PYTHON_BIN="${EXTERNAL_BENCHMARK_PYTHON_BIN:-${PYTHON_BIN:-python3}}"
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/run_omnidocbench.py" "$@"
