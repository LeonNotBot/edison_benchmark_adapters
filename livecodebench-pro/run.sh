#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      echo "Usage: run.sh --config <edison-input.json>"
      exit 0
      ;;
    --config)
      CONFIG="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${CONFIG}" ]]; then
  echo "Usage: run.sh --config <edison-input.json>" >&2
  exit 2
fi

PYTHON_BIN="${EXTERNAL_BENCHMARK_PYTHON_BIN:-${PYTHON_BIN:-python3}}"
exec "${PYTHON_BIN}" "${SCRIPT_DIR}/scripts/run_livecodebench.py" --config "${CONFIG}"
