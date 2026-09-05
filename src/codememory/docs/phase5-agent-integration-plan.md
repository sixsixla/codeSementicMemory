# Phase 5：AI Agent 真实接入与协同开发流程

## 目标

把现有 SQLite-first 记忆核心接入一个真实编码 Agent，先以 Codex 应用作为
第一条验证路径，跑通一轮完整的：

```text
任务开始
  -> 读取相关记忆
  -> Agent 进行代码探索、修改和验证
  -> 写入结构化编码事件
  -> LLM 提取候选记忆
  -> 质量门与项目作用域检查
  -> 合并为长期记忆卡片
  -> Project_J 绑定验证与 3D 图展示
```

本阶段验证的是“记忆系统是否能改善真实 coding loop”，不是先追求捕获
Codex 桌面应用的所有内部事件。

## 当前基线

Phase 4B 已经提供了真实可用的底层闭环：

- `codememory.event.v1` 事件契约和 `POST /v1/events`、`POST /v1/events/batch`；
- SQLite WAL canonical store、幂等、脱敏、outbox 和可重建 FTS；
- Codex `read_thread`/历史导入适配器，且不会复制隐藏推理；
- 候选抽取、质量门、版本化卡片、证据/绑定/link 关系；
- Project_J 的 P4 + CodeBaseMemory manifest 验证；
- 本地 3D graph inspector；
- 真实本地数据基线：54,684 events、2,304 candidates、1,798 cards、
  9,969 bindings、25,963 binding verifications。

因此 Phase 5 不重新设计存储，也不把 Supermemory、向量库或远程 SaaS 引入
核心路径。

## Codex 接入判断

接入必须分为三个等级：

### A. 显式桥接（首轮必须完成）

Codex 任务通过一个很薄的本地桥接层调用现有 HTTP/CLI：

- 任务开始：以 `session_started` 加上下文写入；
- 开始编码前：调用 `/v1/cards/search` 或 `/v1/search` 读取记忆；
- 任务过程中/结束时：批量发送用户请求、文件读取、编辑、命令、验证、
  VCS 和最终结果事件；
- 任务结束：发送 `session_ended`，由 outbox worker 继续抽取、质量检查和合并。

这个等级不依赖 Codex 桌面应用的隐藏 API。可以由本地 Codex skill、项目指令、
CLI 命令或一次性 sidecar 驱动，因此最适合先验证产品价值。

### B. MCP/Skill 工具化（第二步）

把 A 级桥接包装成 Agent 可调用的两个窄工具：

1. `memory_query`：按项目、任务和自然语言返回已接受卡片、证据和代码绑定；
2. `memory_record`：接收一个或一批经过 allowlist 的结构化事件。

工具层只负责传输和鉴权，不在工具中实现抽取、合并或直接改 SQL。若 Codex
应用当前版本可以配置本地 MCP/skill，就优先用它；否则保留同一套 HTTP/CLI
协议，换成项目内 skill 或 sidecar，不改变核心。

### C. 自动捕获（第三步）

只有在 A/B 级闭环证明有效后，才研究 Codex 桌面应用的稳定自动捕获方式：

- 可用的官方/稳定 hook 或 MCP 事件流；
- 本地历史增量读取；
- wrapper/sidecar 对任务生命周期做旁路捕获。

不能把当前 Codex App 的任务管理能力（例如读取/发送线程消息）等同于一个
面向第三方项目的实时 transcript callback；如果没有公开稳定接口，就采用
显式桥接和历史增量导入，不阻塞主项目。

## 事件映射

| Agent 行为 | `event_type` | 必须保留的字段 |
|---|---|---|
| 用户需求/补充说明 | `user_message` | 原文、项目、task/session、seq |
| Agent 最终说明 | `assistant_message` | 摘要、结果、引用的路径/符号 |
| 搜索、读取代码 | `tool_call` / `file_read` | 工具名、查询、路径、命中摘要 |
| 修改代码 | `file_edit` | 路径、symbols、diff/artifact hash、是否最终保留 |
| 构建、测试、检查 | `command_run` / `validation_run` | 命令摘要、退出码、测试结果 |
| Git/P4 变化 | `vcs_change` | revision/changelist、路径范围 |
| 用户评价/返工 | `user_feedback` | 接受、拒绝、修正及关联事件 |
| 任务生命周期 | `session_started` / `session_ended` | cwd、branch、repo、outcome |

