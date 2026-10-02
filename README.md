# 星载递归载荷兼容审计

在升级星载指令发送端、重新定义载荷之前，确认**新版发送端可能产生的每一种递归载荷**
都能被在轨接收端接受。系统以“发送端是生产者、接收端是消费者”裁决
**协归（coinductive）结构子类型**，结论按稳定审计标识冻结。

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

## 迁移签证：供应商改名并重排声明后的新版

已判定兼容的冻结审计可以申请**迁移签证**：工程师填写迁移标识、来源审计标识及
**新版发送端声明、新版接收端声明与新版根类型**。服务仅在新版两侧分别与来源对应
声明**完全结构等价**（同构）时签发迁移结论。

- **允许**：类型声明在列表中的顺序变化、类型名整体替换（环上的递归引用按双射解析）。
- **必须保持**：记录字段名与字段必需性、变体标签集合与各标签载荷、
  基本类型 `int / bool / text`。
- **双射**：结论返回发送端与接收端各自**稳定选出的完整具名类型名双射**
  （来源 → 新版，按来源名排序）。不可达的孤立声明也必须存在保引用结构的同构配对。
- **明确拒绝且不改写任何已冻结记录**：来源审计不存在或结论不兼容；
  新版任一侧具名声明数量或不可达定义数量不一致；无法建立双射（含一对多、
  多对一、深层字段/标签/基础类型/必需性偏差）。拒绝原因一次全部返回。
- **冻结与冲突**：相同 `migration_id` 重传相同请求重开原签证（`frozen_at` 不变）；
  以同一迁移标识**改换来源审计标识或新版契约**返回 `409`，绝不改写原签证。
  被拒绝的迁移（`422`）不落盘；服务重启后仍可按标识重开原签证。
- 原兼容审计及其 API 行为完全保持可用，迁移不改动任何审计记录。

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

结论持久化在命名卷 `audit-data`，容器重启后仍可按标识重开。

## 单次验收组件 verify

`verify` 服务只运行一次并以退出码报告（复核递归兼容/字段缺失/额外变体 →
pytest → 构建检查 → 审计接口冒烟 → 迁移签证接口冒烟）：

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
| POST | `/api/migrations` | 申请或幂等重传迁移签证；签发 `201`、相同请求重传 `200`、改换来源/新版契约 `409`、请求非法 `400`（`issues[]`）、等价不成立 `422`（`reasons[]` 一次返回） |
| GET | `/api/migrations/{migration_id}` | 重开冻结迁移签证，不存在返回 `404` |

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

迁移请求体：

```json
{
  "migration_id": "CMD-MIGRATION-2026-001",
  "source_audit_id": "CMD-AUDIT-2026-001",
  "new_root_name": "Command",
  "new_sender_types": [ /* 新版发送端具名类型声明，顺序可与来源不同 */ ],
  "new_receiver_types": [ /* 新版接收端具名类型声明，顺序可与来源不同 */ ]
}
```

迁移涉及改名，`new_root_name` 必填且必须在新版两套声明中都存在。
签证结论含 `sender_bijection` 与 `receiver_bijection`，均为
`[{"source": "来源类型名", "target": "新版类型名"}, ...]`。

## 目录

- `app/models.py` — 领域模型、违约/复用点/结论、迁移载荷与签证
- `app/parser.py` — JSON 解析与全部静态校验（问题一次反馈）
- `app/subtype.py` — 协归结构子类型引擎
- `app/equivalence.py` — 迁移结构等价判定与具名类型完整双射（含不可达同构搜索）
- `app/storage.py` — 契约指纹（SHA-256）、冻结/幂等/冲突（审计 + 迁移签证）
- `app/api.py`, `app/main.py` — HTTP API 与入口
- `app/static/` — 提交、重开与迁移签证页面（真实 API）
- `scripts/verify.py` — 单次验收组件
- `tests/` — 68 项测试（递归、互递归、稳定性、冻结、等价双射、迁移与 API）
