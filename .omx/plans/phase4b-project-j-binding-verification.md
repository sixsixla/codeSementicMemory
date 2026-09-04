# Phase 4B：Project_J 代码绑定验证

## Review Summary

### Goal

让记忆卡中的 `path/symbol` 绑定可以对照真实代码快照得到可解释的
`verified / missing / renamed / stale / unverified / rejected` 状态，并把检查结果
作为可重建的 SQLite 投影和审计记录。P4 与 CodeBaseMemory 只作为 Project_J 的
外部证据适配器；SQLite 核心不依赖它们，也不把其他工程误判成已验证。

### Scope

- 新增快照、验证运行和绑定检查的迁移与存储。
- 提供统一的 manifest 契约，接受 P4 `fstat`、CodeBaseMemory MCP
  `search_graph/index_status` 或未来 AST/LSP 适配器的结果。
- Project_J 作用域的验证服务、CLI、HTTP API、3D 图谱状态展示。
- 用三个已授权 Project_J Codex 任务作为真实验证样本；不读取或提交源代码、令牌、密码。

### Out of scope

- 不把 P4/CodeBaseMemory SDK 固定为运行时依赖。
- 不在验证时自动修改卡片 statement、版本历史或原始事件。
- 不以一个“只包含选中路径”的快照推断整个仓库的缺失；有限快照只能产生
  `unverified`。
- 不为 CodeSementicMemory 或其他项目发起 P4/CodeBaseMemory 调用。
- provider manifest 的 `project` 标签若明确不是 Project_J，则整个验证请求为
  `not_applicable`，不写入快照或绑定投影。

## Change Inventory

| 目标 | 作用 |
|---|---|
| `migrations/0007_binding_verification.sql`、`migrations/0008_snapshot_manifest_content.sql` | `code_snapshots`、`verification_runs`、`binding_verifications` 及索引；0008 保存完整规范化 manifest 内容。 |
| `src/codememory/verification/models.py` | manifest、文件/符号、检查和运行结果的版本化模型；支持 MCP 常见 envelope。 |
| `src/codememory/verification/providers.py` | 路径/符号索引、P4 `fstat` 只读采集器、CodeBaseMemory manifest 桥接。 |
| `src/codememory/verification/store.py` | 快照幂等写入、当前绑定投影、审计历史、报告和中断运行恢复。 |
| `src/codememory/verification/service.py` | Project_J 作用域守卫、状态判定、覆盖范围安全规则和批处理编排。 |
| `src/codememory/api/app.py` | `/v1/verification/bindings`、`/v1/quality/verify-bindings`、report/list/snapshot detail 接口。 |
| `src/codememory/cli.py` | `verify-bindings`/`verify-project-j`、`verification-report`，支持 manifest、P4 fstat、dry-run/write。 |
| `src/codememory/consolidation/store.py`、`web/*` | 卡片详情与 3D 节点显示最新绑定验证状态。 |
| `fixtures/verification/project-j-baseline.json` | 不含源码的四文件 Project_J 基线，供回归和真实样本重放。 |
| `tests/integration/test_phase4b_verification.py` | 幂等、覆盖安全、重命名/陈旧/缺失、P4 解析、API 和跨项目隔离。 |

## Data and Lifecycle Ownership

```text
events / artifacts / candidates / card versions
        (canonical facts; never rewritten by verification)
                         │
                         ▼
                 memory_card_bindings
                         │
       P4 / CodeBaseMemory / AST-LSP manifest snapshots
                         │
                         ▼
              binding_verifications (per run)
                         │
                         ▼
       binding.status + metadata.verification (current projection)
```

- `code_snapshots` 按 `logical_project_id + provider + manifest_hash` 去重；完整规范化
  manifest 内容写入 `manifest_json`，来源元数据和首次采集时间可审计。
- `manifest_hash` 和验证 `input_hash` 只对时间无关的内容计算；重复采集同一证据不会
  产生新的快照身份，但仍会留下新的运行审计行。
- `binding_verifications` 每次运行保留一行，包含证据、原因、解析后的路径/符号和
  `applied` 标记。
- write 模式只更新当前绑定投影；`unverified` 不会把已有的权威状态降级，避免
  提供者短暂不可用造成回归。
- dry-run 仍记录运行和快照，便于比较输入 hash，但不改变当前绑定状态。
- 选中范围只包含当前 card version；旧版本仍通过 card history 保留。

