# Phase 4A：持续证据质量门与逻辑项目作用域

## Review Summary

### 目标

把本轮发现的“记忆卡质量问题”实现为 CodeSementicMemory 的永久产品能力：每次历史导入、抽取、合并和查询都自动进行可解释的质量判定，并保留原始证据和被拒绝结果。它不是一次性人工清洗，也不依赖某一个云端 LLM。

### 当前证据

- `src/codememory/history/codex.py:81-557` 已能批量导入 Codex 可见历史，但 `_project_id()` 目前主要按精确 `cwd` 哈希，导致一个逻辑上的 Project_J 被拆成多个 project 分区。
- `src/codememory/extraction/context.py:44-245` 会构造有界 head/tail 上下文；它还没有把“用户意图、实际文件证据、验证证据、流程噪声”持久化为质量判断。
- `src/codememory/extraction/providers.py:124-639` 的 `MockLLMProvider` 已有编码启发式，但仍会把“按上面计划执行开发”“交付模式”等流程性文本变成候选，且路径/符号仍需要后置校验。
- `src/codememory/extraction/service.py:37-238` 和 `src/codememory/consolidation/service.py:103-544` 已执行 schema、证据闭环和幂等合并，但尚无候选质量隔离层。
- 当前数据库已经保存 2,304 个候选和 1,798 张卡片，全部为 `proposed`；这批数据适合作为质量门的回放样本，而不是继续无条件扩充。

### 决策

下一步实施一个独立的 **Phase 4A：Continuous Quality Gate + Project Scope** 垂直切片。质量判定是可重建的投影和审计记录；原始事件、候选历史和卡片版本不删除、不覆盖。先做确定性规则和证据评分，再为以后接入 LLM 评审保留接口。

### 明确不做

- 本阶段不接 AST/LSP、Unity/P4 运行时验证；那是 Phase 4B。
- 不因为质量重跑而删除原始事件、候选或卡片历史。
- 不把 Embedding、图数据库、Supermemory 或 MCP 作为质量门依赖。
- 不把“模型说它正确”当作代码事实；没有结构化文件/符号/验证证据时只能是 `review` 或 `quarantine`。

## Acceptance Criteria（可测试）

1. 每个被评估的事件都有版本化的 `role`、`signal_score`、`decision` 和可读原因；同一输入和分类器版本重跑结果完全幂等。
2. `route_observation` 至少同时具备用户/需求信号和具体代码证据（结构化 `file_edit`/`file_read`、工具结果或明确文件路径）；只含助手将来时计划或确认语的候选必须进入 `quarantine`/`review`，不能默认进入可检索卡片。
3. `modified_file` 绑定只能由文件编辑、VCS 变更或明确的结构化工具证据产生；URL、通配符、技能文档、示例占位符和不在作用域内的路径不得成为 accepted binding。
4. `validation` 候选只能由实际 `command_run`/`validation_run` 事件产生；“应该能编译”之类自然语言不能伪造验证结果。
5. 失败/反绑定候选必须有显式失败或否定证据；纯“交付模式”“我会继续检查”等低信息文本不产生 accepted memory。
6. 三个 Project_J 原始路径在逻辑作用域报告中可归并到同一逻辑项目，同时保留各自的 raw `project_id`；CodeSementicMemory 中提及外部 Project_J 的任务被标记为 `scope_mismatch`，不静默改写其原始事件。
7. 默认卡片和搜索接口排除 `quarantine` 候选影响的结果；调试查询可以显式包含它们，并显示原因、分类器版本和证据。
8. 对现有数据库执行质量回放后，`events`、`artifacts` 和事件哈希不变；失败的质量评估不会损坏既有卡片版本或外键关系。
9. 三个授权种子任务继续保留可解释结果：化形之魂至少保留路线/决策/失败证据及其文件绑定；镖车任务面板和归属任务的实际文件证据不能被“流程噪声”覆盖。
10. 新增单元、集成、API/CLI 回归后，原有 53 个测试全部通过；质量回放在批处理模式下不会建立一个超出固定上限的巨型事务。

## Implementation Shape

### Change Inventory

