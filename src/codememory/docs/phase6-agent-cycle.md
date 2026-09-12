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
  ├─ 仅接收 coding shell/edit matcher
  ├─ 记录可见的文件读取、编辑或验证证据
  └─ 无路径、无验证证据的工具调用直接丢弃，不触发抽取
Stop
  ├─ 记录 checkpoint:<turn_id> 和最终可见摘要
  ├─ 以 evidence input_hash 创建 agent_memory_maintenance
  ├─ 写出只含已脱敏事件的 request/learn JSON 文件
  ├─ 请求一次受控的 `[CODEMEMORY_MAINTENANCE]` 续轮
  └─ 当前 Codex LLM 读取 request、填写 notes、执行 learn
       └─ 质量评估 → 合并 → 保存卡片使用/结果反馈
SessionEnd
  └─ 写入 SESSION_ENDED 并关闭周期（不重复昂贵抽取）
```

`agent_memory_cycles` 保存外部线程、任务、会话和最后事件序号；
`agent_memory_maintenance` 保存每个 input hash 的请求次数、当前状态、模型、
note/candidate 数量和错误；
`memory_card_feedback` 保存 presented/used/outcome 反馈。反馈是附加证据，
不会因为一次失败自动删除卡片；它只对 route score 做很小的排序修正。

`cycle prepare` 返回有限事件包和 `input_hash`。Stop hook 会把这个包写入
平台本地数据目录下的 `maintenance/*.request.json`，同时生成一个合法的空
`*.learn.json` 模板。Codex 当前模型按证据地址生成
最多 8 个 notes，`cycle learn` 将其包装为严格的 `ExtractionBatch`，复用原有
质量门和合并器；因此这里真正承担语义提取的是正在工作的 coding agent，而
不是隐含的第二个 embedding/LLM 服务。

## Codex 边界

hook 只使用 Codex 生命周期输入中可见的 prompt、tool input/output 和最终助手
消息。它不读取私有应用数据库，也不复制隐藏推理。命令 hook 采用 fail-open：
数据库或 provider 异常写入本地日志，不阻断当前编码任务。

全局配置位于用户的 `~/.codex/hooks.json`，仓库提供可迁移模板
`integrations/codex/hooks.json`。Codex 可能要求在 Hooks 设置中进行一次审核，
这是产品安全边界。命令强制 `-X utf8`；输入解码还会修复可安全判断的非法
Windows 路径反斜杠，并把孤立 surrogate 替换为 U+FFFD。无法恢复的 payload
仍然 fail-open，并以 `CODEMEMORY_HOOK_ERROR` JSON 记录写入本地 hook 日志；
错误记录只保留类型、阶段和调用帧位置，不复制 prompt/tool input。

默认 `CODEMEMORY_HOOK_PROVIDER=agent`：真正承担语义整理的是正在工作的 Codex，
不隐含调用第二个 LLM 或 embedding 服务。显式选择 `mock`、
`openai-compatible` 或 `local` 时，Stop 可走既有同步 provider 流程。

默认根目录规则将 `D:\P4Workspace\client\mainline` 及其子目录统一映射到
`project_j`。`CODEMEMORY_PROJECT_ROOTS_JSON` 可增加其他本地项目，例如：

```json
{"my_project":["D:/Work/MyProject"]}
```

Hook 生成的 task/session ID 同时包含 project identity；同一 Codex thread 即使切换
工作目录，也不会再因全局 task/session 主键复用而发生跨项目 ownership conflict。

## 重试与维护

- prompt、tool 和 Stop checkpoint 的 capture id 同时包含 Codex 回调 ID 与可见
  内容 hash：完全相同的重试会去重；同一回调 ID 下内容发生变化时会作为新证据，
  不再触发“相同身份、不同内容”冲突。SessionEnd 不重复复制 Stop 已保存的消息。
- 同一 maintenance input hash 默认最多派发 2 次；第一次续轮未执行成功的
  `cycle learn` 会在 `stop_hook_active` 回调中标为 `failed`，下一个正常 Stop
  可以重试。达到上限后停止阻塞原任务。
- 新 evidence hash 会把同 cycle 内尚未完成的旧请求标为 `superseded`。
- 成功或空 notes 的 `cycle learn` 都会把请求标为 `completed`；空 notes 表示当前
  证据没有值得长期保存的内容，不是链路失败。
- Stop 只检查最近一条 `user_message` 之后的结构化代码证据；较早轮次曾经读写过
  文件不会让后续纯问答重复触发维护。
- extraction run 由事件上下文 hash 去重；`force` 仅用于明确的策略/模型更换。
- 关闭周期不删除任何源事件，卡片和反馈可按现有 projection 维护命令重建。
- route 结果始终带 trust level、binding 状态和 feedback 计数，Agent 必须把它当
  快速入口，源码、测试和当前快照仍是事实来源。

## 运行健康检查

```powershell
py -3 -X utf8 -m codememory hook-health --hours 24
```

报告同时展示：各 hook/项目事件数、cycle 状态、maintenance
pending/completed/failed/superseded、最近 extraction/candidate/card 时间、按当前规则
重新计算的项目归属差异、hook 事件 outbox 状态和结构化 fail-open 错误。加
`--strict` 可将 `degraded` 转成非零退出码，便于后续自动巡检。
