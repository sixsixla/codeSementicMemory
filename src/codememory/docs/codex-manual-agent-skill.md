# Codex 手动记忆 Skill（Phase 5A）

这是一条兼容优先的 Codex 项目级使用流程。它不读取 Codex Desktop 私有数据库，
也不捕获隐藏推理；只在任务开始和结束时显式调用本地 CodeMemory。

## 前置条件

在本地启动服务，默认只监听回环地址：

```powershell
py -m codememory serve --db $env:LOCALAPPDATA\CodeSementicMemory\codememory.sqlite3 `
  --host 127.0.0.1 --port 8765
```

也可以直接使用 `codememory agent ...` CLI；CLI 和 HTTP 使用同一个
`AgentBridgeService`，不会绕过事件服务或直接写 SQLite。

## 任务开始

为每轮任务生成稳定的 task/session id，然后调用：

```powershell
codememory agent start `
  --db $db `
  --project-id project_j `
  --task-id task-<id> `
  --session-id session-<id> `
  --title "简短任务标题" `
  --intent "用户的原始 coding 需求" `
  --context-json '{"root_path":"D:/P4Workspace/client/mainline","branch":"mainline"}'
```

然后用需求中的业务词或代码词查询已有记忆：

```powershell
codememory agent query "NPC ShareManager" --project-id project_j --db $db \
  --retrieval-mode route
```

默认 `route` 模式是高召回的代码入口检索：`proposed`/`review` 卡片可以作为低权重
起点返回，但结果会携带 `trust_level`、`route_score`、绑定状态和证据。它们不是当前
源码事实，Agent 仍必须检查当前源码。需要高精度结果时使用
`--retrieval-mode trusted`；审计全部生命周期投影时使用 `audit`。

## 任务结束

将可见的 coding 证据整理成一个 JSON 文件，再调用 `capture`。至少保留：

- 本轮需求和最终摘要；
- 实际探索和修改的文件；
- 关键类型/方法；
- 编译、测试或其他验证结果；
- 被排除的错误候选；
- `success`、`failed` 或 `partial` 结果。

推荐结构：

```json
{
  "project_id": "project_j",
  "task_id": "task-<id>",
  "session_id": "session-<id>",
  "capture_id": "capture-<stable-id>",
  "intent": "用户需求",
  "explored_files": ["Assets/Script/.../Read.cs"],
  "modified_files": ["Assets/Script/.../Changed.cs"],
  "symbols": ["Namespace.Type", "Namespace.Type.Method"],
  "validations": [{"command": "compile", "status": "passed"}],
  "rejected_candidates": ["Assets/Script/.../Old.cs"],
  "summary": "完成了什么以及证据是什么",
  "outcome": "success"
}
```

```powershell
codememory agent capture .\capture.json --db $db
```

最后结束 session，并让本地 worker 立即尝试提取和合并：

```powershell
codememory agent finish .\finish.json --db $db
```

`finish.json` 至少包含 `project_id`、`task_id`、`session_id`、`summary` 和
`outcome`。默认 provider 是本地 deterministic mock；如果配置了兼容 provider，
再显式切换 `provider`，不要把云端调用隐含在事件写入路径中。

## 约束

- 手动摘要默认保存为 `summary` completeness，不能声称捕获了完整 transcript；
- 同一个 `session_id` 或 `capture_id` 重复提交应得到 duplicate；内容变化会 conflict；
- 不提交 reasoning、系统提示、插件目录、凭据或无关聊天内容；
- CodeMemory 返回的是候选记忆，不是当前源码的替代品；
- 失败任务也要记录，避免系统只学习成功路径。

这套流程以后可以被 MCP、wrapper 或 Agent hook 自动触发，但自动化层必须复用
相同的 start/query/capture/finish 协议。