| 文件/目标 | 动作 | 最终职责 |
|---|---|---|
| `migrations/0006_quality_gate.sql` | 新增 | 创建 `event_quality_evaluations`、`candidate_quality_reviews`、`logical_projects`、`project_aliases`、`quality_runs` 及索引；均为可重建/可审计投影。 |
| `src/codememory/quality/models.py` | 新增 | 定义事件角色、质量决策、评分维度、项目别名和回放报告的严格模型。 |
| `src/codememory/quality/classifier.py` | 新增 | 确定性事件分类、文本噪声识别、路径/符号规范化和评分；不调用网络、不读取隐藏推理。 |
| `src/codememory/quality/project_scope.py` | 新增 | 根据规范化 root、Git remote、P4 client 和显式 alias 计算逻辑项目作用域；保留 raw project 身份。 |
| `src/codememory/quality/store.py` | 新增 | 持久化质量评估、原因、版本、回放运行和作用域映射；支持按事件/候选/项目查询。 |
| `src/codememory/quality/service.py` | 新增 | 编排事件质量门、候选质量门、作用域回放和统计报告；失败只产生审计记录。 |
| `src/codememory/extraction/service.py:37-238` | 修改 | 在 provider 前取得带质量标签的上下文，在 `record_success` 后评估候选；保持严格 schema 和证据闭环。 |
| `src/codememory/extraction/context.py:44-245` | 修改 | 接受质量过滤/权重策略，并在 input hash 中记录分类器版本，避免不同质量策略误判为同一输入。 |
| `src/codememory/consolidation/service.py:103-544` | 修改 | 默认只合并 accepted/review 候选；quarantine 候选保留但不创建可检索卡片。 |
| `src/codememory/consolidation/store.py:895-950` | 修改 | 卡片查询、图谱和生命周期详情显示质量状态与原因，不删除旧版本。 |
| `src/codememory/api/app.py` | 修改 | 增加项目作用域、质量报告、候选质量详情和回放触发/状态接口；默认查询过滤 quarantine。 |
| `src/codememory/cli.py` | 修改 | 增加 `quality-report`、`quality-replay`、`project-alias` 命令，支持 dry-run、批量和 summary-only。 |
| `src/codememory/maintenance.py` | 修改 | 重建投影时包含质量表；明确不会触碰 canonical events/artifacts。 |
| `tests/unit/test_quality_classifier.py` | 新增 | 覆盖用户意图、助手计划、交付确认、文件/URL/通配符、验证和隐藏上下文边界。 |
| `tests/integration/test_quality_gate.py` | 新增 | 覆盖幂等回放、候选隔离、项目别名、失败回滚、外键和原始事件不变。 |
| `tests/integration/test_quality_api.py` | 新增 | 覆盖报告、默认过滤、调试 include-quarantine 和作用域 API。 |
| `fixtures/codex-history/selected-threads.json` 及新增噪声 fixture | 修改/新增 | 固化三个真实种子任务与流程噪声/跨项目文本的可重复质量样本。 |
| `src/codememory/docs/phase4a-quality-gate.md` | 新增 | 记录规则、评分、回放、迁移和已知误判边界。 |

### Data and Lifecycle Ownership

- `events`、`artifacts`、`tasks`、`sessions` 仍是不可变事实源。
- `event_quality_evaluations`、`candidate_quality_reviews` 和 `quality_runs` 是可重建的质量投影；每条记录包含 `classifier_version`、原因和时间。
- `logical_projects`/`project_aliases` 只建立 raw project 到逻辑作用域的映射，不重写历史事件的 `project_id`。
- `memory_candidates` 仍保留所有严格合法的 provider 输出；`quarantine` 不是物理删除，而是默认不可用于卡片/召回的状态。
- 卡片版本和生命周期仍由 `ConsolidationService`/`CardStore` 管理；质量门不能直接把卡片改成 stable。质量变差时只产生 review/stale 建议或审计事件。

### 质量状态

```text
accepted   -> 可进入合并和默认检索
review     -> 可保存、可人工/未来 LLM 复核，默认低优先级
quarantine -> 只保留证据和原因，不进入默认卡片/召回
```

评分不是单一“模型置信度”，至少拆成：`intent_signal`、`code_evidence`、`outcome_signal`、`specificity`、`scope_confidence`、`noise_penalty`。阈值和分类器版本写入数据库，不能隐藏在一次性脚本里。

### Critical Flow

正常路径：

```text
导入事件
  -> redact
  -> EventQualityClassifier
  -> 保存事件质量投影
  -> ContextAssembler(带角色/权重)
  -> provider 严格 JSON
  -> CandidateQualityGate
  -> accepted/review 候选
  -> ConsolidationService
  -> 卡片/图谱默认可检索
```

低质量或迟到路径：

```text
provider 输出合法但证据不足
  -> 保存 candidate + quarantine 原因
  -> 不创建新卡片、不覆盖旧版本
  -> 后续工具/验证事件到达
  -> 新 quality_run 重新评估同一候选/新候选
  -> 只有达到阈值才进入合并
```

质量分类器或 provider 失败：

```text
失败 -> quality_run=failed + 错误原因
     -> canonical events 不变
     -> 既有 candidates/cards 不变
     -> 可用相同 input_hash 和新 classifier_version 重试
```

### 规则基线

