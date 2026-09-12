# Logical Project Scope 与历史 backlog

CodeMemory 的原始事件仍按 raw `project_id` 保存。`logical_project_id` 是跨
checkout、父目录和 Codex 会话的语义作用域；`project_aliases` 选择一个 raw
project 的有效 logical owner，但不会改写事件、任务或卡片的原始来源。

## 作用域查询

卡片、candidate、event、AgentBridge 和 graph 查询都接受
`logical_project_id`。查询会先展开有效 alias，再在 raw project 集合中检索，
结果携带 `logical_project_id` 与 `logical_scope_raw_project_ids`。同一
`kind + canonical_key` 的跨 raw 卡片在列表/搜索结果中合并为一个 route 结果，
并保留 `source_card_ids` 和 `source_project_ids` 供审计；单卡详情仍按原始
`card_id` 保留完整版本和证据。

常用入口：

```powershell
codememory agent query "NPC 分享" `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --retrieval-mode route

codememory cards `
  --logical-project-id logical-319e6e98c97340e7807d6bb7

codememory graph `
  --logical-project-id logical-319e6e98c97340e7807d6bb7
```

`route` 仍然是快速入口语义：提议卡和质量为 `review` 的卡可以出现，
`quarantine` 默认隐藏；源码、测试和当前快照仍是事实来源。

## 历史 backlog

outbox 是事件投影投递队列，不是记忆内容本身。抽取的幂等单位是 task 的
context `input_hash`，所以大批量历史数据必须按唯一 task 聚合处理。

先只读查看：

```powershell
codememory backlog plan `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --limit-tasks 50 `
  --min-events 2
```

处理器每个 task 只租约一个代表性 outbox job，完成一次
`extract_task -> consolidate` 后才确认该 task 的所有 pending job。抽取失败
会留下 retry/dead 证据，不会伪装成 completed：

```powershell
codememory backlog process `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --limit-tasks 50 `
  --provider mock
```

处理器默认跳过没有文件、工具、命令、验证或 VCS 证据的纯聊天 task，并在结果中
标记 `no_coding_evidence`；消息正文中出现具体源码路径/符号且已通过质量投影的
`assistant_message`/`user_message` 也算 coding evidence，因此导入的 Codex 历史
无需先转换成 `file_*` 事件。如需显式处理仍然没有这些信号的 task，才使用
`--include-non-coding`。

开发验证或策略试跑使用：

```powershell
codememory backlog process `
  --logical-project-id logical-319e6e98c97340e7807d6bb7 `
  --limit-tasks 10 `
  --dry-run
```

推荐顺序是先修正 Project_J 的显式 alias，再处理最近且有文件/验证证据的
30～50 个 task；观察 accepted/review/quarantine 比例和自然语言到路径的检索
结果后，再以 50～100 个 task 为批次继续。历史卡片默认保持 `proposed`，不因
抽取成功自动晋升为 `verified` 或 `stable`。

批处理前先备份数据库并运行一次完整 integrity check。`events`、artifact、
卡片版本和证据关系不通过 backlog 命令删除或改写；完成的 outbox 记录先保留，
后续再设计有审计依据的归档策略。
