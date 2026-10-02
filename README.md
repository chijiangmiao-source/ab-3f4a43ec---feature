# 星载递归载荷兼容审计

在升级星载指令发送端、重新定义载荷之前，确认**新版发送端可能产生的每一种递归载荷**
都能被在轨接收端接受。系统以“发送端是生产者、接收端是消费者”裁决
**协归（coinductive）结构子类型**，结论按稳定审计标识冻结。

供应商改名并重排声明后，可把已判定兼容的冻结审计**迁移**到新版契约：
仅当新版两侧声明分别与来源对应声明**完全结构等价**时签发迁移结论，
并返回稳定选出的完整名称双射。

## 它保证什么

- **精确的生产者 ⪯ 消费者结构子类型**
  - 接收端要求的必需字段必须由发送端以必需字段提供；接收端可选字段缺失合法，
    存在则值类型相容；发送端多余字段合法。
  - 发送端可能发出的每个变体标签都必须在接收端标签集合中，载荷类型相容；
    接收端多余标签合法。
  - 基本类型 `int / bool / text` 仅与自身相容。
- **递归以协归关系裁决**：沿具名引用展开，环上的比较对以“假设-复用”收口，
  **不展开到固定深度，也不用抽样实例代替**；每个被复用的递归比较对都会在结论中标出。
- **稳定首个违约**：字段按接收端声明顺序、标签按发送端声明顺序遍历，
  返回确定性的首个 `字段缺失 / 可空性变化 / 额外标签 / 基本类型违约`，并给出类型路径。
- **契约问题一次反馈**：未定义引用、重复类型名/字段/标签、无保护别名环、
  不合法标识、超过 24 个类型等，一次性全部返回。
- **结论冻结**：相同 `audit_id` 重传相同契约读取原结论（`frozen_at` 不变）；
  改变任一契约返回 `409` 且绝不改写原结论。

## 迁移到改名新版保证什么

- **完全结构等价才签发**：新版发送端、接收端声明分别与来源冻结审计的对应声明
  逐一等价——允许**类型声明顺序重排**与**类型名整体替换**，但记录字段名及其顺序、
  必需性、变体标签及其顺序、基础类型必须保持，递归引用所指必须对应。
- **覆盖全部具名声明**：自根不可达的定义同样参与等价；新版根类型必须是
  来源根类型的重命名像。
- **完整名称双射**：结论携带发送端、接收端两份 `来源名 → 新名` 的完整双射；
  双射不唯一时按类型名字典序稳定选出，与声明书写顺序无关。
- **明确拒绝且不落盘**：来源审计不存在（`404`）或结论不兼容（`422`）、
  声明数量或不可达定义不一致（`422`）、无法建立双射（`422`）——
  均不写入任何记录，修正后可按同一迁移标识重新提交。
- **迁移结论同样冻结**：相同 `migration_id` 重传相同请求读取原结论；
  改换来源审计或新版契约返回 `409` 且绝不改写；重启后仍可按标识重开。
  原兼容审计及其 API 行为不受影响。

## 类型模型（JSON）

```json
{"kind":"int"} | {"kind":"bool"} | {"kind":"text"}
{"kind":"record","fields":[{"name":"x","type":Type,"required":true}]}
{"kind":"variant","tags":[{"label":"A","type":Type}]}
{"kind":"ref","name":"TypeName"}
```

每套声明为列表 `[{"name": "...", "type": Type}, ...]`（至多 24 个）。
“无保护别名环”指仅由 `ref`/别名构成、不经过任何 `record`/`variant`
受保护节点的环（如 `A = ref B; B = ref A`），这类类型没有确定结构，拒绝。
受保护递归（如 `List = variant{Nil, Cons{head, tail: List}}`）合法。

## 启动（容器化）

```bash
# 默认宿主端口 8080；可用 HOST_PORT 配置
HOST_PORT=18080 docker compose up -d --build
curl -s http://localhost:18080/health      # {"status":"ok"}
# 浏览器打开 http://localhost:18080/ 提交/重开结论
```

结论持久化在命名卷 `audit-data`（审计与迁移结论同卷分目录），容器重启后仍可按标识重开。

## 单次验收组件 verify

`verify` 服务只运行一次并以退出码报告（复核递归兼容/字段缺失/额外变体/
改名等价 → pytest → 构建检查 → 审计与迁移接口冒烟）：

```bash
docker compose build verify
docker compose run --rm verify      # 退出码 0 即验收通过
```

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify.py                       # 不依赖 docker 的完整验收
AUDIT_DATA_DIR=./data PORT=8080 .venv/bin/python -m app.main
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/audits` | 提交或幂等重传；新建 `201`、相同契约重传 `200`、契约冲突 `409`、契约非法 `400`（`issues[]` 一次返回） |
| GET | `/api/audits/{audit_id}` | 重开冻结结论，不存在返回 `404` |
| POST | `/api/migrations` | 提交或幂等重传迁移；新建 `201`、相同请求重传 `200`、改换来源或新版契约 `409`、新版契约非法 `400`、来源缺失 `404`、来源不兼容或不等价 `422`（`reason` 区分原因） |
| GET | `/api/migrations/{migration_id}` | 重开冻结迁移结论，不存在返回 `404` |

提交体：

```json
{
  "audit_id": "CMD-AUDIT-2026-001",
  "root_name": "Cmd",
  "sender_types": [ /* 发送端具名类型声明 */ ],
  "receiver_types": [ /* 接收端具名类型声明 */ ]
}
```

发送端只有一个类型时 `root_name` 可省略；否则必须在两套声明中都存在。

迁移提交体：

```json
{
  "migration_id": "CMD-MIGRATION-2026-001",
  "source_audit_id": "CMD-AUDIT-2026-001",
  "new_root_name": "Command",
  "new_sender_types": [ /* 新版发送端具名类型声明（可改名/重排） */ ],
  "new_receiver_types": [ /* 新版接收端具名类型声明（可改名/重排） */ ]
}
```

迁移结论携带 `sender_mapping` / `receiver_mapping` 两份完整名称双射
（`来源类型名 → 新版类型名`）、来源契约指纹与冻结时间。`422` 拒绝的
`reason`：`source-audit-incompatible`、`declaration-count-mismatch`、
`unreachable-definitions-mismatch`、`name-bijection-not-found`。

## 目录

- `app/models.py` — 领域模型、违约/复用点/审计与迁移结论
- `app/parser.py` — JSON 解析与全部静态校验（问题一次反馈），审计与迁移共用
- `app/subtype.py` — 协归结构子类型引擎
- `app/equivalence.py` — 改名等价引擎：完整名称双射的稳定选出
- `app/storage.py` — 审计契约指纹（SHA-256）、冻结/幂等/冲突
- `app/migration.py` — 迁移签发与冻结存储（`migrations/` 子目录，独立于审计）
- `app/api.py`, `app/main.py` — HTTP API 与入口
- `app/static/` — 提交/迁移与重开页面（真实 API）
- `scripts/verify.py` — 单次验收组件
- `tests/` — 71 项测试（递归、互递归、稳定性、冻结、改名等价、迁移、API）
