#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DATASET_DIR="${REPO_ROOT}/OmniDocBench/full_dataset"

# 检测完整数据集（GT + 全部图片），不完整则下载。完整数据集为 1651 样本。
# hf download 支持断点续传，重复调用会补齐缺失文件，已存在的文件秒过。
EXPECTED_IMAGES=1651
GT_FILE="${DATASET_DIR}/OmniDocBench.json"
IMG_COUNT=$(ls "${DATASET_DIR}/images" 2>/dev/null | wc -l | tr -d ' ')

if [ ! -f "${GT_FILE}" ] || [ "${IMG_COUNT}" -lt "${EXPECTED_IMAGES}" ]; then
    echo "[OmniDocBench] 数据集不完整（GT存在=$([ -f "${GT_FILE}" ] && echo 是 || echo 否), 图片=${IMG_COUNT}/${EXPECTED_IMAGES}），开始下载/续传到 ${DATASET_DIR} ..." >&2
    mkdir -p "${DATASET_DIR}"
    if command -v hf &>/dev/null; then
        # 下载整个 dataset 仓库：包含 OmniDocBench.json(GT) + images/(1651张图片)
        hf download opendatalab/OmniDocBench --repo-type dataset --local-dir "${DATASET_DIR}" --quiet
    else
        echo "[OmniDocBench] 错误：需要 hf CLI，请安装: pip install 'huggingface-hub[cli]'" >&2
        exit 1
    fi
    IMG_COUNT=$(ls "${DATASET_DIR}/images" 2>/dev/null | wc -l | tr -d ' ')
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
