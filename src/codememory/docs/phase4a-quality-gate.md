# Phase 4A：持续证据质量门交付记录

## 交付范围

Phase 4A 把“从编码对话中学习长期记忆”变成一条可重放、可解释、可维护的产品链路：

```text
canonical event
  -> deterministic event quality
  -> LLM/provider candidate
  -> candidate quality review
  -> card consolidation / default retrieval
```

质量数据是 SQLite 中的旁路投影。`events`、`artifacts`、`memory_candidates`、卡片版本和证据关系仍是事实或历史来源，不会因为质量重跑被删除或改写。

本轮新增迁移 `0006_quality_gate.sql`，创建：

- `event_quality_evaluations`：事件角色、分数、决策、维度、原因和分类器版本。
- `candidate_quality_reviews`：候选的证据完整性、绑定有效性和决策。
- `logical_projects` / `project_aliases`：保留 raw project id 的逻辑作用域映射。
- `quality_runs`：dry-run/write 回放审计、输入哈希、状态和错误。

所有批量质量写入以 500 条为上限；进程被中断时，下次回放会把超过五分钟的孤立 `running` 审计标为 `failed`，不会触碰原始事件。

`rebuild-memory --yes` 可以在策略升级时清除候选、卡片、逻辑项目和质量投影；它不删除 canonical projects/tasks/sessions/events/artifacts，也不删除 durable outbox。清除后可从保留的事件重新导入、抽取和回放。

## 决策语义

| 决策 | 默认行为 | 证据含义 |
| --- | --- | --- |
| `accepted` | 可进入合并和默认检索 | 具备明确意图以及具体代码/验证证据 |
| `review` | 保存并显示，供人工或未来 LLM 复核 | 有价值但缺少绑定、快照或边界证明 |
| `quarantine` | 不影响默认卡片、搜索和 3D 图；原始证据保留 | 流程确认、缺失证据、无效绑定或明显合成噪声 |

旧数据库中的卡片不会被物理删除。默认 `cards`、`cards/search`、`memories`、`memories/search` 和 `graph` 只隐藏明确由 quarantine 候选支撑的卡片/候选；审计客户端可传 `include_quarantine=true` 或 CLI `--include-quarantine` 查看完整历史。没有质量记录的旧卡片标记为 `legacy_unreviewed`，仍可见但不会被误报为 accepted。

## 事件质量规则

分类器版本当前为 `coding-quality-gate-v2`，实现位于 `quality/classifier.py`。规则或维度改变时必须递增版本，旧投影会在下次抽取/回放中被重新评估：

1. 用户的实质编码请求是 `intent`；助手确认语、未来时计划和编排消息是 `noise/quarantine`。
2. `file_read`、`file_edit`、`tool_*`、`vcs_change` 提供代码证据；路径会拒绝 URL、glob、技能/AGENTS 文档和明显辅助目录。
3. `command_run` / `validation_run` 只有在事件中存在实际命令和结果时才可作为验证证据；“应该能编译”不是验证。
4. `user_feedback`、会话生命周期和未知事件保留为 review/provenance，不会单独制造稳定事实。

每一条评估同时保存 `intent_signal`、`code_evidence`、`actual_validation`、`scope_mismatch` 等维度和人类可读原因，因而规则升级可以用相同事实回放并比较输入哈希。

## 候选与卡片质量门

`route_observation` 至少需要：

- 一个非确认式的用户意图或等价陈述；
- 至少一个具体 repository binding（路径或合格符号）；
- binding 的证据事件必须仍存在。

没有 binding 但只有自然语言/搜索共现的候选进入 `review`，不会直接成为默认路由。URL、通配符、外部记忆服务符号和辅助文档路径会降低到 `review` 或 `quarantine`。`validation` 候选必须引用实际命令事件；`failure` / `anti_binding` 必须有显式失败、拒绝或禁止信号。以 CodeSementicMemory 为 root 的任务若提及外部 Project_J，会记录 `scope_mismatch=true` 并至少降为 `review`，不会把外部项目事实静默归入本项目。

候选落库后立即生成 review；合并器遇到 quarantine 只写一条拒绝审计，不创建或覆盖卡片。迟到的文件/验证事件可以触发同一候选的新一轮 review，旧卡片版本和证据关系保持不变。

## 逻辑项目作用域

规范化 Windows/POSIX root 后，常见的 `Project_J` checkout 默认以 `name:project_j` 归并；其他 root 仍按 root 隔离。raw project id 永远不被改写。对有歧义的父目录使用显式别名：