## Manifest Contract

```json
{
  "schema_version": "codememory.verification_manifest.v1",
  "provider": "p4 | codebase_memory | filesystem",
  "project": "Project_J",
  "root_path": "D:/P4Workspace/client/mainline",
  "revision": "392462",
  "status": "ready | partial | unavailable | failed",
  "metadata": {"coverage": "complete | selected_paths", "symbol_coverage": "optional"},
  "files": [{"path": "game_client/Project_J/Assets/...cs", "head_rev": "5", "have_rev": "5"}],
  "symbols": [{"name": "Type", "qualified_name": "Namespace.Type", "file_path": "Assets/...cs"}]
}
```

`coverage=complete` 才允许“未找到”变成 `missing`；P4 批量 `fstat` 和 MCP
定点搜索默认是 `selected_paths`。路径归一化会去掉已知的 client/depot/
`Project_J` 前缀，但不会猜测未知仓库根。

## Critical Flows

### 正常写入

```text
verify-bindings
  -> resolve logical scope
  -> Project_J guard
  -> normalize provider manifests
  -> persist content-addressed snapshots
  -> select current bindings (optional task filter)
  -> resolve path/symbol + P4 revision
  -> append binding_verifications
  -> optionally update current binding projection
  -> finish verification_runs with counts/input_hash
```

### 安全降级

```text
provider unavailable / bounded snapshot
  -> unverified + explicit reason
  -> no authoritative status demotion

symbol found at another file
  -> renamed + resolved_path

file exists but symbol disappeared or P4 haveRev != headRev
  -> stale
```

### 非 Project_J

```text
logical scope != name:project_j
  -> not_applicable
  -> no provider parsing/call
  -> no snapshot, binding, or card mutation
```

## Diagram Decision

保留上面的两条最小数据流图。Phase 4B 的关键关系是“绑定—快照—每次检查—当前
投影”，额外类图不会提升决策清晰度；3D UI 直接复用现有 card/file/symbol 节点，
以 `binding_status` 着色。

## Design Guardrails

- 适配器只传结构化 manifest；核心不执行隐式网络、P4 checkout 或源码写操作。
- 外部工具失败必须保留失败原因，不能将空结果当成完整仓库。
- `renamed` 需要同一符号在不同当前路径被解析；仅相似文件名不够。
- P4 `fstat` 只有文件/版本证据；没有符号索引时，不能把“文件存在但类名未列出”
  直接判为 `stale`。
- P4 `haveRev/headRev` 不一致只能标记 `stale`，不能删除绑定。
- 任何状态更新都不改写事件 hash、候选 JSON、卡片 statement 或版本关系。
- 快照覆盖范围写入 metadata，避免“小样本验证”污染全库缺失统计。
- 3D UI 是只读展示，状态仍以 SQLite 审计记录为准。

## Verification Map

| 声明 | 证据 |
|---|---|
| 状态判定可重放 | `test_project_j_binding_is_verified_and_write_is_idempotent`，相同 manifest/input hash 可重复运行。 |
| 不可用不降级 | `test_missing_unavailable_and_non_project_scope_are_safe`。 |
| 重命名/陈旧可解释 | `test_project_j_renamed_and_stale_states_are_explainable`。 |
| P4 只读适配器可解析 | `test_p4_collector_is_read_only_and_parses_ztag_output`。 |
| API/报告可用 | `test_verification_api_exposes_report_and_write`。 |
| 真实 Project_J 样本 | CodeBaseMemory `index_status=ready`（817,030 nodes / 2,022,384 edges）；P4 read-only `fstat` 四个种子文件；任务范围运行 38 bindings，4 verified、33 unverified、1 rejected；全量投影运行 6,412 bindings。 |
| 原始事实不变 | 现有全量回归、health deep integrity、绑定只更新 projection 的 SQL 约束；快照 manifest 可按 snapshot detail 重放。 |

## Stop Rule

在 manifest 覆盖声明、Project_J 真实样本、全量测试/lint/API smoke 和 deep integrity
全部通过前，不扩大到其他项目或把选中路径快照宣称为全库缺失扫描。下一阶段才考虑
完整仓库快照、AST/LSP 精确符号、自动周期刷新和更细的生命周期晋升策略。
