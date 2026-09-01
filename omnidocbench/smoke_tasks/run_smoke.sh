#!/usr/bin/env bash
set -euo pipefail

# OmniDocBench 端到端冒烟测试（不取消，跑到出分）
# 链路：登录 → 建任务(前端只传 adapter/suite/runs/limit/vlm_*) → 启动 →
#       等 input.json(验证后端自动填 gt_json) → 等推理+评测完成 → 打印分数
#
# 源自 test_omnidocbench_params_autofill.sh，删去「取消任务」一段，改为等待终态。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
BACKEND_URL="${BACKEND_URL:-http://localhost:8002}"

echo "=========================================="
echo "OmniDocBench 端到端冒烟测试（limit=3，不取消）"
echo "=========================================="
echo ""

# 步骤 1：登录获取 token
echo "📋 步骤 1：登录..."
LOGIN_RESP=$(curl -s -X POST "${BACKEND_URL}/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}')

TOKEN=$(echo "$LOGIN_RESP" | jq -r '.data.access_token // empty')
if [ -z "$TOKEN" ]; then
    echo "❌ 登录失败"
    echo "$LOGIN_RESP"
    exit 1
fi
echo "✅ 登录成功"
echo ""

# 步骤 2：获取 project_id 和 model_version_id
echo "📋 步骤 2：获取 project 和 model_version..."
PROJECT_ID=$(curl -s "${BACKEND_URL}/api/v1/projects" -H "Authorization: Bearer $TOKEN" | jq -r '.data[0].id')
MODEL_VERSION_ID=$(curl -s "${BACKEND_URL}/api/v1/models/1995ac66-964e-4f24-8c7e-415828f03bb8/versions" -H "Authorization: Bearer $TOKEN" | jq -r '.data[0].id')
echo "   Project: $PROJECT_ID"
echo "   Model Version: $MODEL_VERSION_ID"
echo ""

# 步骤 3：创建任务（前端不传 params.gt_json）
echo "📋 步骤 3：创建任务（前端不传 gt_json）..."
CREATE_RESP=$(curl -s -X POST "${BACKEND_URL}/api/v1/tasks" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{
    \"name\": \"ODB冒烟测试-端到端\",
    \"description\": \"验证后端自动填 gt_json + limit 裁剪 + 进度条 + 真实出分\",
    \"project_id\": \"$PROJECT_ID\",
    \"exec_mode\": \"external\",
    \"config\": {
      \"adapter\": \"omnidocbench\",
      \"agent\": \"model\",
      \"suite\": \"OmniDocBench/end2end\",
      \"runs\": 1,
      \"limit\": 3,
      \"params\": {
        \"vlm_max_tokens\": 16384,
        \"vlm_temperature\": 0
      }
    },
    \"model_version_id\": \"$MODEL_VERSION_ID\"
  }")

TASK_ID=$(echo "$CREATE_RESP" | jq -r '.data.id // empty')
if [ -z "$TASK_ID" ]; then
    echo "❌ 任务创建失败"
    echo "$CREATE_RESP"
    exit 1
fi
echo "✅ 任务创建成功：$TASK_ID"
echo ""
# 步骤 4：启动任务
echo "📋 步骤 4：启动任务..."
curl -s -X POST "${BACKEND_URL}/api/v1/tasks/${TASK_ID}/start" \
  -H "Authorization: Bearer $TOKEN" > /dev/null
echo "✅ 任务已启动"
echo ""

# 步骤 5：等 input.json 生成并验证后端自动填 gt_json
echo "📋 步骤 5：验证后端自动填 gt_json..."
INPUT_JSON_PATH="${REPO_ROOT}/edison_external_runs/${TASK_ID}/input.json"
for i in {1..20}; do
    [ -f "$INPUT_JSON_PATH" ] && break
    sleep 1
done
if [ ! -f "$INPUT_JSON_PATH" ]; then
    echo "❌ input.json 未生成（任务可能启动失败）"
    exit 1
fi
GT_JSON=$(jq -r '.benchmark.params.gt_json // empty' "$INPUT_JSON_PATH")
if [ -z "$GT_JSON" ]; then
    echo "❌ params.gt_json 为空（后端未自动填充）"
    jq '.benchmark.params' "$INPUT_JSON_PATH"
    exit 1
fi
echo "✅ gt_json 已自动填充：$GT_JSON"
echo ""

# 步骤 6：等推理 + 评测跑到终态（不取消）
echo "📋 步骤 6：等待推理 + 评测完成（每 5s 轮询进度）..."
STATUS=""
for i in {1..180}; do
    RESP=$(curl -s "${BACKEND_URL}/api/v1/tasks/${TASK_ID}" -H "Authorization: Bearer $TOKEN")
    STATUS=$(echo "$RESP" | jq -r '.data.status')
    PROG=$(echo "$RESP" | jq -r '"\(.data.progress_current)/\(.data.progress_total)"')
    echo "   [$i] status=$STATUS progress=$PROG"
    case "$STATUS" in
        completed|failed|cancelled) break ;;
    esac
    sleep 5
done
echo ""

if [ "$STATUS" != "completed" ]; then
    echo "❌ 任务未成功完成：status=$STATUS"
    echo "   worker 日志：docker logs edison-eval-upstream-worker-1 --tail 50"
    exit 1
fi

# 步骤 7：打印分数
echo "📋 步骤 7：评测结果..."
curl -s "${BACKEND_URL}/api/v1/tasks/${TASK_ID}/summary" -H "Authorization: Bearer $TOKEN" \
  | jq '.data | {avg_score, score_stddev, score_breakdown: .metrics_agg.score_breakdown}'
echo ""

echo "=========================================="
echo "✅ 冒烟测试通过！"
echo "=========================================="
echo "任务 ID: $TASK_ID"
echo "input.json: $INPUT_JSON_PATH"
