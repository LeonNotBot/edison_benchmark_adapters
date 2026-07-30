#!/usr/bin/env bash
set -euo pipefail

mkdir -p /logs/verifier

target="/tmp/edison_hermes_container_preflight_ok.txt"
if [[ -f "${target}" ]] && [[ "$(tr -d '\r\n ' < "${target}")" == "OK" ]]; then
  echo "Hermes container preflight file exists and contains OK."
  echo 1 > /logs/verifier/reward.txt
else
  echo "Hermes container preflight failed: ${target} was not created with exact content OK."
  if [[ -f "${target}" ]]; then
    echo "Actual content:"
    cat "${target}" || true
  fi
  echo 0 > /logs/verifier/reward.txt
fi
