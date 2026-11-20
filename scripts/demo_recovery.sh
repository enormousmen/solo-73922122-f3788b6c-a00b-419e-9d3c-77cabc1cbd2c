#!/usr/bin/env bash
#
# 演示「从旧备份恢复」全流程：停发 -> 补登 -> 幂等重放 -> 恢复发证 -> 结单。
#
# 前置：
#   RECOVERY_ID=rec-demo-01 docker compose up --build -d
#   （compose 里 RECOVERY_ID 默认留空 = 正常发证；设了编号才进入恢复模式）
#
# 用法： ./scripts/demo_recovery.sh   （可用 BASE_URL / RECOVERY_ID 环境变量覆盖）
#
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
RID="${RECOVERY_ID:-rec-demo-01}"
APPROVED="2026-11-01T08:00:00Z"   # 许可原来的获批时间（旧备份丢失发证记录之前）
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

get()  { echo; echo "== GET $1 ==";  curl -sS -w '\n[HTTP %{http_code}]\n' "$BASE$1"; }
post() { echo; echo "== POST $1 =="; curl -sS -w '\n[HTTP %{http_code}]\n' -X POST "$BASE$1" -H 'Content-Type: application/json' -d "$2"; }

echo "服务: $BASE   恢复编号: $RID"

# 0. 恢复状态：应为 pending（停发中）
get /recovery

# 1. 停发：恢复完成前申请新许可一律 503
post /permits '{"request_id":"req-100","nodes":["n5"]}'

# 2. 探活照常接收（执行器/节点上报不受停发影响）
post /liveness "{\"reports\":[
  {\"node_id\":\"n1\",\"sampled_at\":\"$NOW\",\"healthy\":true,\"boot_id\":\"boot-1\"},
  {\"node_id\":\"n2\",\"sampled_at\":\"$NOW\",\"healthy\":true,\"boot_id\":\"boot-1\"},
  {\"node_id\":\"n3\",\"sampled_at\":\"$NOW\",\"healthy\":true,\"boot_id\":\"boot-1\"},
  {\"node_id\":\"n4\",\"sampled_at\":\"$NOW\",\"healthy\":true,\"boot_id\":\"boot-1\"},
  {\"node_id\":\"n5\",\"sampled_at\":\"$NOW\",\"healthy\":true,\"boot_id\":\"boot-1\"}]}"

# 3. 补登：执行器确认仍未结单的全部获批回执（沿用原申请号/节点/获批时间/启动标识）
post /recovery "{
  \"recovery_id\":\"$RID\",
  \"permits\":[
    {\"request_id\":\"req-001\",\"nodes\":[\"n1\"],
     \"approved_at\":\"$APPROVED\",\"boot_ids_at_approval\":{\"n1\":\"boot-1\"}},
    {\"request_id\":\"req-002\",\"nodes\":[\"n2\"],
     \"approved_at\":\"$APPROVED\",\"boot_ids_at_approval\":{\"n2\":\"boot-1\"}}]}"

# 4. 幂等：同编号重发同一份清单 -> 200 返回首次成功结果
post /recovery "{
  \"recovery_id\":\"$RID\",
  \"permits\":[
    {\"request_id\":\"req-001\",\"nodes\":[\"n1\"],
     \"approved_at\":\"$APPROVED\",\"boot_ids_at_approval\":{\"n1\":\"boot-1\"}},
    {\"request_id\":\"req-002\",\"nodes\":[\"n2\"],
     \"approved_at\":\"$APPROVED\",\"boot_ids_at_approval\":{\"n2\":\"boot-1\"}}]}"

# 5. 同编号换清单 -> 409 冲突
post /recovery "{
  \"recovery_id\":\"$RID\",
  \"permits\":[
    {\"request_id\":\"req-001\",\"nodes\":[\"n1\"],
     \"approved_at\":\"$APPROVED\",\"boot_ids_at_approval\":{\"n1\":\"boot-1\"}}]}"

# 6. 恢复完成，发证恢复
get /recovery
post /permits '{"request_id":"req-100","nodes":["n5"]}'

# 7. 补登的许可参与占用判定：n1 已被 req-001 占用
post /permits '{"request_id":"req-101","nodes":["n1"]}'

# 8. 结单条件不满足：n1 最新探活启动标识未变（节点还没重启）
post /permits/req-001/finish ''

# 9. n1 重启完成，上报新启动标识 -> 按原规则结单
LATER="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
post /liveness "{\"reports\":[{\"node_id\":\"n1\",\"sampled_at\":\"$LATER\",\"healthy\":true,\"boot_id\":\"boot-2\"}]}"
post /permits/req-001/finish ''

# 10. n2 同样重启上报 -> req-002 结单
LATER="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
post /liveness "{\"reports\":[{\"node_id\":\"n2\",\"sampled_at\":\"$LATER\",\"healthy\":true,\"boot_id\":\"boot-2\"}]}"
post /permits/req-002/finish ''

# 11. 终态一览
get '/permits'
echo
echo "演示完成。"