1. 用户消息优先作为 intent；助手消息只有在包含已发生的具体结果、文件或验证证据时提升权重。
2. `我会/先读取/按计划/交付模式/I’ll/I will` 等未来时和确认语默认降权；不得单独生成 route card。
3. `file_edit`/`vcs_change` 是 modified-file 的强证据；助手提到的路径只能是 context hint。
4. URL、glob、`A.cs`/`*.cs`、技能/AGENTS 文档和仓库外路径默认 quarantine，除非显式 alias 或结构化工具证据证明其属于目标项目。
5. `command_run`/`validation_run` 才能产生 validation；退出码、结果和命令文本分别记录。
6. 用户明确否定的候选进入 anti-binding，但仍需绑定到否定事件，不能从任意关键词推断。

## Implementation Steps

1. **固化数据契约与迁移**：新增 `0006_quality_gate.sql` 和 Pydantic 模型；为旧数据库提供 `legacy_unreviewed` 默认状态，不改写事实表。
2. **实现确定性事件分类器**：先覆盖 event type、用户/助手角色、路径/符号规范化、系统/技能/流程噪声和验证证据；每个判定返回原因和版本。
3. **实现逻辑项目作用域**：规范化 Windows 路径，读取已有 repository 元数据；提供显式 alias 命令和 dry-run 报告；raw project 永不被静默合并。
4. **接入抽取与合并**：让上下文 hash 包含质量策略；候选落库后立即写 review；Consolidator 默认跳过 quarantine，并在详情/图谱中展示状态；候选列表和搜索也提供默认隔离与显式审计开关。
5. **提供回放与查询面**：实现 `quality-report`/`quality-replay`、API 和 UI 过滤；回放按任务/批次执行，支持中断后继续，不使用 destructive delete；对 CodeSementicMemory root 中的外部 Project_J 引用记录 `scope_mismatch`。
6. **用真实样本校准**：对三个授权任务及流程噪声 fixture 做精确断言；再对当前数据库做 dry-run，比较 accepted/review/quarantine、Project_J 归并和 CodeMemory scope mismatch。
7. **更新文档并冻结边界**：记录规则版本、误判处理、回滚方式和 Phase 4B 入口；只有质量回放指标达到验收标准后，才继续抽取剩余历史。

## Risks and Mitigations

| 风险 | 缓解 |
|---|---|
| 规则过严导致漏记 | 保留 `review` 状态和全部证据；阈值版本化，允许重跑，不直接丢弃。 |
| 路径相似导致错误合并 | 需要 root + remote/P4 client 或显式 alias；只在报告中建议合并，不重写 raw ID。 |
| 旧卡片已经含噪声 | 质量评估是旁路投影；默认召回过滤，历史仍可审计，后续由人工/快照验证决定生命周期。 |
| 大库回放锁表/超时 | 任务级批处理、固定 batch size、quality_runs 断点和 summary-only 输出。 |
| 分类器规则漂移 | 所有评估保存 `classifier_version` 和输入 hash；新版本生成新 run，不覆盖旧评估。 |

## Verification Map

| 关键声明 | 验证证据 |
|---|---|
| 质量门是持续能力 | 每次 extract/consolidate 的集成测试 + API/CLI report；无独立一次性脚本路径。 |
| 原始事实不变 | 回放前后事件数量、事件 canonical hash、artifact hash 对比；SQLite deep integrity。 |
| 流程噪声被隔离 | synthetic acknowledgement/skill/未来时 fixture 的 decision 和 reason 断言。 |
| 真实编码记忆不被误杀 | 三个授权线程的候选、证据事件、文件绑定和卡片查询断言。 |
| Project_J 作用域可维护 | 三个 raw project alias 的 dry-run/apply/replay 幂等测试，CodeMemory 外部引用得到 mismatch 标记。 |
| 迟到证据安全 | 先 quarantine、后到达 file/validation event 再重评的集成测试；旧卡片版本和 FK 保持不变。 |
| 默认召回干净 | `/v1/cards/search`、`/v1/graph` 和 CLI `cards` 的默认/调试过滤测试。 |

## Diagram Decision

保留上面的单条数据流和迟到路径图即可：本阶段的核心是事件质量状态如何流经已有抽取/合并边界，不需要额外类图。新增类的职责已经在 Change Inventory 中列出。

## Design Guardrails

- 不把一次性人工抽样、删除数据库、手工 SQL 或“重新跑全库”当成产品质量能力。
- 不用质量分数直接晋升 stable；稳定卡片仍需后续代码快照验证和显式生命周期动作。
- 不把助手的计划、系统注入、技能说明或用户粘贴的示例当成实际修改证据。
- 不因项目归并而修改历史事件的 raw `project_id`；逻辑作用域必须可解释、可撤销。
- 不提前加入 Embedding、AST/LSP、MCP 等与本阶段验收无关的依赖。

## Stop Rule

当且仅当质量门、项目作用域、回放/查询接口、真实样本回归和原始事实不变性验证全部通过后，才允许继续抽取剩余 1,490 个任务。若质量指标未达标，继续扩大历史只会放大噪声，应停在本阶段修正规则和 fixture。