只允许记录可用于 coding memory 的可见内容。隐藏推理、系统注入块、凭据和
无关插件说明必须在适配层丢弃；现有 Codex history importer 已有同类过滤规则。

## 双方职责

### Agent/适配层

- 识别 `project_id/task_id/session_id`；
- 在任务开始查询记忆；
- 将可见行为转换为事件 envelope；
- 保证 `seq`、`external_event_id` 和基本幂等；
- 不直接写 SQLite，不自行决定卡片合并和可信度；
- 在无法捕获时明确标记 `partial`，不伪造 `full`。

### CodeMemory 核心

- 校验、脱敏、幂等落库和 outbox；
- 用 LLM/本地 provider 从任务窗口提取 route、decision、failure、validation；
- 通过质量门过滤跨项目、弱证据和异常候选；
- 合并为版本化卡片并维护 evidence/link/binding；
- 对 Project_J 绑定做快照验证；
- 向 Agent 返回可解释的记忆与证据，而不是只返回相似文本。

### 人与代码事实

代码、构建/测试和 P4/CodeBaseMemory 快照是事实来源。Agent 的自然语言和
历史轨迹用于发现候选路由；只有有证据的结果才进入 accepted 长期记忆。

## 分阶段交付

### Phase 5A：本地 Agent Bridge

实现一个无状态薄桥接（优先 CLI + localhost HTTP）：

- `session start`：生成 session/task 上下文；
- `memory query`：调用现有 cards/search API；
- `event append/batch`：构造并发送标准 envelope；
- `session finish`：发送结束事件并报告 outbox job；
- 所有请求默认只绑定 `127.0.0.1`，不开放远程访问；
- 失败时可落 JSONL spool 作为可重放兜底，但不把 JSONL 作为主通道。

完成标准：不用修改核心表结构，能够从一个真实 Codex 任务产生完整的最小
session 事件流，并被现有 worker 消费。

### Phase 5B：Codex 应用最小真实测试

使用一个新的 Project_J coding 任务作为验收样本，建议优先选择已有的三类
任务之一（任务面板、护送归属、化形之魂），但使用新的 session id：

1. Codex 开始前查询相关卡片；
2. Agent 完成一次只读调查或小范围修改；
3. 发送文件/符号/验证/VCS 事件；
4. 运行 `extract-outbox`、质量门和 consolidation；
5. 用 `/v1/cards/search` 查询刚形成的记忆；
6. 用 Project_J baseline 做绑定验证并在 3D 图中查看状态。

验收指标：事件完整性、候选是否有证据、跨项目误连为零、route 的正确文件
命中、失败路径是否被保留、从任务结束到可查询卡片的延迟。

### Phase 5C：历史增量对照

把同一 Codex 任务的显式事件流与 `read_thread`/本地历史导入结果做对照：

- 去重是否稳定；
- 用户需求、最终修改、验证结果是否都能进入 canonical events；
- 系统注入和隐藏推理是否没有进入记忆；
- partial 数据是否被正确标记。

这一步用于评估自动捕获价值，不改变 A/B 级协议。

### Phase 5D：持续维护与评估

建立小型 coding-memory benchmark，持续测量：

- `memory_query` 的 route recall@k / file-symbol precision；
- accepted 卡片的证据闭合率；
- stale/renamed/unverified 绑定比例；
- 错误路由被反馈纠正的时间；
- Agent 额外 token、请求次数和延迟；
- 断线、重复发送、worker 重启后的恢复能力。

## 首轮明确不做

- 不捕获隐藏 chain-of-thought；
- 不把 Codex 桌面内部数据库当作稳定公共 API；
- 不为接入而重写 SQLite/event/extraction/consolidation 核心；
- 不先引入 embedding/vector DB；
- 不把 Project_J 的四文件选定快照当作完整仓库缺失扫描；
- 不在本阶段开放远程生产服务或自动修改代码。

## 下一项可执行任务

下一轮直接进入 **Phase 5A + 5B**：实现薄桥接、写一份 Codex 项目级使用指令，
启动本地服务，然后用一个新的 Project_J Codex 任务做端到端验收。若该轮证明
“查询记忆 -> 编码 -> 事件 -> 卡片 -> 绑定验证”稳定，再决定是否把桥接提升为
MCP 工具或做历史自动捕获。
