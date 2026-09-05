# Phase 6：Codex 自动记忆循环

Phase 6 把路由检索接入一个可恢复的 agent cycle，而不是把 hook 当成另一套
存储。所有观察仍然先进入 `codememory.event.v1`，抽取和合并只是可重建投影。

## 生命周期

```text
SessionStart
  └─ 创建/恢复 agent_memory_cycles 游标
UserPromptSubmit
  ├─ 记录 prompt:<turn_id>（重复提交幂等）
  ├─ route 检索当前项目卡片
  └─ 以 additionalContext 注入路线假设
PostToolUse
  └─ 记录可见的文件读取/编辑证据，不触发抽取
Stop
  ├─ 记录 checkpoint:<turn_id> 和最终可见摘要
  ├─ 请求一次受控的 `[CODEMEMORY_MAINTENANCE]` 续轮
  └─ 当前 Codex LLM 运行 prepare → 结构化 notes → learn
       └─ 质量评估 → 合并 → 保存卡片使用/结果反馈
SessionEnd
  └─ 写入 SESSION_ENDED 并关闭周期（不重复昂贵抽取）
```

`agent_memory_cycles` 保存外部线程、任务、会话和最后事件序号；
`memory_card_feedback` 保存 presented/used/outcome 反馈。反馈是附加证据，
不会因为一次失败自动删除卡片；它只对 route score 做很小的排序修正。

`cycle prepare` 返回有限事件包和 `input_hash`。Codex 当前模型按证据地址生成
最多 8 个 notes，`cycle learn` 将其包装为严格的 `ExtractionBatch`，复用原有
质量门和合并器；因此这里真正承担语义提取的是正在工作的 coding agent，而
不是隐含的第二个 embedding/LLM 服务。

## Codex 边界

hook 只使用 Codex 生命周期输入中可见的 prompt、tool input/output 和最终助手
消息。它不读取私有应用数据库，也不复制隐藏推理。命令 hook 采用 fail-open：
数据库或 provider 异常写入本地日志，不阻断当前编码任务。

全局配置位于用户的 `~/.codex/hooks.json`，仓库提供可迁移模板
`integrations/codex/hooks.json`。Codex 可能要求在 Hooks 设置中进行一次审核，
这是产品安全边界。抽取 provider 默认为离线 `mock`；需要真实 LLM 时由环境变量
选择现有 `openai-compatible`/`local` provider。

## 重试与维护

- `turn_id` 与 capture id 保证 prompt、tool、checkpoint 重试不重复写事件。
- extraction run 由事件上下文 hash 去重；`force` 仅用于明确的策略/模型更换。
- 关闭周期不删除任何源事件，卡片和反馈可按现有 projection 维护命令重建。
- route 结果始终带 trust level、binding 状态和 feedback 计数，Agent 必须把它当
  快速入口，源码、测试和当前快照仍是事实来源。