```powershell
codememory project-alias `
  --db $db `
  --raw-project-id codex-project-07b7a7f1935bd920 `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --alias-value "D:/P4Workspace/client/mainline -> Project_J" `
  --normalized-root D:/P4Workspace/client/mainline `
  --evidence-json '{"source":"reviewed local Codex history","reason":"mainline tasks target Project_J"}'
```

显式 alias 的优先级高于 basename/root 启发式，并在 `quality-projects` 中标记 `effective=true`。因此本地历史里的三个 Project_J raw scope 可在查询时合并，同时仍能追溯各自来源。

## 回放与报告

```powershell
# 只计算并记录审计，不写入评估/别名投影
codememory quality-replay --db $db --limit 100000

# 应用同一版本的质量投影；幂等，可安全重试
codememory quality-replay --db $db --limit 100000 --write

codememory quality-report --db $db
codememory quality-report --db $db --logical-project-id logical-319e6e98c97340e7807d6bb7
```

`quality_runs.input_hash` 对选中的事件/候选 id、过滤条件和分类器版本计算。dry-run 不写评估和别名，但会留下审计记录；write 运行以 500 条事务批次写入。最近一次本地全量回放（2026-09-04）结果：

- 54,684 个 canonical events：7,138 accepted、46,943 review、603 quarantine；其中 8 条被标记 `scope_mismatch`。
- 2,304 个 candidates：352 accepted、1,937 review、15 quarantine。
- 其中 5 个候选带 `scope_mismatch`，均为 review，不进入 CodeSementicMemory 的 accepted 路由。
- 14 个有效逻辑项目作用域、16 个有效 raw project id；18 条 alias（其中 Project_J 逻辑作用域有 3 个有效 raw id；被显式 alias 覆盖的旧启发式 alias 仍可审计）。
- 1,798 张历史卡片仍为 `proposed`；其中 15 张由 quarantine 候选支撑，默认卡片/搜索/图谱可见 1,783 张。

同一版本随后对 Project_J 逻辑作用域做了专项 write 回放（run
`quality-077ea4b6-02e0-4477-a269-da468214fa53`）：选中 54,089 个事件和
2,292 个候选，分别得到 `7,075/46,421/593` 与 `352/1,925/15`
（accepted/review/quarantine），作用域内 `scope_mismatch=0`。该运行的
`input_hash=1293241e707801eb24a730ebf0ceeabeaa32707ee07ca8392f91e759b916f87a`
可用于后续回归，重跑同一筛选条件应保持一致。

回放前后的原始事件数量、canonical event hash、artifact hash 和外键关系保持不变。`quality-report` 的 `latest_runs` 会同时显示成功和中断恢复记录，便于诊断调度器或终端被关闭的情况。

## HTTP 与 3D 检查面

新增接口：

| 路由 | 作用 |
| --- | --- |
| `GET /v1/tasks/{task_id}/quality` | 单任务质量覆盖和决策统计 |
| `GET /v1/quality/report` | 全局/项目/逻辑作用域报告 |
| `POST /v1/quality/replay` | dry-run 或 write 回放 |
| `GET /v1/quality/projects` | 逻辑项目及有效 alias |
| `POST /v1/quality/projects/aliases` | 登记人工复核过的 alias |
| `GET /v1/quality/candidates/{candidate_id}` | 查看候选原因、维度和版本 |

`GET /v1/cards`、`GET /v1/cards/search` 和 `GET /v1/graph` 支持 `include_quarantine=true` 调试开关。默认 3D 图保留事件 provenance，但隐藏 quarantine candidate/card；点击候选或卡片可看到质量决策、绑定和来源证据。

`GET /v1/tasks/{task_id}/memories` 与 `GET /v1/memories/search` 也默认隐藏 quarantine 候选，可用同名参数审计。质量报告的事件/候选统计包含 `scope_mismatches`，用于发现“在 CodeSementicMemory 任务中讨论外部 Project_J”的跨项目引用。

## 已知边界与下一阶段

- 当前质量门是确定性本地规则，不依赖云端 LLM、embedding、图数据库或 MCP；未来的 LLM judge 只能在同一版本化 review contract 后面补充证据。
- 还没有 AST/LSP、Git/P4 snapshot 或 CodebaseMemory 符号解析，所以 accepted binding 仍是“有形状的代码证据”，不是当前源码存在性的证明。
- 仍有 `review` 候选和 legacy 卡片，这是有意保留的可审计状态；不应把质量分数直接当作 `stable` 晋升条件。
- 质量门验证完成后，才允许按批次继续处理剩余未抽取历史；扩大数据前应先观察 Project_J 作用域和噪声比例。
