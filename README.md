# restart-permit-api

存储集群值班员用的**分批重启许可 API**。FastAPI + SQLite，单容器即可运行。

核心规则：

- 服务启动时读取**固定的分片投票副本清单**（`manifest.json`）。
- 节点探活报告携带**采样时间、健康状态、启动标识**；每个节点以**采样时间最新**的一份为准。
- 值班员提交**申请号 + 拟重启的一批节点**，满足以下条件才批准整批：
  1. 本批每个节点最新探活均健康；
  2. 本批节点未被任何**未结束许可**占用；
  3. 扣除 *本批节点 + 未结束许可占用节点 + 健康未知/异常节点* 后，本批涉及的**每个分片**剩余可用投票副本均**过半**；否则拒绝并列出受影响分片。
- **并发申请**在写事务（`BEGIN IMMEDIATE`）中串行判定，守住多数票。
- **同号同批重发**返回原许可（HTTP 200）；同号不同批返回 409。
- 许可与探活记录落盘 SQLite，**服务重启后保留**。
- **结束许可**要求本批每个节点的最新探活：在获批后采集、健康、启动标识与获批时不同。
- **从旧备份恢复**时可在 Compose 配置**恢复编号**：该编号完成恢复前**暂停发新许可**（探活、查询、结单照常），值班员提交执行器确认未结单的获批回执完成**补登**后恢复发证。详见[从旧备份恢复](#从旧备份恢复补登已领走的许可)。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互文档 http://localhost:8000/docs
```

- SQLite 数据在命名卷 `permit-data`（容器内 `/data/permits.db`），重启不丢。
- 分片清单 `./manifest.json` 只读挂载进容器；修改后需 `docker compose restart` 生效。

本地开发（不用 Docker）：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
DB_PATH=./data/permits.db MANIFEST_PATH=./manifest.json \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pytest tests/ -q
```

## 分片清单格式

```json
{
  "shards": {
    "shard-a": ["n1", "n2", "n3"],
    "shard-b": ["n2", "n3", "n4"],
    "shard-c": ["n3", "n4", "n5"]
  }
}
```

每个分片列出全部投票副本节点；过半线 = `floor(票数/2)+1`（3 票需 2 可用）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 服务健康检查 |
| GET | `/manifest` | 查看分片清单与各分片过半线 |
| POST | `/liveness` | 批量上报探活（201） |
| GET | `/liveness` | 全部节点最新探活 |
| GET | `/liveness/{node}` | 单节点最新探活 |
| POST | `/permits` | 申请许可（201 新批 / 200 幂等重发 / 409 拒绝 / 503 恢复停发中） |
| GET | `/permits?status=open\|finished` | 许可列表 |
| GET | `/permits/{request_id}` | 单个许可 |
| POST | `/permits/{request_id}/finish` | 结束许可（200 / 409 条件不满足） |
| GET | `/recovery` | 恢复状态（是否配置编号、停发中/已完成） |
| POST | `/recovery` | 提交恢复补登清单（201 完成 / 200 幂等重放 / 409 拒绝或冲突） |

## 调用示例

### 1. 上报探活

```bash
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports": [
      {"node_id":"n1","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n2","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n3","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n4","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"},
      {"node_id":"n5","sampled_at":"2026-11-19T10:00:00Z","healthy":true,"boot_id":"boot-1"}
    ]}'
# => {"accepted":5}
```

### 2. 申请许可

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","nodes":["n1"]}'
# 201 =>
# {"request_id":"req-001","nodes":["n1"],"status":"open",
#  "approved_at":"...","finished_at":null,
#  "boot_ids_at_approval":{"n1":"boot-1"}}
```

同号同批重发，返回原许可（200，`approved_at` 不变）；同号不同批返回 409。

### 3. 多数票不足被拒（列出受影响分片）

`req-001` 已占用 n1 时再申请 n2（同属 shard-a，3 票最多扣 1 票）：

```bash
curl -X POST http://localhost:8000/permits \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-002","nodes":["n2"]}'
# 409 =>
# {"detail":{"ok":false,"error":"permit_rejected",
#   "affected_shards":[{"shard":"shard-a","voters":3,"required_available":2,
#     "available":1,"available_nodes":["n3"],
#     "excluded":{"n1":"occupied_by:req-001","n2":"in_batch"}}]}}
```

其它拒绝类别：`health_unknown_nodes`（健康未知）、`unhealthy_nodes`（异常）、
`occupied_nodes`（被未结束许可占用）。清单外节点一律 422。

### 4. 节点重启后上报新探活，结束许可

```bash
# 节点重启完成：采样时间晚于获批时间、健康、启动标识已变
curl -X POST http://localhost:8000/liveness \
  -H 'Content-Type: application/json' -d '{
    "reports":[{"node_id":"n1","sampled_at":"2026-11-19T10:20:00Z",
                "healthy":true,"boot_id":"boot-2"}]}'

curl -X POST http://localhost:8000/permits/req-001/finish
# 200 => {"request_id":"req-001",...,"status":"finished","finished_at":"..."}
```

结束条件不满足时 409 并给出逐节点原因，例如：

```json
{"detail":{"error":"finish_conditions_not_met",
           "nodes":{"n1":"boot_id_unchanged"}}}
```

可能原因：`no_liveness_report`、`not_sampled_after_approval`、`unhealthy`、`boot_id_unchanged`。
许可结束后节点被释放，可被后续申请占用。

## 从旧备份恢复（补登已领走的许可）

场景：用**旧 SQLite 备份**恢复服务，库里可能缺少执行器已领走、仍未结单的许可。
此时直接发证会漏判节点占用与多数票，必须先补登再恢复发证。

### 1. 配置恢复编号启动

在 `docker-compose.yml` 的 `RECOVERY_ID` 填一个**新的唯一编号**（或用环境变量覆盖）：

```bash
RECOVERY_ID=rec-2026-11-20-01 docker compose up -d
```

- 该编号**完成恢复前**：`POST /permits` 一律 **503**（停发新许可，含幂等重发）；
  探活上报、查询、结束许可**照常**。
- **留空/不配置** `RECOVERY_ID`：正常发证，`POST /recovery` 返回 409 `recovery_not_configured`。
- 之后若再次从旧备份恢复，**换一个新编号**重启即可（旧编号的恢复结果不影响新编号）。

### 2. 提交补登清单

值班员汇总各执行器**确认仍未结单**的全部获批回执，一次性提交（空清单表示无未结许可）：

```bash
curl -X POST http://localhost:8000/recovery \
  -H 'Content-Type: application/json' -d '{
    "recovery_id": "rec-2026-11-20-01",
    "permits": [
      {"request_id": "req-001", "nodes": ["n1"],
       "approved_at": "2026-11-19T10:00:00Z",
       "boot_ids_at_approval": {"n1": "boot-1"}},
      {"request_id": "req-002", "nodes": ["n2"],
       "approved_at": "2026-11-19T10:05:00Z",
       "boot_ids_at_approval": {"n2": "boot-7"}}
    ]}'
# 201 => {"status":"completed","restored":["req-001","req-002"],"reused":[],...}
```

- 补登**沿用原申请号、节点、获批时间和获批时各节点的启动标识**，不做健康/多数票复查
  （它们是已发出的历史许可）；补登后立即参与占用与多数票判定，可按原规则结单。
- 库中**同号同内容的未结许可直接复用**（计入 `reused`，原记录不变），其余新补登（计入 `restored`）。
- **补登与恢复发证在同一事务生效**：清单落库与"恢复完成"标记一起提交，提交即刻恢复发证。

### 3. 整批拒绝（可修正后重试）

以下任一问题都导致**整批拒绝**（HTTP 409 `recovery_batch_rejected`，`problems` 列出全部问题），
**不落任何数据、继续停发**，修正清单后用同一编号重试即可：

| 问题 error | 含义 |
|---|---|
| `duplicate_request_id` | 清单内同号条目内容不一致（同内容重复会自动去重） |
| `node_conflict_in_batch` | 清单内不同许可占用同一节点 |
| `permit_already_finished` | 库中同号许可**已结单** |
| `permit_content_mismatch` | 库中同号未结许可**内容不符**（响应附库中现有内容） |
| `node_occupied` | 清单许可与库中**另一未结许可**占用同一节点 |

清单外节点 422 `unknown_nodes`；`boot_ids_at_approval` 的节点集合与 `nodes` 不一致 422 `boot_ids_mismatch`；
`recovery_id` 与服务配置不一致 409 `recovery_id_mismatch`。

> 注意：恢复只登记清单内的许可。库中遗留的未结许可若实际已结单（执行器未列入清单），
> 需按正常流程 `POST /permits/{id}/finish` 结单，不会自动关闭。

### 4. 恢复结果的幂等性

恢复成功后（结果落盘 SQLite，**重启后同样生效**）：

- **同编号 + 同一份清单**重发 → **200 返回首次成功结果**（节点顺序、键序不同视为同一份）；
- **同编号 + 不同清单** → **409 `recovery_conflict`**。

### 5. 演示脚本

`scripts/demo_recovery.sh` 演示完整流程（停发 → 探活照常 → 补登 → 幂等重放/冲突 →
恢复发证 → 占用判定 → 结单）：

```bash
RECOVERY_ID=rec-demo-01 docker compose up --build -d
./scripts/demo_recovery.sh
```

## 设计说明

- **幂等**：`request_id` 是许可表主键。节点集合规范化（去重排序）后比较，
  同号同批重放直接返回原许可；同号不同批 409。被拒绝的申请不落库，整改后可用同号重试。
- **并发**：所有写路径走单连接 + 可重入锁 + `BEGIN IMMEDIATE` 事务，
  "检查多数票 → 写入许可"是原子的；并发申请串行判定，不会超批。
- **持久化**：许可与全部探活报告存 SQLite（WAL 模式），最新探活按
  `max(sampled_at)` 实时计算，服务重启后状态完整恢复。
- **恢复**：`recoveries` 表记录已完成恢复的编号、规范化清单与首次成功响应。
  配置了 `RECOVERY_ID` 且库中无该编号记录时 `POST /permits` 停发（503）；
  `POST /recovery` 在单个 `BEGIN IMMEDIATE` 事务里完成"校验 → 补登 → 记录恢复完成"，
  提交即恢复发证；重放比对规范化清单，同单返回首存响应、异单 409。
- **结束判定**：批准时快照各节点 `boot_id`；结束时要求最新报告
  `sampled_at > approved_at` 且健康且 `boot_id` 不同。

## 已知限制 / 未完成项

- 单 worker 运行（SQLite 单写者）；如需多副本需换外部数据库。
- 未做认证鉴权，假定部署在受信内网；如需可前置网关或加 token。
- 探活无"过期"概念：只要最新一份健康即视为健康，不判断采样时间新旧。
- 结束许可比较的是节点上报的采样时钟与服务端获批时钟，依赖各节点时钟基本同步（NTP）。
- 许可无过期/自动回收机制；值班员需显式 finish。
